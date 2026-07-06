/**
 * Sandbox execution seam — the provider-pluggable boundary for running an
 * agent's child process.
 *
 * This is the CONTRACT, the `local` adapter, and an isolated `aca-job` provider
 * (Azure Container Apps dynamic sessions) scaffold. The default provider is
 * `local`, so importing this module is side-effect-free and changes no runtime
 * behavior: `aca-job` is opt-in via SANDBOX_PROVIDER and is not enabled in any
 * environment. The build-time wiring into the adapter spawn path
 * (apps/paperclip/patch-adapter-sandbox.mjs) is likewise gated on
 * SANDBOX_PROVIDER, so the default path is byte-unchanged.
 *
 * The seam lets a task run either in-container (`local`, today's behavior) or
 * in an isolated ephemeral sandbox (`aca-job`) without changing the caller.
 *
 * Result shape matches the adapter's child-process return, so the seam is a
 * drop-in at the spawn dispatch:
 *   { exitCode: number|null, signal: string|null, timedOut: boolean,
 *     stdout: string, stderr: string }
 */

import { spawn } from "node:child_process";

const DEFAULT_MAX_BUFFER = 10 * 1024 * 1024; // 10 MiB per stream, then truncate

/**
 * The `local` provider: runs the command in the current container, exactly as
 * today. A thin, well-tested wrapper over child_process.spawn that honors a
 * timeout and an AbortSignal and never rejects — failures come back in the
 * result shape (so callers branch on exitCode/timedOut, not try/catch).
 */
class LocalSandbox {
  constructor(config = {}) {
    this.provider = "local";
    this.config = config;
  }

  /**
   * @param {string} cmd
   * @param {string[]} args
   * @param {{env?:object, cwd?:string, timeoutMs?:number, signal?:AbortSignal,
   *          maxBuffer?:number, input?:string}} opts
   * @returns {Promise<{exitCode:number|null, signal:string|null,
   *                    timedOut:boolean, stdout:string, stderr:string}>}
   */
  async exec(cmd, args = [], opts = {}) {
    const {
      env,
      cwd,
      timeoutMs,
      signal,
      maxBuffer = DEFAULT_MAX_BUFFER,
      input,
    } = opts;

    return await new Promise((resolve) => {
      let stdout = "";
      let stderr = "";
      let outLen = 0;
      let errLen = 0;
      let timedOut = false;
      let settled = false;
      let timer = null;

      const child = spawn(cmd, Array.isArray(args) ? args : [], {
        env: env || process.env,
        cwd: cwd || undefined,
      });

      const onAbort = () => {
        try { child.kill("SIGTERM"); } catch { /* already gone */ }
      };

      const cleanup = () => {
        if (timer) clearTimeout(timer);
        if (signal && typeof signal.removeEventListener === "function") {
          signal.removeEventListener("abort", onAbort);
        }
      };

      const done = (res) => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(res);
      };

      if (signal) {
        if (signal.aborted) {
          try { child.kill("SIGTERM"); } catch { /* noop */ }
        } else if (typeof signal.addEventListener === "function") {
          signal.addEventListener("abort", onAbort, { once: true });
        }
      }

      if (timeoutMs && timeoutMs > 0) {
        timer = setTimeout(() => {
          timedOut = true;
          try { child.kill("SIGTERM"); } catch { /* noop */ }
        }, timeoutMs);
      }

      // Capture output, truncating each stream at maxBuffer (never OOM on a
      // runaway command; the truncation is silent like the webhook body caps).
      child.stdout.on("data", (d) => {
        if (outLen < maxBuffer) {
          stdout += d.toString("utf-8");
          outLen += d.length;
          if (outLen >= maxBuffer) stdout = stdout.slice(0, maxBuffer);
        }
      });
      child.stderr.on("data", (d) => {
        if (errLen < maxBuffer) {
          stderr += d.toString("utf-8");
          errLen += d.length;
          if (errLen >= maxBuffer) stderr = stderr.slice(0, maxBuffer);
        }
      });

      child.on("error", (err) => {
        // spawn failure (e.g. ENOENT) — surface as a non-zero-ish result rather
        // than throwing, so the seam never rejects.
        done({
          exitCode: null,
          signal: null,
          timedOut,
          stdout,
          stderr: stderr || String((err && err.message) || err),
        });
      });

      child.on("close", (code, sig) => {
        done({ exitCode: code, signal: sig, timedOut, stdout, stderr });
      });

      if (input != null) {
        try { child.stdin.write(input); } catch { /* stdin may be closed */ }
      }
      try { child.stdin.end(); } catch { /* noop */ }
    });
  }
}

// ── The one Azure-specific REST contract for ACA dynamic sessions ───────────
// Everything provider-specific about the ACA dynamic-sessions HTTP shape — the
// executions endpoint/path, the request body, and the response field mapping —
// lives in this single function so it is the ONE thing to reconcile against a
// live session pool. Every branch here is exercised offline through the injected
// transport in tests/sandbox/aca-job.test.mjs; no network, no Azure SDK, no live
// pool is touched.
//
// Shape reconciled to the documented Shell session-pool executions API
// (api-version 2025-10-02-preview, learn.microsoft.com/azure/container-apps/
// sessions-tutorial-shell + sessions-usage):
//   POST <pool>/executions?api-version=<v>&identifier=<sessionId>
//   body { codeInputType:"inline", executionType:"synchronous", shellCommand, timeoutInSeconds }
//   response { properties: { status:"Succeeded"|"Failed", stdout, stderr, exitCode? } }
// The `identifier` is a query parameter (not a body field); the token audience
// is https://dynamicsessions.io (see acaManagedIdentityTokenProvider).
//
// STILL UNVERIFIED against a live pool — the shape matches the docs but has not
// been round-tripped against a real session. Confirm the response mapping (the
// exact stdout/stderr/status field path) with one live spike before enabling
// aca-job in any environment.
function acaSessionExecContract(poolEndpoint, apiVersion, sessionId, command, timeoutSeconds) {
  const q = `api-version=${apiVersion}&identifier=${encodeURIComponent(sessionId)}`;
  return {
    url: `${poolEndpoint}/executions?${q}`,
    body: {
      codeInputType: "inline",
      executionType: "synchronous",
      shellCommand: command,
      timeoutInSeconds: timeoutSeconds,
    },
    mapResult(status, data) {
      // The executions response nests results under `properties`; fall back to
      // a flat body defensively so a shape drift degrades rather than throws.
      const p = (data && data.properties) || data || {};
      const exitCode =
        typeof p.exitCode === "number"
          ? p.exitCode
          : typeof p.status === "string"
            ? p.status.toLowerCase() === "succeeded"
              ? 0
              : 1
            : status === 200
              ? 0
              : 1;
      return { exitCode, stdout: p.stdout ?? "", stderr: p.stderr ?? "" };
    },
  };
}

// Optional managed-identity bearer-token provider for aca-job, matching the
// documented flow: an Entra token with audience https://dynamicsessions.io,
// obtained from the Azure Instance Metadata Service (IMDS) when running inside
// an Azure Container App with the identity assigned. Injected, never a default —
// AcaJobSandbox stays fail-closed until an operator wires this (or their own).
// `transport` is injectable so this is unit-testable offline.
function acaManagedIdentityTokenProvider(config = {}) {
  const resource = config.resource || "https://dynamicsessions.io";
  const imdsUrl =
    config.imdsUrl ||
    `http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=${encodeURIComponent(resource)}`;
  const transport = config.transport || ((url, init) => fetch(url, init));
  return async () => {
    const resp = await transport(imdsUrl, { headers: { Metadata: "true" } });
    const status = resp && typeof resp.status === "number" ? resp.status : 0;
    const data = resp && typeof resp.json === "function" ? await resp.json() : {};
    const token = data && (data.access_token || data.accessToken);
    if (status !== 200 || !token) {
      throw new Error(
        `aca-job managed-identity token fetch failed (status ${status})`,
      );
    }
    return token;
  };
}

/**
 * The `aca-job` provider: runs the command in an isolated Azure Container Apps
 * dynamic session instead of the current container. It implements the same
 * exec() contract as LocalSandbox by delegating to an INJECTED HTTP transport,
 * so every line of provider logic is unit-testable offline with a fake
 * transport. In production the transport defaults to global fetch and the token
 * to a managed-identity provider. Like LocalSandbox, exec() never rejects —
 * failures (including a transport/network error) come back in the result shape.
 *
 * Fail-closed: the constructor throws if `poolEndpoint` is missing, and the
 * default token provider throws until one is injected, so a half-configured
 * `aca-job` can never silently fall back to in-container exec.
 */
class AcaJobSandbox {
  constructor(config = {}) {
    this.provider = "aca-job";
    if (!config.poolEndpoint) {
      throw new Error(
        "aca-job sandbox requires config.poolEndpoint (the ACA session pool management endpoint)",
      );
    }
    this.poolEndpoint = config.poolEndpoint;
    this.apiVersion = config.apiVersion || "2025-10-02-preview";
    // Injected transport: (url, init) => Promise<{status:number, json():Promise<object>}>.
    // Defaults to global fetch in production; tests inject a fake so no network
    // is ever touched.
    this.transport = config.transport || ((url, init) => fetch(url, init));
    // Injected managed-identity bearer-token provider; tests inject a stub.
    // Fails closed (throws) if neither is configured — surfaced via exec()'s
    // result, never as an unhandled rejection.
    this.getToken =
      config.getToken ||
      (async () => {
        throw new Error(
          "aca-job sandbox: no managed-identity token provider configured",
        );
      });
    this.sessionId = config.sessionId || "sandbox";
    this.config = config;
  }

  /**
   * Same signature/return contract as LocalSandbox.exec. Provider-specific
   * request/response shaping is delegated to acaSessionExecContract() above.
   * @param {string} cmd
   * @param {string[]} args
   * @param {{timeoutMs?:number, signal?:AbortSignal}} opts
   * @returns {Promise<{exitCode:number|null, signal:string|null,
   *                    timedOut:boolean, stdout:string, stderr:string}>}
   */
  async exec(cmd, args = [], opts = {}) {
    const { timeoutMs, signal } = opts;
    const command = [cmd, ...(Array.isArray(args) ? args : [])].join(" ");
    // ACA caps a single execution at 220s; default to that when the caller
    // doesn't specify a timeout, otherwise convert the ms budget to seconds.
    const timeoutSeconds = timeoutMs ? Math.max(1, Math.ceil(timeoutMs / 1000)) : 220;
    const contract = acaSessionExecContract(
      this.poolEndpoint,
      this.apiVersion,
      this.sessionId,
      command,
      timeoutSeconds,
    );
    try {
      const token = await this.getToken();
      const init = {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(contract.body),
        signal,
      };
      const resp = await this.transport(contract.url, init);
      const status = resp && typeof resp.status === "number" ? resp.status : 0;
      const data =
        resp && typeof resp.json === "function" ? await resp.json() : {};
      return {
        ...contract.mapResult(status, data),
        signal: null,
        timedOut: false,
      };
    } catch (err) {
      // Never reject — mirror LocalSandbox's fail-into-result behavior.
      const aborted = Boolean(signal && signal.aborted);
      return {
        exitCode: null,
        signal: null,
        timedOut: Boolean(timeoutMs && aborted),
        stdout: "",
        stderr: String((err && err.message) || err),
      };
    }
  }
}

// Provider registry. Default is `local`; `aca-job` is registered but opt-in
// (SANDBOX_PROVIDER=aca-job). Asking for any other provider fails LOUD rather
// than silently falling back to in-container exec.
const PROVIDERS = {
  local: LocalSandbox,
  "aca-job": AcaJobSandbox,
};

/**
 * Construct a sandbox for the configured provider.
 * @param {string} [provider=process.env.SANDBOX_PROVIDER||"local"]
 * @param {object} [config]
 */
function createSandbox(provider = process.env.SANDBOX_PROVIDER || "local", config = {}) {
  const Ctor = PROVIDERS[provider];
  if (!Ctor) {
    throw new Error(
      `Unknown SANDBOX_PROVIDER: '${provider}' (known: ${Object.keys(PROVIDERS).join(", ")})`,
    );
  }
  return new Ctor(config);
}

export {
  createSandbox,
  LocalSandbox,
  AcaJobSandbox,
  PROVIDERS,
  acaManagedIdentityTokenProvider,
};
