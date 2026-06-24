/**
 * Sandbox execution seam — the provider-pluggable boundary for running an
 * agent's child process.
 *
 * This is the CONTRACT + the `local` adapter only. It is intentionally NOT yet
 * wired into the adapter spawn path (apps/paperclip/patch-adapter.mjs). With
 * nothing wired, this module is inert — importing it is side-effect-free and
 * changes no runtime behavior. Wiring it in, and adding an isolated `aca-job`
 * provider (ACA dynamic sessions), are follow-ons.
 *
 * The seam lets a task run either in-container (`local`, today's behavior) or,
 * later, in an isolated ephemeral sandbox without changing the caller.
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

// Provider registry. `aca-job` registers here later; until then asking
// for it fails LOUD rather than silently falling back to in-container exec.
const PROVIDERS = {
  local: LocalSandbox,
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

export { createSandbox, LocalSandbox, PROVIDERS };
