#!/usr/bin/env node
/**
 * Patch hermes-paperclip-adapter at Docker build time.
 *
 * Fixes three upstream issues:
 *   1. gpt-4* model prefix maps to openai-codex (breaks custom endpoints)
 *   2. ctx.authToken is ignored (agents can't auth with Paperclip API)
 *   3. Prompt template lacks task-completion guardrails and uses fragile
 *      curl|python one-liners that gpt-4o-mini can't reproduce
 *
 * Run this AFTER pnpm install / pnpm deploy so the adapter files exist.
 */

import { readFileSync, writeFileSync } from "node:fs";

// AzureAgentForge cost-envelope — per-run env injection (pure, unit-tested).
import { injectRouterRunEnv } from "./patch-adapter-router-env.mjs";

// pnpm uses a content-addressable store — the REAL files live under .pnpm/,
// not at the symlinked path. We must find the actual location at build time.
import { realpathSync } from "node:fs";
const ADAPTER_SYMLINK = "/server-prod/node_modules/hermes-paperclip-adapter/dist";
const ADAPTER_ROOT = realpathSync(ADAPTER_SYMLINK);
console.log(`[patch-adapter] Resolved adapter root: ${ADAPTER_ROOT}`);

// ── Fix 1: Provider hints — route all Azure AI Foundry models to "auto" ──────
// The adapter infers providers from model name prefixes and passes --provider
// to the CLI, overriding HERMES_INFERENCE_PROVIDER=custom. Since ALL our models
// are on the same Azure AI Foundry endpoint, set every problematic prefix to
// "auto" so the env var takes effect.
const constantsPath = `${ADAPTER_ROOT}/shared/constants.js`;
let constants = readFileSync(constantsPath, "utf-8");
const prefixFixes = [
  ['["gpt-4", "openai-codex"]',    '["gpt-4", "auto"]'],    // gpt-4o-mini
  ['["gpt-5", "copilot"]',         '["gpt-5", "auto"]'],    // gpt-5.1-chat, gpt-5-nano, gpt-5.4-nano, gpt-5.1-codex-mini
  ['["o1-", "openai-codex"]',      '["o1-", "auto"]'],      // defensive
  ['["o3-", "openai-codex"]',      '["o3-", "auto"]'],      // defensive
  ['["o4-", "openai-codex"]',      '["o4-", "auto"]'],      // defensive
  ['["kimi", "kimi-coding"]',      '["kimi", "auto"]'],     // Kimi-K2.5
  ['["moonshot", "kimi-coding"]',  '["moonshot", "auto"]'], // moonshot/kimi aliases
];
for (const [from, to] of prefixFixes) {
  if (constants.includes(from)) {
    constants = constants.replace(from, to);
    console.log(`[patch-adapter] Fixed prefix hint: ${from} → ${to}`);
  } else {
    console.warn(`[patch-adapter] WARN: prefix hint not found (adapter may have changed): ${from}`);
  }
}
writeFileSync(constantsPath, constants);
console.log("[patch-adapter] Provider hints patched");

// ── Fix 2: Inject ctx.authToken as PAPERCLIP_API_KEY env var ────────────────
const executePath = `${ADAPTER_ROOT}/server/execute.js`;
let execute = readFileSync(executePath, "utf-8");

// Inject ctx.authToken → PAPERCLIP_API_KEY and add a unique per-session HERMES_DB_PATH.
// Both are injected at the same anchor ("if (ctx.runId)") for atomicity.
// Idempotent: detect which injections are already present (Docker layer cache)
// and only apply what is missing.
//
// adapter v0.3.0 no longer sets env.HERMES_DB_PATH, so we must ADD it rather
// than replace an existing line. Without it Hermes defaults to $HERMES_HOME/state.db
// (Azure File Share) which causes SMB advisory-lock "database is locked" errors.
if (!execute.includes("if (ctx.runId)")) {
  console.warn("[patch-adapter] WARN: 'if (ctx.runId)' anchor not found — env injections skipped");
} else {
  const hasApiKey = execute.includes("env.PAPERCLIP_API_KEY = ctx.authToken");
  const hasDbPath = execute.includes("HERMES_DB_PATH = '/tmp/hermes-'");

  if (!hasApiKey && !hasDbPath) {
    execute = execute.replace(
      "if (ctx.runId)",
      "if (ctx.authToken) env.PAPERCLIP_API_KEY = ctx.authToken;\n    env.HERMES_DB_PATH = '/tmp/hermes-' + (ctx.runId || Date.now()) + '.db';\n    if (ctx.runId)"
    );
    console.log("[patch-adapter] Injected PAPERCLIP_API_KEY + HERMES_DB_PATH (unique per session)");
  } else if (hasApiKey && !hasDbPath) {
    execute = execute.replace(
      "if (ctx.runId)",
      "env.HERMES_DB_PATH = '/tmp/hermes-' + (ctx.runId || Date.now()) + '.db';\n    if (ctx.runId)"
    );
    console.log("[patch-adapter] PAPERCLIP_API_KEY present; added HERMES_DB_PATH (unique per session)");
  } else {
    console.log("[patch-adapter] Both env injections already present (cached layer) — no-op");
  }
}

// AzureAgentForge cost-envelope — forward the per-run id + budget ceiling to the
// spawned Hermes (ROUTER_RUN_ID = ctx.runId; ROUTER_BUDGET_ENVELOPE_USD from the
// container env). patch-hermes-cost-envelope.mjs then relays them into the
// model-router request metadata. Inert unless COST_ENVELOPE_ENABLED=1 on the
// router. Idempotent; injected at the same `if (ctx.runId)` env-setup anchor.
{
  const _re = injectRouterRunEnv(execute);
  if (_re.injected) {
    execute = _re.src;
    console.log("[patch-adapter] Injected ROUTER_RUN_ID + ROUTER_BUDGET_ENVELOPE_USD (§0.7 cost-envelope)");
  } else if (execute.includes("env.ROUTER_RUN_ID = ctx.runId")) {
    console.log("[patch-adapter] §0.7 cost-envelope env already present (cached layer) — no-op");
  } else {
    console.warn("[patch-adapter] WARN: 'if (ctx.runId)' anchor not found — §0.7 cost-envelope env NOT injected");
  }
}

// Force provider to "auto" — NEVER pass --provider to the CLI.
// resolvedProvider is a const (can't reassign), so instead replace the
// conditional that pushes --provider with one that always skips it.
// This lets config.yaml's model.provider=custom take full effect in the CLI.
// Use regex to handle any whitespace/indentation variation.
const providerPushPattern = /if\s*\(resolvedProvider\s*!==\s*"auto"\)\s*\{[^}]*args\.push\(\s*"--provider"\s*,\s*resolvedProvider\s*\)\s*;[^}]*\}/s;
if (providerPushPattern.test(execute)) {
  execute = execute.replace(
    providerPushPattern,
    '/* [patched] --provider disabled — config.yaml controls routing */\n    // Original: if (resolvedProvider !== "auto") { args.push("--provider", resolvedProvider); }'
  );
  console.log("[patch-adapter] Disabled --provider CLI flag");
} else {
  // FATAL: if we can't remove --provider, the adapter will override config.yaml
  console.error("[patch-adapter] FATAL: Could not find args.push('--provider') pattern in execute.js!");
  console.error("[patch-adapter] Dumping lines containing 'resolvedProvider' or '--provider':");
  execute.split("\n").forEach((line, i) => {
    if (line.includes("resolvedProvider") || line.includes("--provider")) {
      console.error(`  Line ${i + 1}: ${line.trimEnd()}`);
    }
  });
  process.exit(1);
}

// ── Fix 3: Replace the default prompt template ─────────────────────────────
// The upstream template has fragile curl|python one-liners and no task
// completion guardrails. Replace with a cleaner version.

// Build the template as a plain string to avoid JS template literal escaping issues
// with backslashes, backticks, and curl line continuations.
const NEW_PROMPT = [
  'You are "{{agentName}}", an AI agent employee in a Paperclip-managed company.',
  '',
  '## API Access Rules',
  '',
  'IMPORTANT: $PAPERCLIP_API_KEY is a shell environment variable. Use it in curl as $PAPERCLIP_API_KEY.',
  'Do NOT try to read it from a file. Do NOT use the browser tool. Use ONLY curl in the terminal.',
  '',
  'Every curl command to the Paperclip API MUST include these headers:',
  '  -H "Authorization: Bearer $PAPERCLIP_API_KEY" -H "Origin: http://localhost:3100" -H "Content-Type: application/json"',
  '',
  'API Base: {{paperclipApiUrl}}',
  '',
  '## API URL Patterns (CRITICAL - read carefully)',
  '',
  'The Paperclip API uses ISSUE IDENTIFIERS (like MRT-11), NOT UUIDs, in URL paths:',
  '  - Get issue details: GET {{paperclipApiUrl}}/issues/MRT-11',
  '  - Update issue status: PATCH {{paperclipApiUrl}}/issues/MRT-11',
  '  - Post comment: POST {{paperclipApiUrl}}/issues/MRT-11/comments',
  '  - List issues: GET {{paperclipApiUrl}}/companies/{{companyId}}/issues?assigneeAgentId={{agentId}}',
  '',
  'When listing issues, each issue has an "identifier" field (e.g. "MRT-11"). Use THAT in URLs, not the "id" field (UUID).',
  'There is NO /close endpoint. To mark done, use PATCH with {"status":"done"}.',
  '',
  'Your identity:',
  '  Agent ID: {{agentId}}',
  '  Company ID: {{companyId}}',
  '',
  '{{#taskId}}',
  '## Assigned Task',
  '',
  'Issue: {{taskId}}',
  'Title: {{taskTitle}}',
  '',
  '{{taskBody}}',
  '',
  '## How to Complete This Task',
  '',
  '1. Read and understand the task. Determine if it is:',
  '   - A QUESTION (requires you to provide an answer)',
  '   - An ACTION (requires you to do something)',
  '',
  '2. Do the actual work.',
  '   - If QUESTION: formulate a clear, accurate answer using your knowledge.',
  '   - If ACTION: perform the action using your tools. Verify it succeeded.',
  '   - NEVER skip this step. NEVER pretend to do work you did not do.',
  '',
  '3. Post your result as a comment (field MUST be "body"):',
  '   curl -s -X POST "{{paperclipApiUrl}}/issues/{{taskId}}/comments" -H "Authorization: Bearer $PAPERCLIP_API_KEY" -H "Origin: http://localhost:3100" -H "Content-Type: application/json" -d \'{"body":"YOUR ACTUAL ANSWER HERE"}\'',
  '',
  '4. Mark done ONLY after posting a meaningful comment:',
  '   curl -s -X PATCH "{{paperclipApiUrl}}/issues/{{taskId}}" -H "Authorization: Bearer $PAPERCLIP_API_KEY" -H "Origin: http://localhost:3100" -H "Content-Type: application/json" -d \'{"status":"done"}\'',
  '',
  '## Rules',
  '- NEVER mark done without posting a comment containing real work output.',
  '- NEVER post generic comments like "completed" or "done".',
  '- If a task asks a question, your comment MUST contain the answer.',
  '- If a command fails, read the error and fix it. Do not ignore errors.',
  '- The comment JSON field is "body" (not "content", not "comment").',
  '{{/taskId}}',
  '',
  '{{#commentId}}',
  '## Comment on This Issue',
  'Someone commented on issue {{taskId}}. Read it:',
  '  curl -s "{{paperclipApiUrl}}/issues/{{taskId}}/comments/{{commentId}}" -H "Authorization: Bearer $PAPERCLIP_API_KEY" -H "Origin: http://localhost:3100"',
  'Address the comment, POST a reply if needed, then continue working.',
  '{{/commentId}}',
  '',
  '{{#noTask}}',
  '## Heartbeat - Check for Work',
  '',
  '1. List open issues assigned to you:',
  '   curl -s "{{paperclipApiUrl}}/companies/{{companyId}}/issues?assigneeAgentId={{agentId}}" -H "Authorization: Bearer $PAPERCLIP_API_KEY" -H "Origin: http://localhost:3100"',
  '',
  '2. Look at the JSON response. Find issues where "status" is NOT "done" or "cancelled".',
  '   Each issue has an "identifier" field (e.g. "MRT-11") - use THAT in API URLs, not the "id" UUID.',
  '',
  '3. Pick the highest-priority open issue and DO THE WORK:',
  '   a. Read the issue title and body to understand what is needed.',
  '   b. If it is a QUESTION, answer it. If it is an ACTION, do it.',
  '   c. Post your answer/result as a comment:',
  '      curl -s -X POST "{{paperclipApiUrl}}/issues/IDENTIFIER/comments" -H "Authorization: Bearer $PAPERCLIP_API_KEY" -H "Origin: http://localhost:3100" -H "Content-Type: application/json" -d \'{"body":"your answer here"}\'',
  '   d. Mark the issue as done:',
  '      curl -s -X PATCH "{{paperclipApiUrl}}/issues/IDENTIFIER" -H "Authorization: Bearer $PAPERCLIP_API_KEY" -H "Origin: http://localhost:3100" -H "Content-Type: application/json" -d \'{"status":"done"}\'',
  '',
  '4. If no issues assigned, check unassigned backlog:',
  '   curl -s "{{paperclipApiUrl}}/companies/{{companyId}}/issues?status=backlog" -H "Authorization: Bearer $PAPERCLIP_API_KEY" -H "Origin: http://localhost:3100"',
  '',
  '5. If nothing to do, report what you checked.',
  '{{/noTask}}',
].join("\n");

// Replace the DEFAULT_PROMPT_TEMPLATE in execute.js
// The template is defined as: const DEFAULT_PROMPT_TEMPLATE = `...`;
const templateStart = execute.indexOf("const DEFAULT_PROMPT_TEMPLATE = `");
if (templateStart === -1) {
  console.error("[patch-adapter] ERROR: Could not find DEFAULT_PROMPT_TEMPLATE in execute.js");
  process.exit(1);
}

// Find the closing backtick — it's the first unescaped backtick after the opening
let depth = 0;
let templateEnd = -1;
for (let i = templateStart + "const DEFAULT_PROMPT_TEMPLATE = `".length; i < execute.length; i++) {
  if (execute[i] === "\\" && execute[i + 1] === "`") {
    i++; // skip escaped backtick
    continue;
  }
  if (execute[i] === "`") {
    templateEnd = i + 1; // include the closing backtick
    break;
  }
}

if (templateEnd === -1) {
  console.error("[patch-adapter] ERROR: Could not find end of DEFAULT_PROMPT_TEMPLATE");
  process.exit(1);
}

const oldTemplate = execute.slice(templateStart, templateEnd);
const newTemplateDecl = "const DEFAULT_PROMPT_TEMPLATE = `" + NEW_PROMPT + "`";

execute = execute.slice(0, templateStart) + newTemplateDecl + execute.slice(templateEnd);
console.log(`[patch-adapter] Replaced prompt template (${oldTemplate.length} → ${newTemplateDecl.length} chars)`);

// ── Fix 4: Inject agent instructions file loading ─────────────────────────
// PaperClip stores per-agent instructions as AGENTS.md files on disk.
// The adapter's buildPrompt() doesn't read them. Patch it to check
// config.instructionsFilePath, read the file, and prepend it to the prompt.
// This gives each agent its own personality/instructions + the shared
// task-execution template.

// Add fs import at the top of execute.js (readFileSync + existsSync)
const fsImportPatch = 'import { readFileSync, existsSync } from "node:fs";\n';
if (!execute.includes('from "node:fs"')) {
  // Insert after the last import line
  const lastImportIdx = execute.lastIndexOf("import ");
  const lineEnd = execute.indexOf("\n", lastImportIdx);
  execute = execute.slice(0, lineEnd + 1) + fsImportPatch + execute.slice(lineEnd + 1);
  console.log("[patch-adapter] Added node:fs import for instructions loading");
} else {
  console.log("[patch-adapter] node:fs import already present");
}

// Patch buildPrompt to prepend instructions file content.
// Find the function and inject instructions loading before the return.
// We look for the unique pattern where conditional sections are processed,
// specifically the last replace call, followed by return.
const instructionsPatch = `
    // [patched] Load per-agent instructions from AGENTS.md on disk
    if (config.instructionsFilePath && existsSync(config.instructionsFilePath)) {
      try {
        const instructions = readFileSync(config.instructionsFilePath, "utf-8").trim();
        if (instructions) {
          rendered = "# Agent Instructions\\n\\n" + instructions + "\\n\\n---\\n\\n" + rendered;
        }
      } catch (e) {
        // Non-fatal — agent runs without custom instructions
        console.warn("[hermes-adapter] Failed to read instructions:", e.message);
      }
    }
`;

// Find the return statement in buildPrompt — it calls renderTemplate(rendered, vars)
// Match either "return renderTemplate(rendered, vars);" or "return rendered;"
const returnPattern = /(\s+)(return renderTemplate\(rendered,\s*vars\);|return rendered;)/;
const returnMatch = execute.match(returnPattern);
if (returnMatch) {
  execute = execute.replace(
    returnPattern,
    instructionsPatch + returnMatch[1] + returnMatch[2]
  );
  console.log("[patch-adapter] Injected instructions file loading into buildPrompt()");
} else {
  console.error("[patch-adapter] WARN: Could not find return statement in buildPrompt — instructions loading skipped");
}

writeFileSync(executePath, execute);
console.log("[patch-adapter] All patches applied successfully");
