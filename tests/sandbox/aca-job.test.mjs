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

const { createSandbox, AcaJobSandbox, LocalSandbox, PROVIDERS } = await import(
  "../../apps/paperclip/sandbox.mjs"
);

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
  function fakeTransport(captured, response) {
    return async (url, init) => {
      captured.url = url;
      captured.init = init;
      return (
        response || {
          status: 200,
          json: async () => ({ stdout: "hello\n", stderr: "", exitCode: 0 }),
        }
      );
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
    assert.deepEqual(res, {
      exitCode: 0,
      signal: null,
      timedOut: false,
      stdout: "hello\n",
      stderr: "",
    });
    assert.match(captured.url, /\/code\/execute\?api-version=/);
    assert.equal(captured.init.method, "POST");
    assert.equal(captured.init.headers.Authorization, "Bearer tok-123");
    assert.match(captured.init.headers["Content-Type"], /application\/json/);
    // The command is joined into a single string in the request body.
    assert.match(captured.init.body, /"command":"echo hello"/);
  });

  test("maps a non-zero exit code from the response", async () => {
    const captured = {};
    const sb = createSandbox("aca-job", {
      poolEndpoint: "https://pool.example",
      transport: fakeTransport(captured, {
        status: 200,
        json: async () => ({ stdout: "", stderr: "boom", exitCode: 3 }),
      }),
      getToken: async () => "tok",
    });
    const res = await sb.exec("false", []);
    assert.equal(res.exitCode, 3);
    assert.equal(res.stderr, "boom");
    assert.equal(res.timedOut, false);
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
