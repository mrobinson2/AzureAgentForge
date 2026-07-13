# ACA Sandbox Finish — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the shipped-but-inert sandbox seam into a real isolated-execution feature by adding an `aca-job` provider (Azure Container Apps dynamic sessions) and wiring the seam into the adapter spawn path behind a default-`local` flag.

**Architecture:** The seam (`apps/paperclip/sandbox.mjs`) already defines the contract, the `local` adapter, and a fail-closed `createSandbox` factory with a `PROVIDERS` registry. We add an `AcaJobSandbox` class that implements the same `exec()` contract by delegating to an **injected HTTP transport** (so all provider logic is unit-testable offline); the single real Azure REST call is isolated in one small function. Wiring into the upstream adapter's `execute.js` is a build-time string patch in `patch-adapter.mjs`, gated on `SANDBOX_PROVIDER` so the default path is unchanged.

**Tech Stack:** Node.js ESM (`.mjs`), `node:test`, ACA dynamic sessions REST API, managed-identity bearer auth.

---

### Task 0: Spike the ACA dynamic-sessions exec API (de-risk before coding)

**Why:** The `aca-job` provider's correctness depends on the exact ACA dynamic-sessions request/response shape, which is not verified in this repo. Spike it like the Voice Live STT spike — prove the call before writing the provider.

**Files:**
- Create: `docs/superpowers/spikes/aca-sandbox/spike.mjs`
- Create: `docs/superpowers/spikes/aca-sandbox/FINDINGS.md`

- [ ] **Step 1:** Write `spike.mjs` that, given `ACA_SESSION_POOL_ENDPOINT` + a bearer token, (a) creates/targets a session by identifier, (b) executes `echo hello` (or a custom-container command), (c) prints the full response JSON and the mapped `{exitCode, stdout, stderr}`.
- [ ] **Step 2:** Run against a real ACA session pool (operator-provided). Capture: the exact execute endpoint + `api-version`, the request body schema, the response field names for stdout/stderr/exit status, and round-trip latency.
- [ ] **Step 3:** Write `FINDINGS.md` with the verified endpoint, payload, response mapping, and auth (MI scope). **This file is the source of truth for Task 2's transport.**
- [ ] **Step 4:** Commit.

```bash
git add docs/superpowers/spikes/aca-sandbox/
git commit -m "spike(sandbox): verify ACA dynamic-sessions exec API shape"
```

> If a live pool isn't available this weekend, STOP after Task 1 and ship Tasks 3–4 as the provider scaffold with the transport behind a clearly-marked, unit-tested seam; mark the live REST mapping as the one unverified line and do not enable `aca-job` in any environment until the spike runs.

---

### Task 1: `AcaJobSandbox` class + fail-closed config, registered in the factory

**Files:**
- Modify: `apps/paperclip/sandbox.mjs` (add class + registry entry)
- Test: `tests/sandbox/aca-job.test.mjs` (new)

- [ ] **Step 1: Write the failing test**

```javascript
import { test, describe } from "node:test";
import assert from "node:assert/strict";
const { createSandbox, AcaJobSandbox, PROVIDERS } = await import("../../apps/paperclip/sandbox.mjs");

describe("aca-job registration", () => {
  test("factory returns AcaJobSandbox for provider='aca-job'", () => {
    const sb = createSandbox("aca-job", { poolEndpoint: "https://pool.example", transport: async () => ({}) });
    assert.ok(sb instanceof AcaJobSandbox);
    assert.equal(sb.provider, "aca-job");
  });
  test("throws (fail closed) when poolEndpoint is missing", () => {
    assert.throws(() => createSandbox("aca-job", {}), /poolEndpoint/);
  });
  test("registry now exposes local and aca-job", () => {
    assert.deepEqual(Object.keys(PROVIDERS).sort(), ["aca-job", "local"]);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tests/sandbox/aca-job.test.mjs`
Expected: FAIL — `AcaJobSandbox` is not exported / `aca-job` not in registry.

- [ ] **Step 3: Write minimal implementation** (in `apps/paperclip/sandbox.mjs`)

```javascript
class AcaJobSandbox {
  constructor(config = {}) {
    this.provider = "aca-job";
    if (!config.poolEndpoint) {
      throw new Error("aca-job sandbox requires config.poolEndpoint (the ACA session pool management endpoint)");
    }
    this.poolEndpoint = config.poolEndpoint;
    this.apiVersion = config.apiVersion || "2024-10-02-preview";
    // Injected transport: (url, {method, headers, body}) => Promise<{status, json()}>
    // Defaults to global fetch in production; tests inject a fake.
    this.transport = config.transport || ((url, init) => fetch(url, init));
    // Injected token provider for managed-identity auth; tests inject a stub.
    this.getToken = config.getToken || (async () => { throw new Error("aca-job: no token provider configured"); });
    this.sessionId = config.sessionId || "paperclip";
  }
  // exec() added in Task 2.
}
```

Then register it:

```javascript
const PROVIDERS = {
  local: LocalSandbox,
  "aca-job": AcaJobSandbox,
};
```

And extend the export:

```javascript
export { createSandbox, LocalSandbox, AcaJobSandbox, PROVIDERS };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test tests/sandbox/aca-job.test.mjs`
Expected: PASS (3 tests).

- [ ] **Step 5: Update the existing registry test that asserts local-only**

In `tests/sandbox/sandbox.test.mjs`, change:

```javascript
test("the registry exposes only local for now", () => {
  assert.deepEqual(Object.keys(PROVIDERS), ["local"]);
});
```
to:
```javascript
test("the registry exposes local and aca-job", () => {
  assert.deepEqual(Object.keys(PROVIDERS).sort(), ["aca-job", "local"]);
});
```
And remove `aca-job` from the "throws on unknown provider" test (keep `e2b`):
```javascript
test("throws (fail closed) on an unknown provider", () => {
  assert.throws(() => createSandbox("e2b"), /Unknown SANDBOX_PROVIDER/);
});
```

- [ ] **Step 6: Run the full sandbox suite + commit**

Run: `node --test tests/sandbox/`
Expected: PASS (existing 16 minus the moved assertion, plus new aca-job tests).

```bash
git add apps/paperclip/sandbox.mjs tests/sandbox/
git commit -m "feat(sandbox): register fail-closed aca-job provider"
```

---

### Task 2: `AcaJobSandbox.exec()` — delegate to the injected transport, map to the result shape

**Files:**
- Modify: `apps/paperclip/sandbox.mjs`
- Test: `tests/sandbox/aca-job.test.mjs`

> Use the response field mapping verified in Task 0 `FINDINGS.md`. The body/endpoint below reflects the documented ACA dynamic-sessions shape; reconcile with FINDINGS before enabling live.

- [ ] **Step 1: Write the failing test** (fake transport, no network)

```javascript
describe("AcaJobSandbox.exec", () => {
  function fakeTransport(captured) {
    return async (url, init) => {
      captured.url = url; captured.init = init;
      return { status: 200, json: async () => ({ stdout: "hello\n", stderr: "", exitCode: 0 }) };
    };
  }
  test("posts to the pool execute endpoint and maps the result shape", async () => {
    const captured = {};
    const sb = createSandbox("aca-job", {
      poolEndpoint: "https://pool.example",
      transport: fakeTransport(captured),
      getToken: async () => "tok-123",
    });
    const res = await sb.exec("echo", ["hello"], { timeoutMs: 5000 });
    assert.deepEqual(res, { exitCode: 0, signal: null, timedOut: false, stdout: "hello\n", stderr: "" });
    assert.match(captured.url, /\/code\/execute\?api-version=/);
    assert.equal(captured.init.headers.Authorization, "Bearer tok-123");
    assert.match(captured.init.headers["Content-Type"], /application\/json/);
  });
  test("never rejects — transport error becomes a result", async () => {
    const sb = createSandbox("aca-job", {
      poolEndpoint: "https://pool.example",
      transport: async () => { throw new Error("network down"); },
      getToken: async () => "tok",
    });
    const res = await sb.exec("echo", ["x"]);
    assert.equal(res.exitCode, null);
    assert.match(res.stderr, /network down/);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tests/sandbox/aca-job.test.mjs`
Expected: FAIL — `exec is not a function`.

- [ ] **Step 3: Write minimal implementation**

```javascript
async exec(cmd, args = [], opts = {}) {
  const { timeoutMs, signal } = opts;
  const url = `${this.poolEndpoint}/code/execute?api-version=${this.apiVersion}`;
  const command = [cmd, ...(Array.isArray(args) ? args : [])].join(" ");
  try {
    const token = await this.getToken();
    const init = {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ identifier: this.sessionId, codeInputType: "inline", command }),
      signal,
    };
    const resp = await this.transport(url, init);
    const data = await resp.json();
    // Field mapping per Task 0 FINDINGS.md:
    return {
      exitCode: typeof data.exitCode === "number" ? data.exitCode : (resp.status === 200 ? 0 : 1),
      signal: null,
      timedOut: false,
      stdout: data.stdout ?? "",
      stderr: data.stderr ?? "",
    };
  } catch (err) {
    const aborted = signal && signal.aborted;
    return {
      exitCode: null, signal: null, timedOut: Boolean(timeoutMs && aborted),
      stdout: "", stderr: String((err && err.message) || err),
    };
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test tests/sandbox/aca-job.test.mjs`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/paperclip/sandbox.mjs tests/sandbox/aca-job.test.mjs
git commit -m "feat(sandbox): aca-job exec maps ACA dynamic-sessions to the seam result shape"
```

---

### Task 3: Guarded wiring into the adapter spawn path (build-time, default `local`)

**Files:**
- Modify: `apps/paperclip/patch-adapter.mjs`
- Test: `tests/sandbox/wiring-transform.test.mjs` (new — tests the string transform, offline)

**Approach:** Do NOT change runtime behavior by default. Add a build-time injection in `patch-adapter.mjs` that, only when the upstream `execute.js` spawn call is found, routes it through `createSandbox(process.env.SANDBOX_PROVIDER)` — which returns `LocalSandbox` (today's exact behavior) unless `SANDBOX_PROVIDER=aca-job`. Because the real spawn lives in the vendored adapter, the patch is a guarded string replacement that logs LOUD and no-ops if the anchor isn't found (consistent with the existing `[patch-adapter]` warnings).

- [ ] **Step 1: Write the failing test** for a pure transform function `injectSandboxSeam(src)` exported from a new small module `apps/paperclip/patch-adapter-sandbox.mjs`:

```javascript
import { test, describe } from "node:test";
import assert from "node:assert/strict";
const { injectSandboxSeam } = await import("../../apps/paperclip/patch-adapter-sandbox.mjs");

describe("injectSandboxSeam", () => {
  test("returns applied=false and unchanged src when anchor is absent", () => {
    const r = injectSandboxSeam("no spawn here");
    assert.equal(r.applied, false);
    assert.equal(r.src, "no spawn here");
  });
  test("is idempotent — second pass does not double-inject", () => {
    const withAnchor = 'const child = spawn(bin, argv, spawnOpts);';
    const once = injectSandboxSeam(withAnchor);
    assert.equal(once.applied, true);
    const twice = injectSandboxSeam(once.src);
    assert.equal(twice.applied, false);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tests/sandbox/wiring-transform.test.mjs`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation** (`apps/paperclip/patch-adapter-sandbox.mjs`)

```javascript
// Pure, unit-tested transform. Anchor MUST be reconciled with the real
// execute.js spawn line at integration time (Task 4); the anchor string here
// is the documented default and is verified live in Task 4.
const ANCHOR = "const child = spawn(bin, argv, spawnOpts);";
const MARK = "/* AAF sandbox seam */";

export function injectSandboxSeam(src) {
  if (src.includes(MARK) || !src.includes(ANCHOR)) return { src, applied: false };
  const replacement =
    `${MARK} const __sb = (await import('/server-prod/sandbox.mjs')).createSandbox();\n` +
    `const child = __sb.provider === 'local' ? spawn(bin, argv, spawnOpts) : __sb;`;
  return { src: src.replace(ANCHOR, replacement), applied: true };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test tests/sandbox/wiring-transform.test.mjs`
Expected: PASS.

- [ ] **Step 5: Call it from `patch-adapter.mjs`** (after the existing execute.js edits, before `writeFileSync(executePath, execute)`):

```javascript
import { injectSandboxSeam } from "./patch-adapter-sandbox.mjs";
// ... after other execute.js transforms:
const _sb = injectSandboxSeam(execute);
if (_sb.applied) { execute = _sb.src; console.log("[patch-adapter] Sandbox seam wired (default local)"); }
else { console.warn("[patch-adapter] WARN: sandbox spawn anchor not found — seam NOT wired (default behavior unchanged)"); }
```

- [ ] **Step 6: Commit**

```bash
git add apps/paperclip/patch-adapter-sandbox.mjs apps/paperclip/patch-adapter.mjs tests/sandbox/wiring-transform.test.mjs
git commit -m "feat(sandbox): guarded build-time wiring of the seam (default local)"
```

- [ ] **Step 7 (integration, build-time):** During the next image build, confirm the `[patch-adapter] Sandbox seam wired` log appears. If the WARN appears instead, read the real `execute.js` spawn line, update `ANCHOR`, re-run the transform test, rebuild. Do not enable `aca-job` until the wired log is confirmed AND the Task 0 spike passed.

---

### Task 4: Terraform var + docs

**Files:**
- Modify: `infrastructure/modules/container-apps/variables.tf` (+ `paperclip.tf` env)
- Modify: `docs/architecture.md` (sandbox section), `ROADMAP.md` (move sandbox from "follow-on" to shipped)

- [ ] **Step 1:** Add a `sandbox_provider` variable (default `"local"`) and a `SANDBOX_PROVIDER` env on the paperclip container app, plus the session-pool endpoint var, all `count`/condition-gated so default deploys are unchanged. Mirror the existing `telegram_enabled`-style gating in `variables.tf`.
- [ ] **Step 2:** Run `terraform validate` in `infrastructure/environments/dev`. Expected: clean.
- [ ] **Step 3:** Update docs: the sandbox is now a pluggable provider (`local` default, `aca-job` for isolation); note enablement is operator opt-in after the spike.
- [ ] **Step 4: Commit.**

```bash
git add infrastructure/ docs/ ROADMAP.md
git commit -m "feat(sandbox): sandbox_provider var + docs; mark aca-job shipped (flag-gated)"
```

---

## Self-Review

- **Spec coverage:** aca-job provider (Tasks 1–2 ✓), wiring into spawn path (Task 3 ✓, guarded + build-time-verified), default-unchanged behavior (✓ via `SANDBOX_PROVIDER` default `local`), tests offline (✓ injected transport + pure transform), Terraform/docs (Task 4 ✓). The unproven external API is isolated and spiked first (Task 0 ✓).
- **Placeholder scan:** the one inherently-unverifiable element (live ACA REST field names + the real execute.js spawn anchor) is dependency-injected and gated behind the Task 0 spike / Task 3 Step 7 integration check, not left as a TODO in shipped code.
- **Type consistency:** `exec()` returns the seam's `{exitCode, signal, timedOut, stdout, stderr}` shape everywhere; `createSandbox(provider, config)` signature matches the existing factory; `transport(url, init) => {status, json()}` is consistent across Tasks 1–2.

**Risk:** Task 3 (wiring) is the only upstream-coupled, build-time-verified step. If the weekend runs short, Tasks 0–2 + 4 ship the `aca-job` provider as a real, tested, flag-gated capability; Task 3 wiring can follow without blocking the release.
