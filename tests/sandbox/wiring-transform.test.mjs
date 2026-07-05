/**
 * Sandbox seam wiring-transform unit tests (node:test, zero dependencies).
 *
 * Run:  node --test tests/sandbox/wiring-transform.test.mjs
 *
 * Tests the pure build-time string transform that wires the sandbox seam into
 * the adapter's execute.js spawn path. Fully offline — no adapter, no build,
 * no filesystem.
 */

import { test, describe } from "node:test";
import assert from "node:assert/strict";

const { injectSandboxSeam } = await import(
  "../../apps/paperclip/patch-adapter-sandbox.mjs"
);

describe("injectSandboxSeam", () => {
  test("returns applied=false and unchanged src when anchor is absent", () => {
    const r = injectSandboxSeam("no spawn here");
    assert.equal(r.applied, false);
    assert.equal(r.src, "no spawn here");
  });

  test("wires the seam when the spawn anchor is present", () => {
    const withAnchor = "const child = spawn(bin, argv, spawnOpts);";
    const r = injectSandboxSeam(withAnchor);
    assert.equal(r.applied, true);
    // The seam marker and the SANDBOX_PROVIDER-gated dispatch are present.
    assert.match(r.src, /AAF sandbox seam/);
    assert.match(r.src, /createSandbox\(\)/);
    assert.match(r.src, /__sb\.provider === 'local'/);
    // The original spawn call is preserved on the local branch.
    assert.match(r.src, /spawn\(bin, argv, spawnOpts\)/);
  });

  test("is idempotent — second pass does not double-inject", () => {
    const withAnchor = "const child = spawn(bin, argv, spawnOpts);";
    const once = injectSandboxSeam(withAnchor);
    assert.equal(once.applied, true);
    const twice = injectSandboxSeam(once.src);
    assert.equal(twice.applied, false);
    assert.equal(twice.src, once.src);
  });
});
