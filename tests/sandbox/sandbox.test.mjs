/**
 * Sandbox seam unit tests (node:test, zero dependencies).
 *
 * Run:  node --test tests/sandbox/sandbox.test.mjs
 *
 * Covers the seam contract + `local` adapter: output capture, exit codes,
 * stderr, env, cwd, stdin, timeout, abort, and the fail-closed provider factory.
 * Pure/offline — uses `sh -c` (CI runs Ubuntu; dev runs macOS, both have sh).
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
    // killed by SIGTERM → exitCode null, signal SIGTERM (platform-dependent but
    // both Linux and macOS report this for a sh -c sleep killed mid-flight).
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
