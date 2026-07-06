/**
 * Action-approval seam unit tests (node:test, zero dependencies).
 *
 * Run:  node --test tests/approval/*.test.mjs
 *
 * Fully offline: the webhook gate's HTTP call is exercised through an INJECTED
 * fake transport, so no network is touched. Covers the inert-by-default policy,
 * fail-closed semantics for gated actions, the explicit allow bypass, the
 * webhook decision mapping, and the never-reject contract.
 */

import { test, describe } from "node:test";
import assert from "node:assert/strict";

const {
  createApprovalGate,
  AutoGate,
  AllowGate,
  WebhookGate,
  APPROVAL_PROVIDERS,
  parseRequiredKinds,
} = await import("../../apps/paperclip/approval.mjs");

describe("registry + factory", () => {
  test("default provider is auto", () => {
    const prev = process.env.APPROVAL_PROVIDER;
    delete process.env.APPROVAL_PROVIDER;
    try {
      assert.ok(createApprovalGate() instanceof AutoGate);
    } finally {
      if (prev !== undefined) process.env.APPROVAL_PROVIDER = prev;
    }
  });

  test("registry exposes auto, allow, webhook", () => {
    assert.deepEqual(Object.keys(APPROVAL_PROVIDERS).sort(), ["allow", "auto", "webhook"]);
  });

  test("unknown provider fails loud", () => {
    assert.throws(() => createApprovalGate("bogus"), /Unknown APPROVAL_PROVIDER/);
  });

  test("parseRequiredKinds splits and trims", () => {
    assert.deepEqual([...parseRequiredKinds("a, b ,c")].sort(), ["a", "b", "c"]);
    assert.equal(parseRequiredKinds("").size, 0);
    assert.equal(parseRequiredKinds(undefined).size, 0);
  });
});

describe("inert by default", () => {
  test("no gated kinds → every action approved (seam is a no-op)", async () => {
    const gate = createApprovalGate("auto", { requiredKinds: new Set() });
    const r = await gate.requestApproval({ kind: "outbound_message" });
    assert.equal(r.approved, true);
    assert.equal(r.reason, "not gated");
  });
});

describe("auto gate fails closed for gated kinds", () => {
  test("a gated action with only the auto provider is DENIED", async () => {
    const gate = createApprovalGate("auto", {
      requiredKinds: new Set(["outbound_message"]),
    });
    const r = await gate.requestApproval({ kind: "outbound_message", summary: "text a customer" });
    assert.equal(r.approved, false);
    assert.match(r.reason, /no approver wired/);
  });

  test("a non-gated action still passes even when other kinds are gated", async () => {
    const gate = createApprovalGate("auto", {
      requiredKinds: new Set(["destructive_tool"]),
    });
    assert.equal((await gate.requestApproval({ kind: "read_file" })).approved, true);
  });
});

describe("allow gate", () => {
  test("explicitly approves gated actions (deliberate bypass)", async () => {
    const gate = createApprovalGate("allow", {
      requiredKinds: new Set(["outbound_message"]),
    });
    const r = await gate.requestApproval({ kind: "outbound_message" });
    assert.equal(r.approved, true);
    assert.equal(r.provider, "allow");
  });
});

describe("webhook gate", () => {
  const GATED = { requiredKinds: new Set(["outbound_message"]) };

  test("posts the action and approves on {approved:true}", async () => {
    const captured = {};
    const gate = new WebhookGate({
      ...GATED,
      endpoint: "https://approve.example/decide",
      transport: async (url, init) => {
        captured.url = url;
        captured.body = JSON.parse(init.body);
        return { status: 200, json: async () => ({ approved: true, reason: "ok by ops" }) };
      },
    });
    const r = await gate.requestApproval({
      kind: "outbound_message", summary: "reply to lead", agent: "agent-a",
    });
    assert.equal(r.approved, true);
    assert.equal(r.reason, "ok by ops");
    assert.equal(captured.url, "https://approve.example/decide");
    assert.equal(captured.body.kind, "outbound_message");
    assert.equal(captured.body.agent, "agent-a");
  });

  test("denies (fail closed) on {approved:false}", async () => {
    const gate = new WebhookGate({
      ...GATED,
      endpoint: "https://approve.example/decide",
      transport: async () => ({ status: 200, json: async () => ({ approved: false, reason: "nope" }) }),
    });
    const r = await gate.requestApproval({ kind: "outbound_message" });
    assert.equal(r.approved, false);
    assert.equal(r.reason, "nope");
  });

  test("fail closed on a non-200 response", async () => {
    const gate = new WebhookGate({
      ...GATED,
      endpoint: "https://approve.example/decide",
      transport: async () => ({ status: 500, json: async () => ({ approved: true }) }),
    });
    assert.equal((await gate.requestApproval({ kind: "outbound_message" })).approved, false);
  });

  test("fail closed when only a truthy-but-not-true approved is returned", async () => {
    const gate = new WebhookGate({
      ...GATED,
      endpoint: "https://approve.example/decide",
      transport: async () => ({ status: 200, json: async () => ({ approved: "yes" }) }),
    });
    assert.equal((await gate.requestApproval({ kind: "outbound_message" })).approved, false);
  });

  test("never rejects — a transport error becomes a denial", async () => {
    const gate = new WebhookGate({
      ...GATED,
      endpoint: "https://approve.example/decide",
      transport: async () => {
        throw new Error("network down");
      },
    });
    const r = await gate.requestApproval({ kind: "outbound_message" });
    assert.equal(r.approved, false);
    assert.match(r.reason, /network down/);
  });

  test("fail closed when no endpoint is configured", async () => {
    const gate = new WebhookGate({ ...GATED, endpoint: "" });
    const r = await gate.requestApproval({ kind: "outbound_message" });
    assert.equal(r.approved, false);
    assert.match(r.reason, /no endpoint/);
  });

  test("attaches a bearer token when getToken is provided", async () => {
    const captured = {};
    const gate = new WebhookGate({
      ...GATED,
      endpoint: "https://approve.example/decide",
      getToken: async () => "tok-9",
      transport: async (url, init) => {
        captured.auth = init.headers.Authorization;
        return { status: 200, json: async () => ({ approved: true }) };
      },
    });
    await gate.requestApproval({ kind: "outbound_message" });
    assert.equal(captured.auth, "Bearer tok-9");
  });
});
