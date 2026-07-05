#!/usr/bin/env node
// AzureAgentForge cost-envelope — adapter-side per-run env injection.
//
// The hermes-paperclip-adapter spawns one Hermes process per task. This patch
// makes the adapter export ROUTER_RUN_ID (= ctx.runId = the triggering issue
// UUID) and, when the container provides one, a ROUTER_BUDGET_ENVELOPE_USD
// ceiling into that child's env. From there patch-hermes-cost-envelope.mjs
// forwards them into the model-router request metadata (metadata.run_id +
// metadata.budget_envelope_usd) so the model router can enforce a per-run spend
// ledger. The whole chain is inert unless COST_ENVELOPE_ENABLED=1 on the router.
//
// Pure + exported so it can be unit-tested in isolation. patch-adapter.mjs
// imports injectRouterRunEnv() and applies it to the adapter's execute.js at
// build time.
//
// Budget source (this increment): the per-run ceiling comes from the
// ROUTER_BUDGET_ENVELOPE_USD container env var — one ceiling applied to every
// run. The router keys its ledger by run_id, so even a single shared ceiling
// enforces a real invariant ("no individual run may exceed $X"), and it
// activates with one env var and zero per-delegation effort.
//
// The richer per-ISSUE path is designed but deliberately NOT wired here:
//   • a WRITER can append a "## Budget envelope\n<n> USD max" block to a child
//     issue description (e.g. via a `create-child --budget <usd>` helper);
//   • a PARSER (parseBudgetEnvelope / runMetadataFromIssue) reads it back.
// The missing hop is the adapter reading the *child issue's description* at
// spawn time (a GET /issues/{ctx.runId} with ctx.authToken, then
// runMetadataFromIssue(ctx.runId, description) → env.ROUTER_BUDGET_ENVELOPE_USD).
// That lives in the adapter's execute.js spawn flow, whose async context isn't
// verifiable from this repo — it needs a live task to wire safely, so it's left
// for a runtime-coupled follow-up. When it lands it simply overrides the
// container default for issues that set a budget.

export const ROUTER_ENV_ANCHOR = "if (ctx.runId)";
export const ROUTER_ENV_MARKER = "env.ROUTER_RUN_ID = ctx.runId";

// Injected verbatim before the adapter's existing `if (ctx.runId)` env-setup
// line. Each statement is a complete single-line `if`, so the original anchor
// statement that follows is untouched. 4-space indent matches the surrounding
// adapter code (and the sibling PAPERCLIP_API_KEY / HERMES_DB_PATH injections).
const INJECTION =
  "if (ctx.runId) env.ROUTER_RUN_ID = ctx.runId;\n" +
  "    if (process.env.ROUTER_BUDGET_ENVELOPE_USD) env.ROUTER_BUDGET_ENVELOPE_USD = process.env.ROUTER_BUDGET_ENVELOPE_USD;\n" +
  "    if (ctx.runId)";

/**
 * Inject ROUTER_RUN_ID + ROUTER_BUDGET_ENVELOPE_USD env assignments at the
 * adapter's `if (ctx.runId)` env-setup anchor. Idempotent (no-op if the marker
 * is already present) and safe (no-op if the anchor is absent). Pure.
 * @param {string} src  contents of the adapter's execute.js
 * @returns {{src: string, injected: number, found: boolean}}
 */
export function injectRouterRunEnv(src) {
  const found = src.includes(ROUTER_ENV_ANCHOR);
  if (src.includes(ROUTER_ENV_MARKER)) return { src, injected: 0, found };
  if (!found) return { src, injected: 0, found: false };
  // Replace only the FIRST anchor occurrence. A function replacer ensures any
  // `$` in INJECTION is never interpreted as a replacement-pattern token.
  return { src: src.replace(ROUTER_ENV_ANCHOR, () => INJECTION), injected: 1, found: true };
}
