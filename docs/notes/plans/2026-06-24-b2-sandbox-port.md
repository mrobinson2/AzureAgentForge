# B2 — ACA Sandboxes Seam (port) Implementation Plan

> **Technical reference for contributors.** For the operational overview, start at [README](../../../README.md) or [Architecture](../../architecture.md).

> **For agentic workers:** REQUIRED SUB-SKILL: Use a subagent-driven development workflow (recommended) or a plan-execution workflow to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a provider-pluggable sandbox execution seam to AAF's paperclip — the contract plus a `local` adapter and a fail-closed provider factory — flag-off and unwired, with full unit tests, as the public mirror of the internally-shipped B2-1.

**Architecture:** One new ES module `apps/paperclip/sandbox.mjs` exporting `createSandbox()` (provider factory), `LocalSandbox` (a `child_process.spawn` wrapper that never rejects — failures come back in a result object), and a `PROVIDERS` registry containing only `local`. Asking for any other provider throws (fail-closed). The module is side-effect-free on import and is **not** wired into the adapter spawn path, so it changes no runtime behavior; wiring it in and adding an `aca-job` (ACA dynamic-sessions) provider are follow-ons.

**Tech Stack:** Node 22, ES modules, `node:test` built-in runner (zero dependencies), `node:child_process`.

---

## File Structure

- **Create** `apps/paperclip/sandbox.mjs` — the seam (contract + `local` adapter + factory).
- **Create** `tests/sandbox/sandbox.test.mjs` — `node:test` unit tests for the contract.
- **Modify** `.github/workflows/ci.yml` — run the new test file in the `node-tests` job.

AAF's paperclip JS lives in `apps/paperclip/` and its node tests in `tests/<area>/*.test.mjs` (e.g. `tests/auth-proxy/auth-proxy.test.mjs`), run by the `node-tests` CI job via `node --test`. This port follows that exact layout.

---

## Task 1: Port the sandbox seam unit tests (red)

**Files:**
- Create: `tests/sandbox/sandbox.test.mjs`

- [ ] **Step 1: Create the test file**

Create `tests/sandbox/sandbox.test.mjs`:

```javascript
/**
 * Sandbox seam unit tests (node:test, zero dependencies).
 *
 * Run:  node --test tests/sandbox/sandbox.test.mjs
 *
 * Covers the seam contract + `local` adapter: output capture, exit codes,
 * stderr, env, cwd, stdin, timeout, abort, output cap, and the fail-closed
 * provider factory. Pure/offline — uses `sh -c` (CI runs Ubuntu; dev runs
 * macOS, both have sh).
 */

import { test, describe } from "node:test";
import assert from "node:assert/strict";

const { createSandbox, LocalSandbox, PROVIDERS } = await import("../../apps/paperclip/sandbox.mjs");

describe("createSandbox factory", () => {
  test("defaults to the local provider", () => {
    const sb = createSandbox();
    assert.ok(sb instanceof LocalSandbox);
    assert.equal(sb.provider, "local");
  });

  test("returns a LocalSandbox for provider='local'", () => {
    assert.ok(createSandbox("local") instanceof LocalSandbox);
  });

  test("throws (fail closed) on an unknown provider", () => {
    assert.throws(() => createSandbox("aca-job"), /Unknown SANDBOX_PROVIDER/);
    assert.throws(() => createSandbox("e2b"), /Unknown SANDBOX_PROVIDER/);
  });

  test("the registry exposes only local for now", () => {
    assert.deepEqual(Object.keys(PROVIDERS), ["local"]);
  });
});

describe("LocalSandbox.exec — basic execution", () => {
  const sb = new LocalSandbox();

  test("captures stdout and a zero exit code", async () => {
    const r = await sb.exec("sh", ["-c", "printf hello"]);
    assert.equal(r.exitCode, 0);
    assert.equal(r.signal, null);
    assert.equal(r.timedOut, false);
    assert.equal(r.stdout, "hello");
    assert.equal(r.stderr, "");
  });

  test("propagates a non-zero exit code", async () => {
    const r = await sb.exec("sh", ["-c", "exit 3"]);
    assert.equal(r.exitCode, 3);
    assert.equal(r.timedOut, false);
  });

  test("captures stderr separately from stdout", async () => {
    const r = await sb.exec("sh", ["-c", "printf out; printf err 1>&2"]);
    assert.equal(r.exitCode, 0);
    assert.equal(r.stdout, "out");
    assert.equal(r.stderr, "err");
  });

  test("does not reject when the binary is missing (ENOENT → result, not throw)", async () => {
    const r = await sb.exec("this-binary-does-not-exist-xyz", []);
    assert.equal(r.exitCode, null);
    assert.match(r.stderr, /ENOENT|not.*found|spawn/i);
  });
});

describe("LocalSandbox.exec — env / cwd / stdin", () => {
  const sb = new LocalSandbox();

  test("passes a custom env", async () => {
    const r = await sb.exec("sh", ["-c", "printf %s \"$FOO\""], { env: { ...process.env, FOO: "bar" } });
    assert.equal(r.stdout, "bar");
  });

  test("honors cwd", async () => {
    const r = await sb.exec("sh", ["-c", "pwd"], { cwd: "/tmp" });
    // macOS reports /private/tmp for /tmp; accept either.
    assert.match(r.stdout.trim(), /(^|\/)tmp$/);
  });

  test("feeds stdin from opts.input", async () => {
    const r = await sb.exec("cat", [], { input: "piped-in" });
    assert.equal(r.exitCode, 0);
    assert.equal(r.stdout, "piped-in");
  });
});

describe("LocalSandbox.exec — timeout and abort", () => {
  const sb = new LocalSandbox();

  test("kills a command that exceeds timeoutMs and flags timedOut", async () => {
    const r = await sb.exec("sh", ["-c", "sleep 5"], { timeoutMs: 100 });
    assert.equal(r.timedOut, true);
    assert.equal(r.exitCode, null);
    assert.equal(r.signal, "SIGTERM");
  });

  test("a fast command under the timeout completes normally", async () => {
    const r = await sb.exec("sh", ["-c", "printf done"], { timeoutMs: 5000 });
    assert.equal(r.exitCode, 0);
    assert.equal(r.timedOut, false);
    assert.equal(r.stdout, "done");
  });

  test("an AbortSignal aborted mid-run kills the child", async () => {
    const ac = new AbortController();
    const p = sb.exec("sh", ["-c", "sleep 5"], { signal: ac.signal });
    setTimeout(() => ac.abort(), 50);
    const r = await p;
    assert.equal(r.exitCode, null);
    assert.equal(r.signal, "SIGTERM");
  });

  test("an already-aborted signal kills immediately", async () => {
    const r = await sb.exec("sh", ["-c", "sleep 5"], { signal: AbortSignal.abort() });
    assert.equal(r.exitCode, null);
    assert.equal(r.signal, "SIGTERM");
  });
});

describe("LocalSandbox.exec — output cap", () => {
  const sb = new LocalSandbox();

  test("truncates output at maxBuffer instead of growing unbounded", async () => {
    // Emit ~50KB but cap at 1KB.
    const r = await sb.exec("sh", ["-c", "for i in $(seq 1 5000); do printf 0123456789; done"], { maxBuffer: 1024 });
    assert.equal(r.exitCode, 0);
    assert.ok(r.stdout.length <= 1024, `stdout ${r.stdout.length} should be <= 1024`);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test tests/sandbox/sandbox.test.mjs`
Expected: FAIL — the dynamic `import("../../apps/paperclip/sandbox.mjs")` rejects with `ERR_MODULE_NOT_FOUND` (file doesn't exist yet).

- [ ] **Step 3: Commit the red test**

```bash
git add tests/sandbox/sandbox.test.mjs
git commit -m "test(paperclip): sandbox seam unit tests (red, B2)"
```

---

## Task 2: Port the sandbox seam module (green)

**Files:**
- Create: `apps/paperclip/sandbox.mjs`

- [ ] **Step 1: Create the module**

Create `apps/paperclip/sandbox.mjs` (header genericized for this repo — no internal feature references):

```javascript
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

// Provider registry. An `aca-job` provider registers here later; until then
// asking for it fails LOUD rather than silently falling back to in-container exec.
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
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `node --test tests/sandbox/sandbox.test.mjs`
Expected: PASS — all suites green (factory, basic execution, env/cwd/stdin, timeout/abort, output cap).

- [ ] **Step 3: Confirm the module is inert (no accidental wiring)**

Run: `grep -rn "sandbox.mjs\|createSandbox\|LocalSandbox" apps/paperclip/patch-adapter.mjs apps/paperclip/auth-proxy.mjs`
Expected: no matches — nothing imports the seam yet (it ships unwired by design).

- [ ] **Step 4: Commit**

```bash
git add apps/paperclip/sandbox.mjs
git commit -m "feat(paperclip): sandbox execution seam — local adapter + factory (B2, flag-off)"
```

---

## Task 3: Run the new tests in CI

**Files:**
- Modify: `.github/workflows/ci.yml` (the `node-tests` job)

- [ ] **Step 1: Add a CI step for the sandbox tests**

In `.github/workflows/ci.yml`, in the `node-tests` job, immediately after the existing auth-proxy step:

```yaml
      - name: auth-proxy unit tests (no deps; node built-in runner)
        run: node --test tests/auth-proxy/*.test.mjs
```

add:

```yaml
      - name: sandbox seam unit tests (no deps; node built-in runner)
        run: node --test tests/sandbox/*.test.mjs
```

- [ ] **Step 2: Validate the workflow locally (syntax)**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml')); print('ci.yml: valid YAML')"`
Expected: `ci.yml: valid YAML`

- [ ] **Step 3: Re-run the full node test set the way CI will**

Run: `node --test tests/auth-proxy/*.test.mjs tests/sandbox/*.test.mjs`
Expected: PASS — both suites green together.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run sandbox seam unit tests in the node-tests job (B2)"
```

---

## Self-Review

**Spec coverage** (against §2.2 of the v1.3 design):
- `apps/paperclip/sandbox.mjs` with provider factory + local adapter, unwired → Tasks 1–2. ✅
- Ported unit tests → Task 1. ✅
- Flag-off / fail-closed factory (`createSandbox` throws on unknown provider) → covered by `createSandbox` + its tests. ✅
- Seam-only, hot-path wiring (B2-2) and `aca-job` adapter (B2-3) stay future → enforced by Task 2 Step 3 (no imports). ✅
- CI runs the tests → Task 3. ✅

**Placeholder scan:** none — both files are complete; CI edit shows exact YAML.

**Type consistency:** the result shape `{exitCode, signal, timedOut, stdout, stderr}` is identical across the module (`LocalSandbox.exec` resolutions) and the test assertions. Exports (`createSandbox`, `LocalSandbox`, `PROVIDERS`) match the test's destructured import.

**Public-repo hygiene:** header comment genericized — no internal feature/spec names; module body is generic Node with no internal hostnames. Run `scripts/scan-internal-refs.sh` after staging to confirm.
