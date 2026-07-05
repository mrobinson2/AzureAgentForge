/**
 * tenant-console control-plane auth-proxy seams (node:test, zero dependencies).
 *
 * REFERENCE SCAFFOLDING: this test exercises auth-proxy seams that are not
 * vendored into this experimental directory, so it will not run as-is (the
 * dynamic import below points at a control-plane auth-proxy that lives in the
 * single-tenant stack). It is kept to document the expected contract.
 *
 * Run (when wired against a real auth-proxy):  node --test tenant-governor.test.mjs
 *
 * Covers:
 *  - governor passthrough root-endpoint mappings: POST /api/memory/admit →
 *    {governor}/admit and POST /api/memory/workspace-expire →
 *    {governor}/workspace-expire (asserted against a live stub governor —
 *    path, x-governor-key, body relay, response relay);
 *  - regression: the existing /api/memory* list/action + /api/digest flows
 *    still strip only the /api prefix;
 *  - SCOPE_MAP: memory:admin gates the passthrough; the native
 *    instructions routes require agents:read / agents:write.
 *
 * The proxy reads env at import time, so the stub governor is started FIRST
 * and its address exported via GOVERNOR_BASE_URL before the dynamic import
 * (same pattern as the sibling test files).
 */

import { test, describe, before, after } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { createServer, request as httpRequest } from "node:http";

// ── Stub governor (must be listening before the proxy import reads env) ─────
const govRequests = [];
const govServer = createServer((req, res) => {
  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", () => {
    govRequests.push({ method: req.method, url: req.url, headers: req.headers, body });
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true, path: req.url }));
  });
});
await new Promise((resolve) => govServer.listen(0, "127.0.0.1", resolve));

// ── Test env (must precede the proxy import) ─────────────────────────────────
const SECRET = "test-secret-please-ignore";
process.env.PAPERCLIP_AUTOMATION_JWT_SECRET = SECRET;
process.env.PAPERCLIP_AUTOMATION_JWT_ISSUER = "aaf-control-plane";
process.env.PAPERCLIP_AUTOMATION_JWT_AUDIENCE = "paperclip-api";
process.env.PAPERCLIP_INTERNAL_PORT = "39999"; // nothing listening — backend calls fail fast
process.env.GOVERNOR_BASE_URL = `http://127.0.0.1:${govServer.address().port}`;
process.env.GOVERNOR_API_KEY = "governor-key-test";

// Reference path — the control-plane auth-proxy is not vendored in this dir.
// If it is absent, skip cleanly instead of erroring with module-not-found, so
// `node --test` over the experimental tree reports a skip rather than a failure.
let proxy;
try {
  proxy = await import("../../control-plane/auth-proxy.mjs");
} catch {
  await new Promise((r) => govServer.close(r));
  test("tenant-governor auth-proxy (reference — control-plane not vendored here)", { skip: true }, () => {});
}

if (proxy) {

// ── Helpers ──────────────────────────────────────────────────────────────────

function b64url(obj) {
  return Buffer.from(JSON.stringify(obj)).toString("base64url");
}

function makeJwt(payload, { secret = SECRET, alg = "HS256" } = {}) {
  const header = b64url({ alg, typ: "JWT" });
  const body = b64url(payload);
  const sig = createHmac("sha256", secret).update(`${header}.${body}`).digest("base64url");
  return `${header}.${body}.${sig}`;
}

const NOW = Math.floor(Date.now() / 1000);
const BASE_CLAIMS = {
  iss: "aaf-control-plane",
  aud: "paperclip-api",
  sub: "test-suite",
  role: "automation",
  exp: NOW + 3600,
};
const memoryAdminJwt = () => makeJwt({ ...BASE_CLAIMS, scope: ["memory:admin"] });
const scopedJwt = (...scope) => makeJwt({ ...BASE_CLAIMS, scope });

let PORT;
before(async () => {
  await new Promise((resolve) => {
    proxy.server.listen(0, "127.0.0.1", () => {
      PORT = proxy.server.address().port;
      resolve();
    });
  });
});
after(() => {
  proxy.server.close();
  govServer.close();
});

function req(path, { method = "GET", token = null, body = null } = {}) {
  return new Promise((resolve, reject) => {
    const headers = { "content-type": "application/json" };
    if (token) headers.authorization = `Bearer ${token}`;
    const r = httpRequest(
      { host: "127.0.0.1", port: PORT, path, method, headers },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          let json = null;
          try { json = JSON.parse(data); } catch { /* non-JSON */ }
          resolve({ status: res.statusCode, json, text: data });
        });
      },
    );
    r.on("error", reject);
    if (body !== null) r.write(typeof body === "string" ? body : JSON.stringify(body));
    r.end();
  });
}

// ── Governor passthrough — tenant root-endpoint mappings ─────────────────────

describe("governor passthrough — tenant-console root endpoints", () => {
  test("POST /api/memory/admit forwards to governor /admit (key + body + response relayed)", async () => {
    const payload = { workspace: "tenant-example-fieldservice", text: "pinned playbook memory" };
    const res = await req("/api/memory/admit", {
      method: "POST",
      token: memoryAdminJwt(),
      body: payload,
    });
    assert.equal(res.status, 200);
    assert.deepEqual(res.json, { ok: true, path: "/admit" }); // stub response relayed back
    const got = govRequests.at(-1);
    assert.equal(got.method, "POST");
    assert.equal(got.url, "/admit");
    assert.equal(got.headers["x-governor-key"], "governor-key-test");
    assert.equal(got.headers["x-memory-actor"], "test-suite");
    assert.equal(got.headers.authorization, undefined, "Bearer must be stripped");
    assert.deepEqual(JSON.parse(got.body), payload);
  });

  test("POST /api/memory/workspace-expire forwards to governor /workspace-expire", async () => {
    const payload = { workspace: "tenant-example-fieldservice" };
    const res = await req("/api/memory/workspace-expire", {
      method: "POST",
      token: memoryAdminJwt(),
      body: payload,
    });
    assert.equal(res.status, 200);
    assert.deepEqual(res.json, { ok: true, path: "/workspace-expire" });
    const got = govRequests.at(-1);
    assert.equal(got.url, "/workspace-expire");
    assert.equal(got.headers["x-governor-key"], "governor-key-test");
    assert.deepEqual(JSON.parse(got.body), payload);
  });

  test("query strings survive the root rewrite", async () => {
    const res = await req("/api/memory/admit?dry_run=1", {
      method: "POST",
      token: memoryAdminJwt(),
      body: {},
    });
    assert.equal(res.status, 200);
    assert.equal(govRequests.at(-1).url, "/admit?dry_run=1");
  });

  test("regression: GET /api/memory list flow still maps to /memory*", async () => {
    const res = await req("/api/memory?workspace=hermes&klass=durable_fact", {
      token: memoryAdminJwt(),
    });
    assert.equal(res.status, 200);
    assert.equal(govRequests.at(-1).url, "/memory?workspace=hermes&klass=durable_fact");
  });

  test("regression: POST /api/memory/<id>/action still maps under /memory", async () => {
    // Two path segments after /memory, so the one-segment "POST:/api/memory/*"
    // SCOPE_MAP wildcard never matched this route — the operator CLI drives it
    // with an admin-role token (null scopes bypass), same as before this change.
    const res = await req("/api/memory/doc-123/action", {
      method: "POST",
      token: makeJwt({ ...BASE_CLAIMS, sub: "operator", role: "admin" }),
      body: { action: "pin" },
    });
    assert.equal(res.status, 200);
    assert.equal(govRequests.at(-1).url, "/memory/doc-123/action");
  });

  test("regression: GET /api/digest still maps to /digest", async () => {
    const res = await req("/api/digest?window_hours=48", { token: memoryAdminJwt() });
    assert.equal(res.status, 200);
    assert.equal(govRequests.at(-1).url, "/digest?window_hours=48");
  });

  test("only POST gets the root rewrite — GET /api/memory/admit stays under /memory", async () => {
    const res = await req("/api/memory/admit", { token: memoryAdminJwt() });
    assert.equal(res.status, 200);
    assert.equal(govRequests.at(-1).url, "/memory/admit");
  });

  test("admit requires memory:admin — scoped token without it → 403, governor untouched", async () => {
    const countBefore = govRequests.length;
    const res = await req("/api/memory/admit", {
      method: "POST",
      token: scopedJwt("issues:write", "agents:write"),
      body: { workspace: "t" },
    });
    assert.equal(res.status, 403);
    assert.match(res.text, /memory:admin scope required/);
    assert.equal(govRequests.length, countBefore, "stub governor must not be hit");
  });

  test("admit rejects missing bearer (401)", async () => {
    const res = await req("/api/memory/admit", { method: "POST", body: {} });
    assert.equal(res.status, 401);
  });
});

// ── checkScope — native instructions routes ──────────────────────────────────

describe("checkScope — instructions routes", () => {
  const AGENT = "00000000-0000-4000-8000-000000000000";

  test("GET instructions-bundle requires agents:read", () => {
    assert.equal(proxy.checkScope("GET", `/api/agents/${AGENT}/instructions-bundle`, ["agents:read"]), true);
    assert.equal(proxy.checkScope("GET", `/api/agents/${AGENT}/instructions-bundle`, ["issues:read"]), false);
  });

  test("PATCH instructions-bundle requires agents:write", () => {
    assert.equal(proxy.checkScope("PATCH", `/api/agents/${AGENT}/instructions-bundle`, ["agents:write"]), true);
    assert.equal(proxy.checkScope("PATCH", `/api/agents/${AGENT}/instructions-bundle`, ["agents:read"]), false);
  });

  test("GET instructions-bundle/file requires agents:read", () => {
    assert.equal(proxy.checkScope("GET", `/api/agents/${AGENT}/instructions-bundle/file`, ["agents:read"]), true);
    assert.equal(proxy.checkScope("GET", `/api/agents/${AGENT}/instructions-bundle/file`, []), false);
  });

  test("PUT instructions-bundle/file requires agents:write", () => {
    assert.equal(proxy.checkScope("PUT", `/api/agents/${AGENT}/instructions-bundle/file`, ["agents:write"]), true);
    assert.equal(proxy.checkScope("PUT", `/api/agents/${AGENT}/instructions-bundle/file`, ["agents:read"]), false);
  });

  test("DELETE instructions-bundle/file requires agents:write", () => {
    assert.equal(proxy.checkScope("DELETE", `/api/agents/${AGENT}/instructions-bundle/file`, ["agents:write"]), true);
    assert.equal(proxy.checkScope("DELETE", `/api/agents/${AGENT}/instructions-bundle/file`, ["agents:read"]), false);
  });

  test("PATCH instructions-path requires agents:write", () => {
    assert.equal(proxy.checkScope("PATCH", `/api/agents/${AGENT}/instructions-path`, ["agents:write"]), true);
    assert.equal(proxy.checkScope("PATCH", `/api/agents/${AGENT}/instructions-path`, ["agents:read"]), false);
  });

  test("memory:admin gates POST /api/memory/admit + workspace-expire", () => {
    assert.equal(proxy.checkScope("POST", "/api/memory/admit", ["memory:admin"]), true);
    assert.equal(proxy.checkScope("POST", "/api/memory/admit", ["agents:write"]), false);
    assert.equal(proxy.checkScope("POST", "/api/memory/workspace-expire", ["memory:admin"]), true);
    assert.equal(proxy.checkScope("POST", "/api/memory/workspace-expire", ["issues:write"]), false);
  });
});

// ── Instructions routes through the live proxy ───────────────────────────────

describe("instructions routes (live server)", () => {
  const AGENT = "00000000-0000-4000-8000-000000000000";

  test("PUT instructions-bundle/file without agents:write → 403", async () => {
    const res = await req(`/api/agents/${AGENT}/instructions-bundle/file`, {
      method: "PUT",
      token: scopedJwt("agents:read"),
      body: { content: "# AGENTS.md" },
    });
    assert.equal(res.status, 403);
    assert.match(res.text, /Insufficient scope/);
  });

  test("PUT instructions-bundle/file WITH agents:write clears the scope gate (dies at the dead backend)", async () => {
    const res = await req(`/api/agents/${AGENT}/instructions-bundle/file`, {
      method: "PUT",
      token: scopedJwt("agents:write"),
      body: { content: "# AGENTS.md" },
    });
    // Port 39999 has nothing listening, so the forward fails with 502 —
    // which PROVES the request cleared JWT auth + the agents:write scope
    // (a scope failure would have been a 403 before any backend contact).
    assert.equal(res.status, 502);
  });
});
} // if (proxy) — reference suite; runs only when the control-plane proxy is present
