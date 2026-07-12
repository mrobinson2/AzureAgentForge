/**
 * Provider-pin transform unit tests (node:test, zero dependencies).
 *
 * Run:  node --test tests/paperclip/provider-transform.test.mjs
 *
 * Tests the pure build-time string transform that forces `--provider custom`
 * in the hermes-paperclip adapter's spawn args — in both the npm dist shape
 * (execute.js) and the vendored workspace TypeScript shape (execute.ts).
 * Fully offline — no adapter, no build, no filesystem.
 */

import { test, describe } from "node:test";
import assert from "node:assert/strict";

const { forceProviderCustom, PROVIDER_MARKER } = await import(
  "../../apps/paperclip/patch-adapter-provider.mjs"
);

// Compiled npm dist shape (multi-line, 4-space indentation).
const DIST_SHAPE = [
  "    const resolvedProvider = hint ?? \"auto\";",
  "    if (resolvedProvider !== \"auto\") {",
  "        args.push(\"--provider\", resolvedProvider);",
  "    }",
  "    if (ctx.runId)",
].join("\n");

// Workspace TypeScript shape (single line, different whitespace).
const WS_TS_SHAPE =
  'const resolvedProvider: string = hint ?? "auto";\n' +
  'if (resolvedProvider !== "auto") { args.push("--provider", resolvedProvider); }\n';

describe("forceProviderCustom", () => {
  test("pins --provider custom in the npm dist shape", () => {
    const r = forceProviderCustom(DIST_SHAPE);
    assert.equal(r.applied, true);
    assert.equal(r.alreadyPatched, false);
    assert.match(r.src, /args\.push\("--provider", "custom"\)/);
    assert.ok(r.src.includes(PROVIDER_MARKER), "idempotency marker embedded");
    // Surrounding code is untouched.
    assert.match(r.src, /if \(ctx\.runId\)/);
  });

  test("pins --provider custom in the workspace TypeScript shape", () => {
    const r = forceProviderCustom(WS_TS_SHAPE);
    assert.equal(r.applied, true);
    assert.match(r.src, /args\.push\("--provider", "custom"\)/);
    assert.ok(r.src.includes(PROVIDER_MARKER));
  });

  test("is idempotent — second pass reports alreadyPatched and does not change src", () => {
    const once = forceProviderCustom(DIST_SHAPE);
    assert.equal(once.applied, true);
    const twice = forceProviderCustom(once.src);
    assert.equal(twice.applied, false);
    assert.equal(twice.alreadyPatched, true);
    assert.equal(twice.src, once.src);
  });

  test("reports applied=false alreadyPatched=false when the anchor is absent (caller must fail loud)", () => {
    const src = "const args = []; // no provider push here";
    const r = forceProviderCustom(src);
    assert.equal(r.applied, false);
    assert.equal(r.alreadyPatched, false);
    assert.equal(r.src, src);
  });

  test("does not force custom on an unrelated conditional", () => {
    const src = 'if (mode !== "auto") { args.push("--mode", mode); }';
    const r = forceProviderCustom(src);
    assert.equal(r.applied, false);
    assert.equal(r.src, src);
  });
});
