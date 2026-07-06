/**
 * aca-job provider unit tests (node:test, zero dependencies).
 *
 * Run:  node --test tests/sandbox/aca-job.test.mjs
 *
 * Fully offline: the ACA dynamic-sessions HTTP call is exercised through an
 * INJECTED fake transport, so no network, no Azure SDK, and no live session
 * pool are touched. Covers factory registration, fail-closed config, the
 * request/response mapping, the never-reject contract, and that the default
 * SANDBOX_PROVIDER still selects `local`.
 */

import { test, describe } from "node:test";
import assert from "node:assert/strict";

const {
  createSandbox,
  AcaJobSandbox,
  LocalSandbox,
  PROVIDERS,
  acaManagedIdentityTokenProvider,
} = await import("../../apps/paperclip/sandbox.mjs");

describe("aca-job registration", () => {
  test("factory returns AcaJobSandbox for provider='aca-job'", () => {
    const sb = createSandbox("aca-job", {
      poolEndpoint: "https://pool.example",
      transport: async () => ({}),
    });
    assert.ok(sb instanceof AcaJobSandbox);
    assert.equal(sb.provider, "aca-job");
  });

  test("throws (fail closed) when poolEndpoint is missing", () => {
    assert.throws(() => createSandbox("aca-job", {}), /poolEndpoint/);
  });

  test("registry exposes local and aca-job", () => {
    assert.deepEqual(Object.keys(PROVIDERS).sort(), ["aca-job", "local"]);
  });

  test("default SANDBOX_PROVIDER still selects local", () => {
    const prev = process.env.SANDBOX_PROVIDER;
    delete process.env.SANDBOX_PROVIDER;
    try {
      const sb = createSandbox();
      assert.ok(sb instanceof LocalSandbox);
      assert.equal(sb.provider, "local");
    } finally {
      if (prev !== undefined) process.env.SANDBOX_PROVIDER = prev;
    }
  });
});

describe("AcaJobSandbox.exec", () => {
  // The documented executions response nests results under `properties`.
  function fakeTransport(captured, response) {
    return async (url, init) => {
      captured.url = url;
      captured.init = init;
      return (
        response || {
          status: 200,
          json: async () => ({
            properties: { status: "Succeeded", stdout: "hello\n", stderr: "" },
          }),
        }
      );
    };
  }

  test("posts to the executions endpoint with the documented shell shape", async () => {
    const captured = {};
    const sb = createSandbox("aca-job", {
      poolEndpoint: "https://pool.example",
      sessionId: "sess-42",
      transport: fakeTransport(captured),
      getToken: async () => "tok-123",
    });
    const res = await sb.exec("echo", ["hello"], { timeoutMs: 5000 });
    assert.deepEqual(res, {
      exitCode: 0,
      signal: null,
      timedOut: false,
      stdout: "hello\n",
      stderr: "",
    });
    // Endpoint path is /executions; identifier is a QUERY parameter, not body.
    assert.match(captured.url, /\/executions\?api-version=2025-10-02-preview/);
    assert.match(captured.url, /[?&]identifier=sess-42/);
    assert.equal(captured.init.method, "POST");
    assert.equal(captured.init.headers.Authorization, "Bearer tok-123");
    assert.match(captured.init.headers["Content-Type"], /application\/json/);
    // Body carries shellCommand + executionType + a timeout, no identifier.
    const body = JSON.parse(captured.init.body);
    assert.equal(body.shellCommand, "echo hello");
    assert.equal(body.codeInputType, "inline");
    assert.equal(body.executionType, "synchronous");
    assert.equal(body.timeoutInSeconds, 5); // 5000ms → 5s
    assert.ok(!("identifier" in body), "identifier must be a query param");
  });

  test("defaults timeoutInSeconds to the ACA 220s cap when unset", async () => {
    const captured = {};
    const sb = createSandbox("aca-job", {
      poolEndpoint: "https://pool.example",
      transport: fakeTransport(captured),
      getToken: async () => "tok",
    });
    await sb.exec("sleep", ["1"]);
    assert.equal(JSON.parse(captured.init.body).timeoutInSeconds, 220);
  });

  test("maps a Failed status to a non-zero exit code", async () => {
    const sb = createSandbox("aca-job", {
      poolEndpoint: "https://pool.example",
      transport: async () => ({
        status: 200,
        json: async () => ({
          properties: { status: "Failed", stdout: "", stderr: "boom" },
        }),
      }),
      getToken: async () => "tok",
    });
    const res = await sb.exec("false", []);
    assert.equal(res.exitCode, 1);
    assert.equal(res.stderr, "boom");
    assert.equal(res.timedOut, false);
  });

  test("honors an explicit numeric exitCode in properties", async () => {
    const sb = createSandbox("aca-job", {
      poolEndpoint: "https://pool.example",
      transport: async () => ({
        status: 200,
        json: async () => ({ properties: { exitCode: 3, stderr: "x" } }),
      }),
      getToken: async () => "tok",
    });
    assert.equal((await sb.exec("false", [])).exitCode, 3);
  });

  test("degrades (not throws) on a flat/unwrapped response body", async () => {
    // Defensive fallback: if the response isn't `properties`-wrapped, read flat.
    const sb = createSandbox("aca-job", {
      poolEndpoint: "https://pool.example",
      transport: async () => ({
        status: 200,
        json: async () => ({ stdout: "flat", stderr: "" }),
      }),
      getToken: async () => "tok",
    });
    const res = await sb.exec("echo", ["flat"]);
    assert.equal(res.stdout, "flat");
    assert.equal(res.exitCode, 0);
  });

  test("never rejects — transport error becomes a result", async () => {
    const sb = createSandbox("aca-job", {
      poolEndpoint: "https://pool.example",
      transport: async () => {
        throw new Error("network down");
      },
      getToken: async () => "tok",
    });
    const res = await sb.exec("echo", ["x"]);
    assert.equal(res.exitCode, null);
    assert.equal(res.signal, null);
    assert.match(res.stderr, /network down/);
  });

  test("never rejects — a missing token provider surfaces in the result", async () => {
    // No getToken injected → the default provider throws; exec() must catch it.
    const sb = createSandbox("aca-job", {
      poolEndpoint: "https://pool.example",
      transport: async () => ({ status: 200, json: async () => ({}) }),
    });
    const res = await sb.exec("echo", ["x"]);
    assert.equal(res.exitCode, null);
    assert.match(res.stderr, /token provider/);
  });
});

describe("acaManagedIdentityTokenProvider", () => {
  test("fetches an IMDS token for the dynamicsessions audience", async () => {
    const captured = {};
    const getToken = acaManagedIdentityTokenProvider({
      transport: async (url, init) => {
        captured.url = url;
        captured.init = init;
        return { status: 200, json: async () => ({ access_token: "mi-tok" }) };
      },
    });
    const tok = await getToken();
    assert.equal(tok, "mi-tok");
    assert.match(captured.url, /169\.254\.169\.254/);
    assert.match(captured.url, /resource=https%3A%2F%2Fdynamicsessions\.io/);
    assert.equal(captured.init.headers.Metadata, "true");
  });

  test("throws (fail closed) on a non-200 / tokenless IMDS response", async () => {
    const getToken = acaManagedIdentityTokenProvider({
      transport: async () => ({ status: 400, json: async () => ({}) }),
    });
    await assert.rejects(getToken(), /token fetch failed/);
  });

  test("wires into AcaJobSandbox as the injected getToken", async () => {
    const captured = {};
    const sb = createSandbox("aca-job", {
      poolEndpoint: "https://pool.example",
      getToken: acaManagedIdentityTokenProvider({
        transport: async () => ({
          status: 200,
          json: async () => ({ access_token: "mi-2" }),
        }),
      }),
      transport: async (url, init) => {
        captured.auth = init.headers.Authorization;
        return { status: 200, json: async () => ({ properties: { status: "Succeeded" } }) };
      },
    });
    await sb.exec("echo", ["hi"]);
    assert.equal(captured.auth, "Bearer mi-2");
  });
});
