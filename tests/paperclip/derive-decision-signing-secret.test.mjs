/**
 * derive-decision-signing-secret.py contract (node:test, zero deps).
 * Run: node --test tests/paperclip/derive-decision-signing-secret.test.mjs
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHmac } from "node:crypto";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), "../../apps/paperclip/derive-decision-signing-secret.py");
const run = (env) => spawnSync("python3", [SCRIPT], { env: { PATH: process.env.PATH, ...env }, encoding: "utf-8" });
const ROOT = "r".repeat(40);

describe("derive-decision-signing-secret.py", () => {
  test("derives HMAC-SHA256(root, label) as 64 hex chars, deterministic", () => {
    const a = run({ BETTER_AUTH_SECRET: ROOT });
    const b = run({ BETTER_AUTH_SECRET: ROOT });
    assert.equal(a.status, 0, a.stderr);
    assert.match(a.stdout, /^[0-9a-f]{64}$/);
    assert.equal(a.stdout, b.stdout);
    const expected = createHmac("sha256", ROOT).update("aaf:paperclip:decision-signing:v1").digest("hex");
    assert.equal(a.stdout, expected);
  });
  test("different roots give different keys and the root never appears in the output", () => {
    const a = run({ BETTER_AUTH_SECRET: ROOT });
    const b = run({ BETTER_AUTH_SECRET: "s".repeat(40) });
    assert.notEqual(a.stdout, b.stdout);
    assert.ok(!a.stdout.includes(ROOT));
  });
  test("fails closed when the root is unset, placeholder, or too short", () => {
    assert.equal(run({}).status, 1);
    assert.equal(run({ BETTER_AUTH_SECRET: "__unset__" }).status, 1);
    assert.equal(run({ BETTER_AUTH_SECRET: "short" }).status, 1);
  });
  test("ROOT_SECRET_ENV selects another root variable", () => {
    const r = run({ ROOT_SECRET_ENV: "OTHER_ROOT", OTHER_ROOT: ROOT });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /^[0-9a-f]{64}$/);
  });
});
