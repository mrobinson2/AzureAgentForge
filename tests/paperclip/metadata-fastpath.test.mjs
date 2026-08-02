/**
 * Offline tests for:
 *   - apps/paperclip/metadata-fastpath.mjs (pure classify + answer-build logic)
 *   - apps/paperclip/patch-metadata-fastpath.mjs (the build-time transform
 *     that wires the above into heartbeat.ts's executeRun)
 *
 * Zero dependencies — Node 22+ built-in test runner:
 *   node --test tests/paperclip/metadata-fastpath.test.mjs
 *
 * No network, no LLM, no filesystem writes outside applyMetadataFastpath's
 * pure string transform (main()'s writeFileSync path is never exercised
 * here — only the exported pure function is).
 */

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import {
  classifyMetadataQuestion,
  buildMetadataAnswer,
  DEFAULT_ROUTE_NOTE,
} from "../../apps/paperclip/metadata-fastpath.mjs";
import {
  applyMetadataFastpath,
  MARKER,
} from "../../apps/paperclip/patch-metadata-fastpath.mjs";

// ── classifyMetadataQuestion: positives, one per kind ───────────────────────

describe("classifyMetadataQuestion — positives", () => {
  test("model_identity: 'What LLM are you using right now?'", () => {
    const result = classifyMetadataQuestion("What LLM are you using right now?", "");
    assert.deepEqual(result, { kind: "model_identity" });
  });

  test("model_identity: 'Which model powers this agent?'", () => {
    const result = classifyMetadataQuestion("Which model powers this agent?", "");
    assert.deepEqual(result, { kind: "model_identity" });
  });

  test("model_identity: 'model are you' phrasing", () => {
    const result = classifyMetadataQuestion("Quick check — what model are you, exactly?", "");
    assert.deepEqual(result, { kind: "model_identity" });
  });

  test("billing_route: 'subscription or metered'", () => {
    const result = classifyMetadataQuestion(
      "Is this run subscription or metered billing?",
      "",
    );
    assert.deepEqual(result, { kind: "billing_route" });
  });

  test("billing_route: bare 'billing' in a short question", () => {
    const result = classifyMetadataQuestion("How does billing work for this agent?", "");
    assert.deepEqual(result, { kind: "billing_route" });
  });

  test("run_provenance: 'which tier'", () => {
    const result = classifyMetadataQuestion("Which tier answered this issue?", "");
    assert.deepEqual(result, { kind: "run_provenance" });
  });

  test("run_provenance: 'what is the run id'", () => {
    const result = classifyMetadataQuestion("What is the run id for this?", "");
    assert.deepEqual(result, { kind: "run_provenance" });
  });

  test("duration: 'how long did'", () => {
    const result = classifyMetadataQuestion("How long did that last run take?", "");
    assert.deepEqual(result, { kind: "duration" });
  });

  test("matches when the question is in the description, not the title", () => {
    const result = classifyMetadataQuestion(
      "Quick question",
      "What LLM are you running for this task?",
    );
    assert.deepEqual(result, { kind: "model_identity" });
  });
});

// ── classifyMetadataQuestion: negatives — real task titles must never match ─
//
// This block is the load-bearing one: it exists to prove the false-positive
// asymmetry documented in metadata-fastpath.mjs's header actually holds.
// Wrongly fastpathing a real task is far worse than wrongly LLM-ing a
// metadata question, so every ambiguous or mixed-signal case below must
// fall through to null (i.e. to the normal LLM path).

describe("classifyMetadataQuestion — negatives (false positives are the failure mode)", () => {
  test("real task: content digest title", () => {
    assert.equal(
      classifyMetadataQuestion(
        "Daily Email - Channel Transcript Extract and Digest",
        "",
      ),
      null,
    );
  });

  test("real task: weather email implementation", () => {
    assert.equal(
      classifyMetadataQuestion("Implement daily local weather email", ""),
      null,
    );
  });

  test("real task: 'Review router configuration' (contains no metadata keywords)", () => {
    assert.equal(classifyMetadataQuestion("Review router configuration", ""), null);
  });

  test("real task: bare 'run id' without a leading question word (a real debugging task, not a metadata ask)", () => {
    assert.equal(
      classifyMetadataQuestion("Debug why run id 45 failed on the Foundry endpoint", ""),
      null,
    );
  });

  test("real task: 'which model should we use' style design discussion is intentionally in-scope per the pattern set", () => {
    // Documented trade-off, not a bug: the required pattern set
    // ("which (llm|model)") necessarily also matches a real design question
    // phrased the same way. False negatives are acceptable (one extra LLM
    // call); this test exists to make the trade-off visible rather than to
    // assert it should be null.
    assert.deepEqual(
      classifyMetadataQuestion("Which model should we use for this agent?", ""),
      { kind: "model_identity" },
    );
  });

  test("ambiguous/mixed: long, non-question-shaped description never matches even with an incidental keyword", () => {
    const longDescription =
      "This ticket tracks a multi-step migration of the customer data model to the " +
      "new schema. Steps: 1) add the new billing columns to the accounts table, " +
      "2) backfill historical rows from the legacy ledger, 3) update the reporting " +
      "views that join against the old model shape, 4) coordinate with finance on " +
      "the cutover window, 5) verify no downstream job still reads the deprecated " +
      "columns before dropping them. This description intentionally exceeds four " +
      "hundred characters and is not phrased as a question, so the length/shape " +
      "guard must suppress classification even though it contains both 'model' " +
      "and 'billing' as ordinary words.";
    assert.ok(longDescription.length > 400);
    assert.equal(classifyMetadataQuestion("Migrate customer data model", longDescription), null);
  });

  test("ambiguous/mixed: a real task that mentions 'model' and 'billing' in passing, short but not question-shaped", () => {
    assert.equal(
      classifyMetadataQuestion(
        "Update the billing model docs",
        "Refresh the pricing model documentation to match the new billing tiers.",
      ),
      null,
    );
  });

  test("empty title and description", () => {
    assert.equal(classifyMetadataQuestion("", ""), null);
  });

  test("non-string input never throws", () => {
    assert.equal(classifyMetadataQuestion(null, undefined), null);
    assert.equal(classifyMetadataQuestion(undefined, 12345), null);
  });
});

// ── buildMetadataAnswer — wording ───────────────────────────────────────────

describe("buildMetadataAnswer", () => {
  test("model_identity: exact required wording", () => {
    const answer = buildMetadataAnswer({
      kind: "model_identity",
      agentName: "Researcher",
      requestedModel: "gpt-5.1-chat",
      routeNote: "Routing/serving details live in the model-router configuration.",
      issueKey: "ISSUE-999",
    });
    assert.equal(
      answer,
      "This issue was answered by the platform's zero-LLM metadata fastpath.\n\n" +
        "Agent **Researcher** is configured to run **gpt-5.1-chat** " +
        "(requested model, from the deployment roster). Routing/serving details live in the model-router configuration.\n\n" +
        "This is platform routing metadata, not model self-identification.",
    );
  });

  test("falls back to DEFAULT_ROUTE_NOTE when routeNote is omitted", () => {
    const answer = buildMetadataAnswer({
      kind: "model_identity",
      agentName: "Ops",
      requestedModel: "gpt-5.1-chat",
    });
    assert.ok(answer.includes(DEFAULT_ROUTE_NOTE));
  });

  test("every kind's answer contains the requested model, the route note, and the closing disclaimer", () => {
    for (const kind of ["model_identity", "billing_route", "run_provenance", "duration"]) {
      const answer = buildMetadataAnswer({
        kind,
        agentName: "QA",
        requestedModel: "claude-sonnet-4-6",
        routeNote: "custom operator note here",
        issueKey: "ISSUE-42",
      });
      assert.ok(answer.includes("claude-sonnet-4-6"), `${kind} missing requested model`);
      assert.ok(answer.includes("custom operator note here"), `${kind} missing routeNote`);
      assert.ok(
        answer.includes("This is platform routing metadata, not model self-identification."),
        `${kind} missing the not-self-identification line`,
      );
      assert.ok(
        answer.startsWith("This issue was answered by the platform's zero-LLM metadata fastpath."),
        `${kind} missing the standard header`,
      );
    }
  });

  test("unknown kind falls back to the model_identity wording rather than throwing", () => {
    const answer = buildMetadataAnswer({ kind: "not_a_real_kind", agentName: "X", requestedModel: "Y" });
    assert.ok(answer.includes("is configured to run"));
  });

  test("never leaks a polluted environment — the function only ever echoes its explicit args", () => {
    const originalEnv = { ...process.env };
    process.env.OPENAI_API_KEY = "sk-totally-fake-test-secret-do-not-leak";
    process.env.PAPERCLIP_AUTOMATION_JWT_SECRET = "totally-fake-test-jwt-value";
    try {
      const answer = buildMetadataAnswer({
        kind: "model_identity",
        agentName: "Researcher",
        requestedModel: "gpt-5.1-chat",
      });
      assert.ok(!answer.includes("sk-totally-fake-test-secret-do-not-leak"));
      assert.ok(!answer.includes("totally-fake-test-jwt-value"));
      assert.ok(!answer.toLowerCase().includes("openai_api_key"));
    } finally {
      process.env = originalEnv;
    }
  });

  test("missing/empty fields fall back to safe defaults instead of printing 'undefined'", () => {
    const answer = buildMetadataAnswer({ kind: "model_identity" });
    assert.ok(!answer.includes("undefined"));
    assert.ok(answer.includes("this agent"));
    assert.ok(answer.includes("unknown"));
  });
});

// ── The patch transform itself, against a real (trimmed, verbatim) fixture ──
//
// Source: paperclipai/paperclip, tag v2026.707.0 (the pin in
// services/paperclip/Dockerfile's PAPERCLIP_VERSION arg, SHA
// 390627b46eb333309d357004384b220ecf8a65af — verified against the real
// upstream tag during development of this patch), server/src/services/
// heartbeat.ts. Two segments, verbatim, joined with a truncation marker —
// the real file is ~14.8k lines; these are the two regions
// patch-metadata-fastpath.mjs anchors on (near line 113 and line 10070).

const VENDORED_FIXTURE = [
  // --- server/src/services/heartbeat.ts, near the local imports (~L108-115) ---
  '  type RealizedExecutionWorkspace,',
  '  sanitizeRuntimeServiceBaseEnv,',
  '} from "./workspace-runtime.js";',
  'import { issueService } from "./issues.js";',
  'import {',
  '  ISSUE_BLOCKERS_RESOLVED_WAKE_REASON,',
  '',
  '// ... (trimmed — ~9,900 lines of unrelated service code) ...',
  '',
  // --- server/src/services/heartbeat.ts, inside executeRun (~L10060-10072) ---
  '    try {',
  '    const agent = await getAgent(run.agentId);',
  '    if (!agent) {',
  '      await setRunStatus(runId, "failed", {',
  '        error: "Agent not found",',
  '        errorCode: "agent_not_found",',
  '        finishedAt: new Date(),',
  '      });',
  '      await setWakeupStatus(run.wakeupRequestId, "failed", {',
  '        finishedAt: new Date(),',
  '        error: "Agent not found",',
  '      });',
  '      const failedRun = await getRun(runId);',
  '      if (failedRun) await releaseIssueExecutionAndPromote(failedRun);',
  '      return;',
  '    }',
  '',
  '    const runtime = await ensureRuntimeState(agent);',
  '    const context = parseObject(run.contextSnapshot);',
  '    const taskKey = deriveTaskKeyWithHeartbeatFallback(context, null);',
  '    const sessionCodec = getAdapterSessionCodec(agent.adapterType);',
  '    const issueId = readNonEmptyString(context.issueId);',
  '    let issueContext = issueId ? await getIssueExecutionContext(agent.companyId, issueId) : null;',
  '    const issueDependencyReadiness = issueId',
  '      ? await issuesSvc.listDependencyReadiness(agent.companyId, [issueId]).then((rows) => rows.get(issueId) ?? null)',
  '      : null;',
].join("\n");

test("transform inserts the import right after the issues.js import", () => {
  const { src, applied, reason } = applyMetadataFastpath(VENDORED_FIXTURE);
  assert.equal(applied, true, reason);
  assert.match(src, /from "\.\/metadata-fastpath\.mjs"/);
  const importIdx = src.indexOf('import { issueService } from "./issues.js";');
  const fastpathImportIdx = src.indexOf("classifyMetadataQuestion, buildMetadataAnswer");
  assert.ok(fastpathImportIdx > importIdx, "fastpath import must land after the issues.js import");
});

test("transform inserts the fastpath block right after issueContext is resolved", () => {
  const { src, applied } = applyMetadataFastpath(VENDORED_FIXTURE);
  assert.equal(applied, true);
  const anchorIdx = src.indexOf(
    "let issueContext = issueId ? await getIssueExecutionContext(agent.companyId, issueId) : null;",
  );
  const fastpathIdx = src.indexOf("PAPERCLIP_METADATA_FASTPATH");
  assert.ok(fastpathIdx > anchorIdx, "fastpath block must land after issueContext is resolved");
  // Must land BEFORE the next real line (issueDependencyReadiness) so the
  // short-circuit actually happens before further executeRun setup.
  const nextLineIdx = src.indexOf("const issueDependencyReadiness = issueId");
  assert.ok(fastpathIdx < nextLineIdx, "fastpath block must land before the next executeRun statement");
});

test("transform includes flag gate, comment/status/run-status/wakeup-status calls, and a fallback catch", () => {
  const { src } = applyMetadataFastpath(VENDORED_FIXTURE);
  assert.match(src, /process\.env\.PAPERCLIP_METADATA_FASTPATH === "1"/);
  assert.match(src, /issuesSvc\.addComment\(/);
  assert.match(src, /issuesSvc\.update\(issueId, \{ status: "done" \}\)/);
  assert.match(src, /setRunStatus\(runId, "succeeded"/);
  assert.match(src, /setWakeupStatus\(run\.wakeupRequestId, "completed"/);
  assert.match(src, /releaseIssueExecutionAndPromote\(/);
  assert.match(src, /catch \(err\) \{/);
});

test("transform is idempotent on an already-patched source", () => {
  const first = applyMetadataFastpath(VENDORED_FIXTURE);
  const second = applyMetadataFastpath(first.src);
  assert.equal(second.applied, false);
  assert.equal(second.alreadyPatched, true);
  assert.equal(second.reason, "already applied");
  assert.equal(second.src, first.src);
});

test("transform fails loudly when the import anchor has drifted", () => {
  const driftedFixture = VENDORED_FIXTURE.replace(
    'import { issueService } from "./issues.js";',
    'import { issueService as issuesServiceRenamed } from "./issues.js";',
  );
  const result = applyMetadataFastpath(driftedFixture);
  assert.equal(result.applied, false);
  assert.equal(result.alreadyPatched, false);
  assert.equal(result.reason, "import anchor not found");
});

test("transform fails loudly when the logic anchor has drifted", () => {
  const driftedFixture = VENDORED_FIXTURE.replace(
    "let issueContext = issueId ? await getIssueExecutionContext(agent.companyId, issueId) : null;",
    "let issueContext = issueId ? await getIssueExecutionContextV2(agent.companyId, issueId) : null;",
  );
  const result = applyMetadataFastpath(driftedFixture);
  assert.equal(result.applied, false);
  assert.equal(result.alreadyPatched, false);
  assert.equal(result.reason, "logic anchor not found");
});

test("transform fails loudly on completely unrelated source", () => {
  const result = applyMetadataFastpath("// completely different file, no anchors here");
  assert.equal(result.applied, false);
  assert.equal(result.alreadyPatched, false);
  assert.equal(result.reason, "import anchor not found");
});

test("transform fails loudly when the import anchor appears more than once", () => {
  const duplicated = VENDORED_FIXTURE.replace(
    'import { issueService } from "./issues.js";',
    'import { issueService } from "./issues.js";\nimport { issueService } from "./issues.js";',
  );
  const result = applyMetadataFastpath(duplicated);
  assert.equal(result.applied, false);
  assert.equal(result.alreadyPatched, false);
  assert.equal(result.reason, "import anchor matched more than once");
  assert.equal(result.found, 2);
});

test("MARKER is exported and present in the patched output exactly for idempotency detection", () => {
  const { src } = applyMetadataFastpath(VENDORED_FIXTURE);
  assert.ok(typeof MARKER === "string" && MARKER.length > 0);
  assert.ok(src.includes(MARKER));
});
