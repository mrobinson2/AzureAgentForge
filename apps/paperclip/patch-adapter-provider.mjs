/**
 * Provider-pin transform — force `--provider custom` in the hermes-paperclip
 * adapter's spawn args (pure, unit-tested).
 *
 * Ported from upstream private deployment incident learnings (2026-07):
 * Hermes v0.18.x's name-based provider auto-resolution matches env-credential
 * providers BEFORE config.yaml's custom provider. A model name with a known
 * vendor prefix resolved to a direct vendor endpoint that rejects Hermes's
 * `thinking` request argument (HTTP 400, non-retryable) → zero-tool runs →
 * runs misclassified as plan_only → issues blocked on missing_disposition.
 *
 * Merely OMITTING --provider (the previous patch behavior) is insufficient;
 * an explicit `--provider custom` outranks auto-resolution and pins every
 * agent-run model call to config.yaml's router (base_url + api_key),
 * restoring budget and fallback governance for ALL models.
 *
 * The transform targets the upstream conditional:
 *
 *   if (resolvedProvider !== "auto") { args.push("--provider", resolvedProvider); }
 *
 * and replaces it with an unconditional push of "custom". The same shape
 * exists in the npm dist (execute.js) and the vendored workspace TypeScript
 * source (src/server/execute.ts) — BOTH must be patched, because the
 * PaperClip 2026.707.x server loads the WORKSPACE adapter through the tsx
 * loader at runtime; patching only the npm dist leaves the live path
 * unpatched (that exact gap shipped the incident).
 *
 * Pure string-in/string-out so tests/paperclip/provider-transform.test.mjs
 * can exercise it offline. The caller (patch-adapter.mjs) is responsible for
 * failing the build loudly when `applied` and `alreadyPatched` are both
 * false — a silent skip ships an image whose agent runs bypass the router.
 */

/** Idempotency marker embedded in the replacement. */
export const PROVIDER_MARKER = "[aaf-patched provider-custom]";

/**
 * The upstream conditional that passes the auto-resolved provider to the CLI.
 * Regex (not literal) to tolerate whitespace/indentation variation between
 * the compiled dist and the TypeScript source.
 */
export const PROVIDER_PUSH_PATTERN =
  /if\s*\(resolvedProvider\s*!==\s*"auto"\)\s*\{[^}]*args\.push\(\s*"--provider"\s*,\s*resolvedProvider\s*\)\s*;[^}]*\}/s;

/**
 * Replace the conditional provider push with an unconditional
 * `--provider custom`.
 *
 * @param {string} src adapter source (dist execute.js or workspace execute.ts)
 * @returns {{applied: boolean, alreadyPatched: boolean, src: string}}
 *   applied=true  → src contains the patched push;
 *   alreadyPatched=true → marker already present, src returned unchanged;
 *   both false    → anchor not found, src returned unchanged (caller must
 *                   treat this as a fatal build error).
 */
export function forceProviderCustom(src) {
  if (src.includes(PROVIDER_MARKER)) {
    return { applied: false, alreadyPatched: true, src };
  }
  if (!PROVIDER_PUSH_PATTERN.test(src)) {
    return { applied: false, alreadyPatched: false, src };
  }
  const replacement =
    `args.push("--provider", "custom"); /* ${PROVIDER_MARKER} force custom: route via config.yaml router; name-based auto-resolution picked a direct vendor endpoint (thinking 400) */\n` +
    '    // Original: if (resolvedProvider !== "auto") { args.push("--provider", resolvedProvider); }';
  return {
    applied: true,
    alreadyPatched: false,
    src: src.replace(PROVIDER_PUSH_PATTERN, replacement),
  };
}
