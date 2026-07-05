// Tests for the vendor-neutral intake webhook reference handler.
// Zero dependencies — Node's built-in test runner. Run:  node --test handler.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  timingSafeEqual,
  verifyBearer,
  idempotencyKey,
  normalizeIntake,
  buildHandoffIssue,
  handleIntakeWebhook,
} from "./handler.mjs";

// A tiny in-memory idempotency store standing in for Redis/DB.
function memStore() {
  const seen = new Set();
  return { has: async (k) => seen.has(k), add: async (k) => seen.add(k), seen };
}

test("timingSafeEqual matches equal, rejects unequal and length diffs", () => {
  assert.equal(timingSafeEqual("abc", "abc"), true);
  assert.equal(timingSafeEqual("abc", "abd"), false);
  assert.equal(timingSafeEqual("abc", "abcd"), false);
});

test("verifyBearer: empty expected secret denies (webhook disabled)", () => {
  assert.equal(verifyBearer("Bearer anything", ""), false);
  assert.equal(verifyBearer("Bearer anything", undefined), false);
});

test("verifyBearer: correct/incorrect/malformed headers", () => {
  assert.equal(verifyBearer("Bearer s3cret", "s3cret"), true);
  assert.equal(verifyBearer("bearer s3cret", "s3cret"), true); // case-insensitive scheme
  assert.equal(verifyBearer("Bearer wrong", "s3cret"), false);
  assert.equal(verifyBearer("s3cret", "s3cret"), false); // no scheme
  assert.equal(verifyBearer("", "s3cret"), false);
});

test("idempotencyKey: prefers a provider id, falls back to a body hash", () => {
  assert.equal(idempotencyKey({ external_id: "X1" }), "intake:X1");
  assert.equal(idempotencyKey({ call: { id: "C9" } }), "intake:C9");
  const a = idempotencyKey({ a: 1, b: 2 });
  const b = idempotencyKey({ a: 1, b: 2 });
  assert.equal(a, b); // stable
  assert.ok(a.startsWith("intake:h"));
  assert.notEqual(idempotencyKey({ a: 1 }), idempotencyKey({ a: 2 }));
});

test("normalizeIntake: maps varied provider shapes to a neutral object", () => {
  const n = normalizeIntake({
    external_id: "E1",
    name: "Jordan Rivers",
    email: "jordan@example.com",
    summary: "wants a quote",
    answers: { budget: "medium", timeline: "Q3" },
    source: "voice-agent",
  });
  assert.equal(n.externalId, "E1");
  assert.equal(n.contact.name, "Jordan Rivers");
  assert.equal(n.contact.email, "jordan@example.com");
  assert.equal(n.summary, "wants a quote");
  assert.deepEqual(n.fields, { budget: "medium", timeline: "Q3" });
  assert.equal(n.receivedVia, "voice-agent");
});

test("buildHandoffIssue: camelCase body with assignee + external ref", () => {
  const issue = buildHandoffIssue(
    normalizeIntake({ external_id: "E1", name: "Sam", answers: { q1: "yes" } }),
    { handoffAgentId: "agent-123", title: "New intake" }
  );
  assert.equal(issue.title, "New intake");
  assert.equal(issue.assigneeAgentId, "agent-123");
  assert.equal(issue.externalRef, "E1");
  assert.ok(issue.description.includes("q1: yes"));
  // No snake_case keys (the platform API drops them silently).
  assert.equal("assignee_agent_id" in issue, false);
});

test("handleIntakeWebhook: rejects a bad secret before any side effect", async () => {
  let called = false;
  const res = await handleIntakeWebhook(
    { authorization: "Bearer nope", body: { external_id: "E1" } },
    { expectedSecret: "right", store: memStore(), fetch: async () => { called = true; return { ok: true }; } }
  );
  assert.equal(res.status, 401);
  assert.equal(called, false);
});

test("handleIntakeWebhook: happy path creates a handoff and records the key", async () => {
  const store = memStore();
  let posted = null;
  const res = await handleIntakeWebhook(
    { authorization: "Bearer right", body: { external_id: "E1", name: "Lee" } },
    {
      expectedSecret: "right",
      handoffAgentId: "agent-9",
      workItemsUrl: "http://localhost/api/issues",
      apiToken: "tok",
      store,
      fetch: async (url, opts) => { posted = { url, opts }; return { ok: true, status: 201 }; },
    }
  );
  assert.equal(res.status, 201);
  assert.equal(res.body.status, "handed_off");
  assert.ok(store.seen.has("intake:E1"));
  assert.match(posted.opts.headers.Authorization, /Bearer tok/);
});

test("handleIntakeWebhook: duplicate delivery is a no-op 200", async () => {
  const store = memStore();
  await store.add("intake:E1");
  let called = false;
  const res = await handleIntakeWebhook(
    { authorization: "Bearer right", body: { external_id: "E1" } },
    { expectedSecret: "right", store, fetch: async () => { called = true; return { ok: true }; } }
  );
  assert.equal(res.status, 200);
  assert.equal(res.body.status, "duplicate");
  assert.equal(called, false); // no second handoff
});

test("handleIntakeWebhook: upstream failure returns 502 and does NOT record the key", async () => {
  const store = memStore();
  const res = await handleIntakeWebhook(
    { authorization: "Bearer right", body: { external_id: "E1" } },
    {
      expectedSecret: "right",
      workItemsUrl: "http://localhost/api/issues",
      apiToken: "tok",
      store,
      fetch: async () => ({ ok: false, status: 500 }),
    }
  );
  assert.equal(res.status, 502);
  assert.equal(store.seen.has("intake:E1"), false); // provider may retry
});
