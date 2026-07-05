#!/usr/bin/env node
/**
 * AzureAgentForge sandbox seam — build-time wiring of the sandbox execution
 * boundary into the hermes-paperclip-adapter's execute.js spawn path.
 *
 * The adapter spawns one child process per task. This transform routes that
 * spawn through createSandbox(process.env.SANDBOX_PROVIDER), which returns the
 * LocalSandbox (today's EXACT behavior) unless SANDBOX_PROVIDER=aca-job. Because
 * the default provider is `local`, the wired path is byte-equivalent to the
 * original spawn at runtime unless an operator opts in — and `aca-job` is not
 * enabled in any environment.
 *
 * Pure + exported so it is unit-tested offline (tests/sandbox/wiring-transform.test.mjs).
 * patch-adapter.mjs imports injectSandboxSeam() and applies it to execute.js at
 * build time, logging LOUD and no-op'ing if the anchor isn't found (consistent
 * with the existing `[patch-adapter]` warnings). The anchor below is the
 * documented default spawn line; it is reconciled with the real execute.js at
 * integration time (build log confirms `Sandbox seam wired`) before `aca-job`
 * is ever enabled.
 */

const ANCHOR = "const child = spawn(bin, argv, spawnOpts);";
const MARK = "/* AAF sandbox seam */";

/**
 * Route the adapter's child spawn through the sandbox seam, gated so the
 * default (`local`) path is unchanged. Idempotent (no-op if the marker is
 * already present) and safe (no-op if the anchor is absent). Pure.
 * @param {string} src  contents of the adapter's execute.js
 * @returns {{src: string, applied: boolean}}
 */
export function injectSandboxSeam(src) {
  if (src.includes(MARK) || !src.includes(ANCHOR)) return { src, applied: false };
  const replacement =
    `${MARK} const __sb = (await import('/server-prod/sandbox.mjs')).createSandbox();\n` +
    `const child = __sb.provider === 'local' ? spawn(bin, argv, spawnOpts) : __sb;`;
  // Function replacer so any `$` in the replacement is never treated as a
  // replacement-pattern token.
  return { src: src.replace(ANCHOR, () => replacement), applied: true };
}

export { ANCHOR as SANDBOX_SEAM_ANCHOR, MARK as SANDBOX_SEAM_MARK };
