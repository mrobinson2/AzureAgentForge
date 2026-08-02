#!/usr/bin/env node
/**
 * patch-metadata-fastpath.mjs — ZERO-LLM metadata fastpath.
 *
 * PROBLEM: every heartbeat wake — including ones asking the platform a pure
 * metadata question about itself ("what LLM are you running", "is this
 * subscription or metered billing", "how long did that run take") — pays
 * for a full agent spawn (LLM call + subprocess) before an answer is posted.
 * The backend already knows agent.adapterConfig.model, the issue's own
 * fields, and the run's own timestamps; none of that needs an LLM.
 *
 * THE PATCH (dark by default, single-variable rollback):
 *   In `executeRun` (server/src/services/heartbeat.ts), once the run has been
 *   claimed and both the agent and the issue's execution context
 *   (title/description) are loaded, check
 *   PAPERCLIP_METADATA_FASTPATH=1 -> classifyMetadataQuestion(title, desc).
 *   On a match: post a deterministic markdown comment (built by
 *   metadata-fastpath.mjs, ZERO LLM calls), PATCH the issue to `done`, mark
 *   the run `succeeded` and the wakeup `completed`, log
 *   `[metadata-fastpath] answered <issueId> kind=<kind> zero-llm`, release
 *   the issue execution lock, and RETURN — skipping every remaining line of
 *   executeRun (adapter resolution, workspace setup, the actual subprocess
 *   spawn). Any failure inside the fastpath itself is caught and falls
 *   through to the normal LLM-backed path — the fastpath can only ever save
 *   work, never block it.
 *
 *   Unset (the default): behavior is byte-identical to unpatched
 *   heartbeat.ts — the `if` is simply never entered.
 *
 * WHY THIS ANCHOR: `issueId` and `issueContext` (which carries `title` and
 * `description`) are both freshly resolved here, before the auto-checkout
 * branch and everything downstream that exists only to prepare an adapter
 * spawn. It's the earliest point in executeRun where classification is
 * possible with real data, which maximizes the LLM calls (and subprocess
 * spawns) this patch actually prevents.
 *
 * The runtime classify/answer logic lives in the sibling, pure,
 * dependency-free apps/paperclip/metadata-fastpath.mjs — imported by the
 * patched TS source as `./metadata-fastpath.mjs`. Because tsc (NodeNext,
 * strict) cannot type-check a plain .mjs sibling with no declaration file
 * (TS7016 "Could not find a declaration file"), this script also writes a
 * matching metadata-fastpath.d.mts stub next to heartbeat.ts so the build's
 * `tsc` step passes cleanly. The real runtime .mjs file does not need to
 * exist at compile time — it only needs to land in the compiled
 * server/dist/services/ output before `pnpm --filter @paperclipai/server
 * deploy --prod` packages the deploy bundle, which services/paperclip/
 * Dockerfile handles as a separate COPY step placed after the tsc build.
 *
 * Applies at build time to the TypeScript source, before `pnpm --filter
 * @paperclipai/server build` — the same stage/timing as this repo's other
 * server/src/*.ts patches (patch-plugin-host.mjs, patch-plugin-secrets-
 * handler.mjs, patch-plugin-worker-manager.mjs), and strictly before the
 * tsc build: editing the .ts source after compilation would be a silent
 * no-op.
 *
 * Idempotent. FATAL (exit 1) if either anchor can't be located — better a
 * loud build break than a silently un-patched image.
 */
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

// Idempotency + local-variable-name marker embedded in the inserted block.
// Deliberately namespaced like this repo's other patch markers (see
// patch-adapter-provider.mjs's PROVIDER_MARKER) rather than reusing any
// upstream vendor's convention.
export const MARKER = "__aafMetadataFastpath";

const TARGET = "/app/server/src/services/heartbeat.ts";

// ── Anchor 1: local import block. Insert the fastpath's import right after
// the existing issues-service import — a short, unique, single-line anchor
// unlikely to be reformatted independently of a real refactor.
const IMPORT_ANCHOR = `import { issueService } from "./issues.js";`;
const IMPORT_INSERT = `import { classifyMetadataQuestion, buildMetadataAnswer } from "./metadata-fastpath.mjs"; // [aaf-patched ${MARKER}]`;

// ── Anchor 2: the point in executeRun where `issueId` and `issueContext`
// (title/description) are both first resolved, before the auto-checkout
// branch and everything that exists purely to prepare an adapter spawn.
const LOGIC_ANCHOR = `    const issueId = readNonEmptyString(context.issueId);
    let issueContext = issueId ? await getIssueExecutionContext(agent.companyId, issueId) : null;`;

const LOGIC_INSERT = `
    // [aaf-patched ${MARKER}] ZERO-LLM platform-metadata fastpath. Dark
    // unless PAPERCLIP_METADATA_FASTPATH=1. See apps/paperclip/
    // metadata-fastpath.mjs + patch-metadata-fastpath.mjs.
    if (process.env.PAPERCLIP_METADATA_FASTPATH === "1" && issueId && issueContext) {
      const ${MARKER}Match = classifyMetadataQuestion(issueContext.title, issueContext.description);
      if (${MARKER}Match) {
        try {
          const ${MARKER}RequestedModel =
            typeof agent.adapterConfig?.model === "string" && agent.adapterConfig.model
              ? agent.adapterConfig.model
              : "unknown";
          const ${MARKER}Answer = buildMetadataAnswer({
            kind: ${MARKER}Match.kind,
            agentName: agent.name,
            requestedModel: ${MARKER}RequestedModel,
            routeNote: process.env.METADATA_FASTPATH_ROUTE_NOTE || undefined,
            issueKey: issueContext.identifier ?? issueId,
          });
          await issuesSvc.addComment(issueId, ${MARKER}Answer, { agentId: agent.id, runId });
          await issuesSvc.update(issueId, { status: "done" });
          await setRunStatus(runId, "succeeded", { finishedAt: new Date() });
          await setWakeupStatus(run.wakeupRequestId, "completed", { finishedAt: new Date() });
          logger.info(
            { issueId, kind: ${MARKER}Match.kind, agentId: agent.id, runId },
            \`[metadata-fastpath] answered \${issueId} kind=\${${MARKER}Match.kind} zero-llm\`,
          );
          const ${MARKER}FinishedRun = await getRun(runId);
          if (${MARKER}FinishedRun) await releaseIssueExecutionAndPromote(${MARKER}FinishedRun);
          return;
        } catch (err) {
          // Never let the fastpath itself break a real run — fall through to
          // the normal LLM-backed execution path on any failure.
          logger.error(
            { err, issueId, runId },
            "[metadata-fastpath] failed, falling back to normal execution",
          );
        }
      }
    }
`;

/**
 * Pure transform so tests can exercise the patch without touching the image.
 * Returns { applied, alreadyPatched, src, reason?, found? } — matches the
 * shape used elsewhere in this directory (see patch-adapter-provider.mjs)
 * so callers apply the same "applied || alreadyPatched, else fatal"
 * convention. Never throws.
 */
export function applyMetadataFastpath(src) {
  if (src.includes(MARKER)) {
    return { applied: false, alreadyPatched: true, src, reason: "already applied" };
  }

  const importMatches = src.split(IMPORT_ANCHOR).length - 1;
  if (importMatches === 0) {
    return { applied: false, alreadyPatched: false, src, reason: "import anchor not found", found: 0 };
  }
  if (importMatches !== 1) {
    return {
      applied: false,
      alreadyPatched: false,
      src,
      reason: "import anchor matched more than once",
      found: importMatches,
    };
  }

  const logicMatches = src.split(LOGIC_ANCHOR).length - 1;
  if (logicMatches === 0) {
    return { applied: false, alreadyPatched: false, src, reason: "logic anchor not found", found: 0 };
  }
  if (logicMatches !== 1) {
    return {
      applied: false,
      alreadyPatched: false,
      src,
      reason: "logic anchor matched more than once",
      found: logicMatches,
    };
  }

  let next = src.replace(IMPORT_ANCHOR, `${IMPORT_ANCHOR}\n${IMPORT_INSERT}`);
  next = next.replace(LOGIC_ANCHOR, `${LOGIC_ANCHOR}\n${LOGIC_INSERT}`);
  return { applied: true, alreadyPatched: false, src: next, reason: "ok" };
}

// Hand-written type declaration for the plain-JS sibling module. NodeNext
// module resolution looks for `<name>.d.mts` when type-checking a `.mjs`
// import specifier — verified empirically: without this file, `tsc` fails
// the build with TS7016 ("Could not find a declaration file"); with it
// present (even though the real .mjs need not exist yet at compile time),
// the build passes and the emitted JS keeps a real ESM `import` statement.
const DECLARATION_FILENAME = "metadata-fastpath.d.mts";
const DECLARATION_CONTENTS = `export function classifyMetadataQuestion(
  title: string | null | undefined,
  description: string | null | undefined,
): { kind: "model_identity" | "billing_route" | "run_provenance" | "duration" } | null;

export function buildMetadataAnswer(input: {
  kind: "model_identity" | "billing_route" | "run_provenance" | "duration";
  agentName?: string | null;
  requestedModel?: string | null;
  routeNote?: string | null;
  issueKey?: string | null;
}): string;

export const DEFAULT_ROUTE_NOTE: string;
`;

function main() {
  let src;
  try {
    src = readFileSync(TARGET, "utf-8");
  } catch (err) {
    console.error(`[patch-metadata-fastpath] FATAL: cannot read ${TARGET}: ${err.message}`);
    process.exit(1);
  }

  const result = applyMetadataFastpath(src);
  if (!result.applied) {
    if (result.alreadyPatched) {
      console.log("[patch-metadata-fastpath] already applied — no-op");
      return;
    }
    console.error(`[patch-metadata-fastpath] FATAL: ${result.reason} (matches=${result.found ?? "n/a"})`);
    console.error("[patch-metadata-fastpath] Upstream reshaped executeRun's imports or issue-context setup —");
    console.error("[patch-metadata-fastpath] re-check both anchors before shipping.");
    process.exit(1);
  }

  writeFileSync(TARGET, result.src);

  const declarationPath = path.join(path.dirname(TARGET), DECLARATION_FILENAME);
  writeFileSync(declarationPath, DECLARATION_CONTENTS);

  console.log(
    "[patch-metadata-fastpath] applied: PAPERCLIP_METADATA_FASTPATH=1 now short-circuits metadata-question wakes with zero LLM calls",
  );
  console.log(`[patch-metadata-fastpath] wrote type declaration: ${declarationPath}`);
}

const isMain = import.meta.url === `file://${process.argv[1]}`;
if (isMain) {
  main();
}
