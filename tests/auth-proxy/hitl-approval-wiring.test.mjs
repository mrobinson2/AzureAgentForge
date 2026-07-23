/**
 * HITL approval-gate wiring unit tests (node:test, zero dependencies, offline).
 *
 * Run:  node --test tests/auth-proxy/hitl-approval-wiring.test.mjs
 *
 * Exercises the auth-proxy side of the HITL wiring: the outbound-comment route
 * matcher and gateOutboundMessage's decide + emit behavior. The approval gate
 * and the escalation-event emitter are INJECTED, so no network and no boot-time
 * env singleton is touched — the module import is side-effect-free.
 */

import { test, describe } from "node:test";
import assert from "node:assert/strict";

import { gateOutboundMessage, outboundCommentIssueId } from "../../apps/paperclip/auth-proxy.mjs";
import { AutoGate, AllowGate } from "../../apps/paperclip/approval.mjs";

const GATED = new Set(["outbound_message"]);

/** A spy emit that records every event passed to gateOutboundMessage. */
function spyEmit() {
  const calls = [];
  const fn = async (evt) => { calls.push(evt); };
  fn.calls = calls;
  return fn;
}

describe("outboundCommentIssueId", () => {
  test("matches the comment POST route and extracts the issue id", () => {
    assert.equal(outboundCommentIssueId("POST", "/api/issues/abc-123/comments"), "abc-123");
  });
  test("rejects other methods and paths", () => {
    assert.equal(outboundCommentIssueId("GET", "/api/issues/abc-123/comments"), null);
    assert.equal(outboundCommentIssueId("POST", "/api/issues/abc-123"), null);
    assert.equal(outboundCommentIssueId("POST", "/api/issues/abc-123/comments/extra"), null);
    assert.equal(outboundCommentIssueId("PATCH", "/api/issues/abc-123/comments"), null);
  });
});

describe("gateOutboundMessage — inert by default", () => {
  test("an un-gated kind passes, no escalation id, no emit", async () => {
    const gate = new AutoGate({ requiredKinds: new Set() }); // nothing gated
    const emit = spyEmit();
    const r = await gateOutboundMessage(
      { agent: "forge", summary: "hi", issueId: "i1" },
      { gate, emit },
    );
    assert.equal(r.approved, true);
    assert.equal(r.escalationId, null);
    assert.equal(emit.calls.length, 0);
  });
});

describe("gateOutboundMessage — gated", () => {
  test("auto provider FAILS CLOSED and emits opened + denied decision", async () => {
    const gate = new AutoGate({ requiredKinds: GATED });
    const emit = spyEmit();
    const r = await gateOutboundMessage(
      { agent: "forge", summary: "post this", issueId: "i9" },
      { gate, emit },
    );
    assert.equal(r.approved, false);
    assert.ok(r.escalationId, "a gated action mints an escalation id");
    assert.equal(emit.calls.length, 2);

    const [opened, decision] = emit.calls;
    assert.equal(opened.event_type, "escalation_opened");
    assert.equal(opened.escalation_id, r.escalationId);
    assert.equal(opened.lane, "red");
    assert.equal(opened.source, "approval");
    assert.equal(opened.actor_peer, "forge");
    assert.equal(opened.issue_id, "i9");

    assert.equal(decision.event_type, "autonomy_decision");
    assert.equal(decision.escalation_id, r.escalationId);
    assert.equal(decision.decision, "denied");
    assert.equal(typeof decision.latency_ms, "number");
  });

  test("allow provider approves and emits opened + approved decision", async () => {
    const gate = new AllowGate({ requiredKinds: GATED });
    const emit = spyEmit();
    const r = await gateOutboundMessage(
      { agent: "atlas", summary: "ok", issueId: "i2" },
      { gate, emit },
    );
    assert.equal(r.approved, true);
    assert.ok(r.escalationId);
    assert.equal(emit.calls.length, 2);
    assert.equal(emit.calls[0].event_type, "escalation_opened");
    assert.equal(emit.calls[1].event_type, "autonomy_decision");
    assert.equal(emit.calls[1].decision, "approved");
  });

  test("a missing agent degrades to unknown-agent in the emitted actor_peer", async () => {
    const gate = new AllowGate({ requiredKinds: GATED });
    const emit = spyEmit();
    await gateOutboundMessage({ agent: undefined, summary: "x", issueId: null }, { gate, emit });
    assert.equal(emit.calls[0].actor_peer, "unknown-agent");
  });
});
