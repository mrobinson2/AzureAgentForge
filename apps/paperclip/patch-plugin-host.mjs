#!/usr/bin/env node
/**
 * patch-plugin-host.mjs
 *
 * Fixes a PaperClip plugin SDK gap where `agents.sessions.sendMessage({ prompt })`
 * never delivers the prompt to the agent. heartbeat.wakeup's `payload.prompt` is
 * not read by enrichWakeContextSnapshot; the hermes-paperclip-adapter reads
 * prompts from issue bodies via contextSnapshot.issueId.
 *
 * Workaround: in sendMessage, materialize a backing issue with the prompt as
 * body, assigned to the session's agent. Pass `issueId` in the wakeup payload
 * so contextSnapshot.issueId gets set and the adapter delivers the prompt
 * through the existing verified assignee-wake path.
 *
 * Same fix applied to agents.invoke, which has the identical bug.
 *
 * Run at Docker build time after PaperClip source is cloned, before pnpm build.
 */

import { readFileSync, writeFileSync } from "node:fs";

const TARGET = "/app/server/src/services/plugin-host-services.ts";

let src = readFileSync(TARGET, "utf-8");

// ── Patch 1: Add `issues` import from @paperclipai/db ────────────────────────
// Conditional: from PAPERCLIP_VERSION=v2026.428.0+ upstream already imports
// `issues as issuesTable` itself (as part of a multi-line import block), so
// this patch becomes a no-op. The fatal error only fires if neither shape
// is present, which would mean upstream restructured the import beyond
// recognition — flag for manual review rather than silently ship a broken
// build. See docs/research/paperclip-version-upgrade-analysis.md §3.
const IMPORT_OLD = `import { pluginLogs, agentTaskSessions as agentTaskSessionsTable } from "@paperclipai/db";`;
const IMPORT_NEW = `import { pluginLogs, agentTaskSessions as agentTaskSessionsTable, issues as issuesTable } from "@paperclipai/db";`;

if (src.includes(IMPORT_OLD)) {
  src = src.replace(IMPORT_OLD, IMPORT_NEW);
  console.log("[patch-plugin-host] Patch 1 applied: import line rewritten");
} else if (src.includes("issues as issuesTable")) {
  console.log("[patch-plugin-host] Patch 1 skipped: upstream already imports issuesTable");
} else {
  console.error("[patch-plugin-host] FAIL: cannot locate issuesTable import in any known shape. Upstream may have restructured imports; manual review needed.");
  process.exit(1);
}

// ── Patch 2: sendMessage — create backing issue, pass issueId in wakeup ─────
const SEND_OLD = `        if (!session) throw new Error(\`Session not found: \${params.sessionId}\`);

        const run = await heartbeat.wakeup(session.agentId, {
          source: "automation",
          triggerDetail: "system",
          reason: params.reason ?? null,
          payload: { prompt: params.prompt },
          contextSnapshot: {
            taskKey: session.taskKey,
            wakeSource: "automation",
            wakeTriggerDetail: "system",
          },
          requestedByActorType: "system",
          requestedByActorId: pluginId,
        });`;

const SEND_NEW = `        if (!session) throw new Error(\`Session not found: \${params.sessionId}\`);

        // [AAF PATCH — patch-plugin-host.mjs] Create backing issue so the
        // agent has a real task. heartbeat.wakeup's payload.prompt is never
        // read; the adapter reads prompts from issue bodies via
        // contextSnapshot.issueId. Direct DB insert (no service call) to skip
        // identifier generation and avoid issue.created activity log entry —
        // we don't want plugin event echoes for these ephemeral backing issues.
        const [backingIssue] = await db
          .insert(issuesTable)
          .values({
            companyId,
            title: params.prompt.slice(0, 80),
            description: params.prompt,
            status: "todo",
            assigneeAgentId: session.agentId,
            originKind: "plugin_session",
            originId: session.id,
          })
          .returning();

        const run = await heartbeat.wakeup(session.agentId, {
          source: "automation",
          triggerDetail: "system",
          reason: params.reason ?? null,
          payload: { issueId: backingIssue.id, prompt: params.prompt },
          contextSnapshot: {
            taskKey: session.taskKey,
            issueId: backingIssue.id,
            wakeSource: "automation",
            wakeTriggerDetail: "system",
          },
          requestedByActorType: "system",
          requestedByActorId: pluginId,
        });`;

if (!src.includes(SEND_OLD)) {
  console.error("[patch-plugin-host] FAIL: sendMessage block not found. Upstream may have changed.");
  process.exit(1);
}
src = src.replace(SEND_OLD, SEND_NEW);

// ── Patch 3: agents.invoke — same fix (it has the same bug) ──────────────────
const INVOKE_OLD = `        const run = await heartbeat.wakeup(params.agentId, {
          source: "automation",
          triggerDetail: "system",
          reason: params.reason ?? null,
          payload: { prompt: params.prompt },
          requestedByActorType: "system",
          requestedByActorId: pluginId,
        });`;

const INVOKE_NEW = `        // [AAF PATCH — patch-plugin-host.mjs] See sendMessage above for rationale.
        const [invokeBackingIssue] = await db
          .insert(issuesTable)
          .values({
            companyId,
            title: params.prompt.slice(0, 80),
            description: params.prompt,
            status: "todo",
            assigneeAgentId: params.agentId,
            originKind: "plugin_invoke",
          })
          .returning();
        const run = await heartbeat.wakeup(params.agentId, {
          source: "automation",
          triggerDetail: "system",
          reason: params.reason ?? null,
          payload: { issueId: invokeBackingIssue.id, prompt: params.prompt },
          contextSnapshot: { issueId: invokeBackingIssue.id },
          requestedByActorType: "system",
          requestedByActorId: pluginId,
        });`;

if (!src.includes(INVOKE_OLD)) {
  console.error("[patch-plugin-host] FAIL: invoke block not found. Upstream may have changed.");
  process.exit(1);
}
src = src.replace(INVOKE_OLD, INVOKE_NEW);

writeFileSync(TARGET, src);
console.log("[patch-plugin-host] Patched sendMessage + invoke to create backing issues");
