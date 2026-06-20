// Unit tests for the auth-proxy security guards (JWT verification, scope
// checks, path-traversal containment, frontmatter parsing). Zero dependencies —
// Node's built-in test runner. Run: node --test tests/auth-proxy/
//
// auth-proxy.mjs only binds a port under isMainModule, so importing it here is
// side-effect-free.
import test from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { resolve } from "node:path";

// JWT_ISSUER / JWT_AUDIENCE are read at module-import time; pin them so token
// fixtures are deterministic regardless of the ambient environment.
process.env.PAPERCLIP_AUTOMATION_JWT_ISSUER = "test-issuer";
process.env.PAPERCLIP_AUTOMATION_JWT_AUDIENCE = "test-audience";

const { verifyJwt, checkScope, governorTargetPath, safePath, parseFrontmatter, stripBom } =
  await import("../../apps/paperclip/auth-proxy.mjs");

// ── helpers ──────────────────────────────────────────────────────────────────
const b64url = (s) => Buffer.from(s).toString("base64url");
function makeJwt(payload, secret, { alg = "HS256" } = {}) {
  const h = b64url(JSON.stringify({ alg, typ: "JWT" }));
  const p = b64url(JSON.stringify(payload));
  const sig = createHmac("sha256", secret).update(`${h}.${p}`).digest("base64url");
  return `${h}.${p}.${sig}`;
}
const SECRET = "test-secret";
const claims = { sub: "svc", iss: "test-issuer", aud: "test-audience" };

// ── verifyJwt ────────────────────────────────────────────────────────────────
test("verifyJwt accepts a valid token and returns claims", () => {
  const p = verifyJwt(makeJwt({ ...claims, scope: ["skills:read"] }, SECRET), SECRET);
  assert.equal(p.sub, "svc");
  assert.deepEqual(p.scope, ["skills:read"]);
});
test("verifyJwt rejects a forged signature", () => {
  assert.throws(() => verifyJwt(makeJwt(claims, SECRET), "wrong-secret"), /Invalid signature/);
});
test("verifyJwt rejects a non-HS256 algorithm (alg confusion)", () => {
  assert.throws(() => verifyJwt(makeJwt(claims, SECRET, { alg: "none" }), SECRET), /Unsupported algorithm/);
});
test("verifyJwt rejects a malformed token", () => {
  assert.throws(() => verifyJwt("a.b", SECRET), /Malformed JWT/);
});
test("verifyJwt rejects an expired token", () => {
  assert.throws(() => verifyJwt(makeJwt({ ...claims, exp: 1 }, SECRET), SECRET), /expired/);
});
test("verifyJwt rejects a not-yet-valid token (nbf)", () => {
  assert.throws(() => verifyJwt(makeJwt({ ...claims, nbf: 9999999999 }, SECRET), SECRET), /not yet valid/);
});
test("verifyJwt rejects the wrong issuer", () => {
  assert.throws(() => verifyJwt(makeJwt({ ...claims, iss: "evil" }, SECRET), SECRET), /Invalid issuer/);
});
test("verifyJwt rejects the wrong audience", () => {
  assert.throws(() => verifyJwt(makeJwt({ ...claims, aud: "evil" }, SECRET), SECRET), /Invalid audience/);
});

// ── checkScope ───────────────────────────────────────────────────────────────
test("checkScope: null scopes (admin role) bypasses all checks", () => {
  assert.equal(checkScope("POST", "/api/memory/x", null), true);
});
test("checkScope: a matching scope is allowed", () => {
  assert.equal(checkScope("GET", "/api/memory", ["memory:admin"]), true);
});
test("checkScope: a missing scope is denied", () => {
  assert.equal(checkScope("GET", "/api/memory", ["skills:read"]), false);
});
test("checkScope: the wildcard scope is allowed", () => {
  assert.equal(checkScope("DELETE", "/api/memory/x", ["*"]), true);
});
test("checkScope: path wildcard matches exactly one segment", () => {
  assert.equal(checkScope("POST", "/api/memory/abc", ["memory:admin"]), true);
  // /api/memory/abc/def matches no pattern -> needs the "*" scope, which is absent
  assert.equal(checkScope("POST", "/api/memory/abc/def", ["memory:admin"]), false);
});

// ── governorTargetPath (memory-governor passthrough hardening) ───────────────
test("governorTargetPath allows the documented surface", () => {
  assert.equal(governorTargetPath("/api/memory"), "/memory");
  assert.equal(governorTargetPath("/api/memory/recall?q=x"), "/memory/recall?q=x");
  assert.equal(governorTargetPath("/api/digest"), "/digest");
  assert.equal(governorTargetPath("/api/digest/today"), "/digest/today");
});
test("governorTargetPath rejects raw path traversal", () => {
  assert.equal(governorTargetPath("/api/memory/../admit"), null);
  assert.equal(governorTargetPath("/api/memory/../../healthz"), null);
});
test("governorTargetPath rejects percent-encoded traversal", () => {
  assert.equal(governorTargetPath("/api/memory/..%2fadmit"), null);
  assert.equal(governorTargetPath("/api/memory/%2e%2e/admit"), null);
});
test("governorTargetPath rejects malformed percent-encoding", () => {
  assert.equal(governorTargetPath("/api/memory/%zz"), null);
});
test("governorTargetPath rejects routes outside /memory and /digest", () => {
  assert.equal(governorTargetPath("/api/memoryfoo"), null);
  assert.equal(governorTargetPath("/api/admit"), null);
});
test("governorTargetPath forwards in-bounds dot-segments unchanged", () => {
  assert.equal(governorTargetPath("/api/memory/x/../y"), "/memory/x/../y");
});

// ── safePath (path-traversal containment) ────────────────────────────────────
test("safePath allows an in-jail path", () => {
  assert.equal(safePath("/srv/skills", "foo", "SKILL.md"), resolve("/srv/skills/foo/SKILL.md"));
});
test("safePath rejects parent traversal", () => {
  assert.equal(safePath("/srv/skills", "../etc/passwd"), null);
});
test("safePath rejects a sibling-prefix escape (../skills-evil)", () => {
  assert.equal(safePath("/srv/skills", "../skills-evil"), null);
});
test("safePath allows the base itself", () => {
  assert.equal(safePath("/srv/skills"), resolve("/srv/skills"));
});

// ── stripBom ─────────────────────────────────────────────────────────────────
test("stripBom removes a leading BOM and is a no-op otherwise", () => {
  assert.equal(stripBom("﻿hi"), "hi");
  assert.equal(stripBom("hi"), "hi");
});

// ── parseFrontmatter ─────────────────────────────────────────────────────────
test("parseFrontmatter parses scalars and bracket arrays", () => {
  const fm = parseFrontmatter("---\nname: foo\ntags: [a, b, c]\n---\nbody");
  assert.equal(fm.name, "foo");
  assert.deepEqual(fm.tags, ["a", "b", "c"]);
});
test("parseFrontmatter returns {} when no frontmatter is present", () => {
  assert.deepEqual(parseFrontmatter("just a body"), {});
});
