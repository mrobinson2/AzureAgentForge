#!/usr/bin/env node
/**
 * patch-plugin-worker-manager.mjs
 *
 * Extends the env-var allow-list in PaperClip's plugin-worker-manager so the
 * paperclip-plugin-discord worker can read deployment-specific config from
 * process.env. The current upstream behaviour (server/src/services/plugin-worker-manager.ts
 * ~line 612) is explicitly minimal:
 *
 *     // Security: Do NOT spread process.env into the worker. Plugins should
 *     // only receive a minimal, controlled environment to prevent leaking host
 *     // secrets (like DATABASE_URL, internal API keys, etc.).
 *     const workerEnv: Record<string, string> = {
 *       ...options.env,
 *       PATH: process.env.PATH ?? "",
 *       NODE_PATH: process.env.NODE_PATH ?? "",
 *       PAPERCLIP_PLUGIN_ID: pluginId,
 *       NODE_ENV: process.env.NODE_ENV ?? "production",
 *       TZ: process.env.TZ ?? "UTC",
 *     };
 *
 * We respect that intent — secrets stay file-mounted under /secrets/<name>
 * (the Phase 1B discord plugin reads /secrets/discord-bot-token directly,
 * bypassing both the env-var path and the platform secret-bindings flow).
 * What we add here is a narrow allow-list for NON-SECRET configuration
 * values that the plugin needs at startup:
 *
 *   - WAR_ROOM_GUILD_ID, WAR_ROOM_VOICE_CHANNEL_ID
 *     (Discord IDs — not secrets, but values that drive the voice client's
 *      join target)
 *   - AZURE_VOICE_LIVE_ENDPOINT, AZURE_VOICE_LIVE_API_VERSION,
 *     AZURE_VOICE_LIVE_MODEL
 *     (Azure resource hostname + API metadata)
 *   - VOICE_PROVIDER, VOICE_ENABLE_DEEPGRAM_FALLBACK
 *     (provider selection flags)
 *   - PAPERCLIP_VOICE_ISSUE_AGENT_ID, PAPERCLIP_VOICE_ISSUE_COMPANY_ID
 *     (PaperClip UUIDs for the issue-sink path)
 *
 * Secrets (DEEPGRAM_API_KEY, AZURE_VOICE_LIVE_API_KEY, VOICE_WEBHOOK_URL)
 * STAY OUT of the env-var allow-list. The plugin reads those from /secrets/
 * files. This preserves the security boundary upstream put in place; we're
 * only relaxing the non-secret-leakage gate, not the secret-leakage gate.
 *
 * Drop this patch when upstream lands a per-plugin env-var config mechanism
 * (e.g., a plugin manifest field declaring which env vars to pass through).
 *
 * Run at Docker build time after PaperClip source is cloned, before pnpm build.
 */

import { readFileSync, writeFileSync } from "node:fs";

const TARGET = "/app/server/src/services/plugin-worker-manager.ts";

let src = readFileSync(TARGET, "utf-8");

// Anchor on the existing workerEnv block. We replace it with the same block
// plus the additional deployment-specific non-secret env var pass-throughs.
const OLD = `    const workerEnv: Record<string, string> = {
      ...options.env,
      PATH: process.env.PATH ?? "",
      NODE_PATH: process.env.NODE_PATH ?? "",
      PAPERCLIP_PLUGIN_ID: pluginId,
      NODE_ENV: process.env.NODE_ENV ?? "production",
      TZ: process.env.TZ ?? "UTC",
    };`;

const NEW = `    // [AAF PATCH — patch-plugin-worker-manager.mjs] Extended allow-list
    // for paperclip-plugin-discord. Secrets stay file-mounted under
    // /secrets/<name>; only NON-SECRET config values pass through env.
    // See the patch script header for the rationale + drop conditions.
    const PLUGIN_ENV_PASSTHROUGH = [
      "WAR_ROOM_GUILD_ID",
      "WAR_ROOM_VOICE_CHANNEL_ID",
      "VOICE_PROVIDER",
      "VOICE_ENABLE_DEEPGRAM_FALLBACK",
      "AZURE_VOICE_LIVE_ENDPOINT",
      "AZURE_VOICE_LIVE_API_VERSION",
      "AZURE_VOICE_LIVE_MODEL",
      "PAPERCLIP_VOICE_ISSUE_AGENT_ID",
      "PAPERCLIP_VOICE_ISSUE_COMPANY_ID",
    ];
    const extraEnv: Record<string, string> = {};
    for (const k of PLUGIN_ENV_PASSTHROUGH) {
      const v = process.env[k];
      if (typeof v === "string" && v.length > 0) extraEnv[k] = v;
    }

    const workerEnv: Record<string, string> = {
      ...options.env,
      PATH: process.env.PATH ?? "",
      NODE_PATH: process.env.NODE_PATH ?? "",
      PAPERCLIP_PLUGIN_ID: pluginId,
      NODE_ENV: process.env.NODE_ENV ?? "production",
      TZ: process.env.TZ ?? "UTC",
      ...extraEnv,
    };`;

if (src.includes("[AAF PATCH — patch-plugin-worker-manager.mjs]")) {
  console.log("[patch-plugin-worker-manager] skipped: already patched (idempotent re-run)");
} else if (src.includes(OLD)) {
  src = src.replace(OLD, NEW);
  writeFileSync(TARGET, src);
  console.log("[patch-plugin-worker-manager] applied: env-var allow-list extended");
} else {
  console.error(
    "[patch-plugin-worker-manager] FAIL: cannot locate the workerEnv block anchor. " +
      "Upstream may have restructured plugin-worker-manager.ts. Manual review needed.",
  );
  process.exit(1);
}
