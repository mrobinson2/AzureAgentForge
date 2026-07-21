/**
 * Agent identity — PAPERCLIP_AGENT_SLUG injection.
 *
 * Upstream's buildPaperclipEnv hands the spawned Hermes PAPERCLIP_AGENT_ID (a
 * UUID) and PAPERCLIP_COMPANY_ID, but no slug. The memory helpers key identity
 * off PAPERCLIP_AGENT_SLUG, so without the injection every agent's writes
 * collapse onto one fallback peer — memory cannot be attributed per agent, and
 * the governor enforces the per-agent memoryProfile against the wrong identity.
 *
 * The slug must match the platform's existing convention exactly, because three
 * places have to agree on it: services/watchdog/roster.py's name→slug map,
 * governor/profiles.py's profile keys, and this injection. These tests pin the
 * injected expression against that map, so drift in either direction fails.
 *
 * Offline: reads the patch source, evaluates only the injected expression.
 */

import { strict as assert } from "node:assert";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const PATCH_SRC = readFileSync(
  new URL("../../apps/paperclip/patch-adapter.mjs", import.meta.url),
  "utf-8",
);

/** Pull the injected slug expression out of the patch and make it callable. */
function slugifyFromPatch(name) {
  const m = PATCH_SRC.match(
    /const AGENT_SLUG_INJECTION =\s*([\s\S]*?);\n\nif \(!execute\.includes/,
  );
  assert.ok(m, "AGENT_SLUG_INJECTION not found — did the patch get restructured?");
  // The constant is a JS-source string; evaluate it to get the concatenated code.
  const injected = eval(m[1]); // eslint-disable-line no-eval
  const expr = injected
    .replace(/^if \(ctx\.agent\?\.name\) env\.PAPERCLIP_AGENT_SLUG = /, "")
    .trim()
    .replace(/;\s*$/, "");
  const ctx = { agent: { name } };
  const env = {};
  // eslint-disable-next-line no-eval
  return eval(`(function (ctx, env) { return ${expr}; })`)(ctx, env);
}

// Every slug the governor's DEFAULT_PROFILES and the watchdog roster know, keyed
// by the display name PaperClip actually carries on the agent record.
const ROSTER = [
  ["Orchestrator", "orchestrator"],
  ["Strategy", "strategy"],
  ["Planner", "planner"],
  ["Coder", "coder"],
  ["Infrastructure", "infrastructure"],
  ["Researcher", "researcher"],
  ["Coach", "coach"],
  ["Business", "business"],
  ["Psychology", "psychology"],
  ["QA", "qa"],
  ["Security", "security"],
  ["CostGuardian", "cost-guardian"],
  ["Curator", "curator"],
];

test("every roster display name maps to its canonical slug", () => {
  for (const [name, slug] of ROSTER) {
    assert.equal(slugifyFromPatch(name), slug, `${name} must slug to ${slug}`);
  }
});

test("an all-caps name is not split mid-acronym", () => {
  // "QA" -> "qa", never "q-a": the boundary rule only fires lower→upper.
  assert.equal(slugifyFromPatch("QA"), "qa");
});

test("spaces and stray punctuation normalize to single hyphens", () => {
  assert.equal(slugifyFromPatch("Cost Guardian"), "cost-guardian");
  assert.equal(slugifyFromPatch("  Coder  "), "coder");
  assert.equal(slugifyFromPatch("Q&A / Testing"), "q-a-testing");
});

test("no leading or trailing hyphens survive", () => {
  assert.equal(slugifyFromPatch("!Researcher!"), "researcher");
});

test("the injection is guarded on ctx.agent?.name", () => {
  // An agent record without a name must not produce PAPERCLIP_AGENT_SLUG="" —
  // an empty slug is a peer id, and an empty peer is its own accidental identity.
  assert.match(PATCH_SRC, /if \(ctx\.agent\?\.name\) env\.PAPERCLIP_AGENT_SLUG/);
});

test("a missing anchor fails the build rather than shipping silently", () => {
  // Shipping without the injection returns the platform to one shared memory
  // peer. That has to be a build failure, not a warning nobody reads.
  const block = PATCH_SRC.slice(PATCH_SRC.indexOf("AGENT_SLUG_INJECTION"));
  assert.match(block, /FATAL: 'if \(ctx\.runId\)' anchor not found/);
  assert.match(block, /process\.exit\(1\)/);
});
