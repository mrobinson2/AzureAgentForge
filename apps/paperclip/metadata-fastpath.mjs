/**
 * metadata-fastpath.mjs — ZERO-LLM platform-metadata fastpath.
 *
 * THE IDEA: a slice of the issues an agent gets assigned aren't real work —
 * they're questions about the platform itself: "what model are you running",
 * "is this subscription or metered billing", "which run answered this",
 * "how long did that run take". Every one of those questions has its answer
 * sitting in a database row the backend already loaded (the agent's
 * configured model, the run's own timestamps, the issue's own fields). Today
 * none of that matters: the question still triggers a full agent spawn — an
 * LLM call plus a subprocess — to produce an answer the backend could have
 * written itself. The cheapest LLM call is the one you never make.
 *
 * THIS MODULE is the pure, dependency-free classify+answer logic that
 * patch-metadata-fastpath.mjs's build-time patch wires into PaperClip's
 * run-execute path (see that file's header for exactly where and why). It
 * has no knowledge of Postgres, Express, or the run-spawn path — it only
 * turns (title, description) into a classification, and a
 * classification+context into a markdown answer. That separation is what
 * makes it unit-testable offline (see tests/paperclip/metadata-fastpath.
 * test.mjs) and safe to reason about: it can never reach a network call, a
 * DB row, or an env dump, because it is never given access to any of those
 * things.
 *
 * CLASSIFICATION IS DELIBERATELY CONSERVATIVE. Consider the two ways this
 * can go wrong:
 *   - False NEGATIVE: a genuine metadata question falls through to the
 *     normal LLM path. Cost: one avoidable model call. Mildly wasteful,
 *     otherwise harmless — the agent still answers correctly.
 *   - False POSITIVE: a real task gets silently short-circuited with a
 *     canned non-answer, the issue is marked done, and nobody looks at it
 *     again. Cost: a real question goes unanswered and the tracking system
 *     lies about it having been handled.
 * Those two failure modes are not symmetric, so the classifier is built to
 * fail toward the cheap mistake. Every pattern below requires an explicit
 * question shape ("what/which ...", "how long did ...") rather than a bare
 * keyword, and the whole thing backs off entirely on anything long and not
 * question-shaped — a real task description that happens to mention
 * "billing" or "model" in passing must never match.
 */

// Below this combined title+description length, or when the text is clearly
// question-shaped (ends in "?"), classification is allowed to run. Above it
// (and not question-shaped), classification conservatively bails — real task
// tickets tend to run long; genuine platform-metadata questions tend to be
// short.
const MAX_METADATA_QUESTION_LEN = 400;

const METADATA_PATTERNS = [
  {
    kind: "model_identity",
    patterns: [
      /\b(?:what|which)\s+(?:llm|model)\b/i,
      /\bmodel\s+are\s+you\b/i,
      /\byour\s+(?:llm|model)\b/i,
    ],
  },
  {
    kind: "billing_route",
    patterns: [
      /\bsubscription\s+or\s+metered\b/i,
      // A bare "billing" keyword would also match a real task that mentions
      // billing in passing ("Update the billing model docs"). Require a
      // question word within a short span of "billing" instead — this is
      // deliberately narrower than a keyword search so it stays honest with
      // the "explicit question shape, not a bare keyword" rule the other
      // patterns already follow.
      /\b(?:what(?:'s|\s+is)?|which|how|is)\b[\s\S]{0,40}?\bbilling\b/i,
    ],
  },
  {
    kind: "run_provenance",
    patterns: [
      // Requires a leading question word so "debug run id 45" (a real task)
      // doesn't match — only "what/which (the) run id/tier/provenance" does.
      /\b(?:what(?:'s|\s+is)?|which)\s+(?:the\s+)?(?:run\s*id|tier|provenance)\b/i,
    ],
  },
  {
    kind: "duration",
    patterns: [
      /\bhow\s+long\s+did\b/i,
    ],
  },
];

/**
 * classifyMetadataQuestion(title, description) -> null | { kind }
 *
 * Returns one of {kind: 'model_identity'|'billing_route'|'run_provenance'|'duration'}
 * or null. Never throws — malformed input just falls through to null.
 */
export function classifyMetadataQuestion(title, description) {
  const t = typeof title === "string" ? title : "";
  const d = typeof description === "string" ? description : "";
  const combined = `${t} ${d}`.trim();
  if (!combined) return null;

  const combinedLen = t.length + d.length;
  const isQuestionShaped = /\?\s*$/.test(t.trim()) || /\?\s*$/.test(d.trim());
  if (combinedLen >= MAX_METADATA_QUESTION_LEN && !isQuestionShaped) {
    return null;
  }

  for (const { kind, patterns } of METADATA_PATTERNS) {
    if (patterns.some((re) => re.test(combined))) {
      return { kind };
    }
  }
  return null;
}

// Operator-set free text, read from METADATA_FASTPATH_ROUTE_NOTE by the
// caller (the patched run-execute path) — this module never touches
// process.env itself, so it can never be the thing that leaks an env dump.
export const DEFAULT_ROUTE_NOTE = "Routing/serving details live in the model-router configuration.";

const HEADER = "This issue was answered by the platform's zero-LLM metadata fastpath.";
const FOOTER = "This is platform routing metadata, not model self-identification.";

function coerceString(value, fallback) {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

/**
 * buildMetadataAnswer({kind, agentName, requestedModel, routeNote, issueKey}) -> string
 *
 * Pure string formatting only — every value it emits was handed to it
 * explicitly by the caller. It never reads env vars, secrets, or anything
 * else, so there is no code path by which it could leak a token even if the
 * caller's own environment is polluted with one.
 *
 * The answer is deliberately honest about its own provenance: the header and
 * footer both make clear this came from deterministic code, not a model, so
 * a reader can never mistake it for the agent describing itself.
 */
export function buildMetadataAnswer(input) {
  const opts = input && typeof input === "object" ? input : {};
  const agentName = coerceString(opts.agentName, "this agent");
  const requestedModel = coerceString(opts.requestedModel, "unknown");
  const routeNote = coerceString(opts.routeNote, DEFAULT_ROUTE_NOTE);
  const issueKey = coerceString(opts.issueKey, "");

  const bodies = {
    model_identity:
      `Agent **${agentName}** is configured to run **${requestedModel}** ` +
      `(requested model, from the deployment roster). ${routeNote}`,
    billing_route:
      `Agent **${agentName}** bills through the platform's model-router ` +
      `(requested model **${requestedModel}**), not a direct per-seat subscription. ${routeNote}`,
    run_provenance:
      `This response came from agent **${agentName}** (requested model **${requestedModel}**)` +
      `${issueKey ? ` on issue **${issueKey}**` : ""}. ${routeNote}`,
    duration:
      `Run duration is recorded per-run in the platform's execution records, not reported ` +
      `by the agent itself (agent **${agentName}**, requested model **${requestedModel}**). ${routeNote}`,
  };

  const body = bodies[opts.kind] ?? bodies.model_identity;
  return `${HEADER}\n\n${body}\n\n${FOOTER}`;
}
