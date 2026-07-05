#!/usr/bin/env node
/**
 * patch-plugin-secrets-handler.mjs
 *
 * Reverts PaperClip commit 87011615 ("PAP-2394 PAP-2395 Fail closed plugin
 * secret refs", 2026-04-26) which replaced the entire `createPluginSecretsHandler`
 * implementation with an unconditional throw:
 *
 *     throw new Error(PLUGIN_SECRET_REFS_DISABLED_MESSAGE);
 *
 * The stated rationale was "fail closed until plugin config and worker
 * runtime both carry an explicit company scope for secret bindings and
 * resolution." That landed for cross-tenancy isolation reasons that don't
 * apply to AzureAgentForge's single-operator deployment — but the consequence is that
 * every plugin which resolves a secret ref during init (e.g. discord-plugin's
 * discordBotTokenRef + paperclipBoardApiKeyRef at src/worker.ts:310) fails
 * `Worker initialize failed: Plugin secret references are disabled...`,
 * leaving it stuck in `status=error` forever.
 *
 * This patch restores the pre-gate implementation verbatim from
 * paperclipai/paperclip commit 87011615^. The pre-gate code does:
 *   1. Rate-limit (30 attempts/min/plugin)
 *   2. Validate ref format (UUID shape)
 *   3. Scope-check: only allow refs declared as `format: "secret-ref"` in
 *      the plugin's instanceConfigSchema (defends against cross-plugin
 *      enumeration even on a single operator)
 *   4. Look up the secret record in company_secrets by UUID
 *   5. Delegate to secretService.resolveSecretValue() for decryption
 *
 * Drop this patch when upstream lands "company-scoped plugin config" and
 * the gate's followup work — at that point the throw will be replaced by
 * the proper implementation upstream and this patch will fail loudly at
 * Docker build (because the throw line won't be there to substitute).
 *
 * Tracking: this is the same general pattern as patch-plugin-host.mjs
 * (which patches plugin-host-services.ts for the sendMessage gap).
 *
 * Run at Docker build time after PaperClip source is cloned, before pnpm build.
 */

import { readFileSync, writeFileSync } from "node:fs";

const TARGET = "/app/server/src/services/plugin-secrets-handler.ts";

let src = readFileSync(TARGET, "utf-8");

// ── Patch 1: add the imports the restored implementation needs ──────────────
//
// Pre-gate imports: drizzle's `eq`, companySecrets + pluginConfig tables,
// pluginRegistryService, secretService. Post-gate keeps only the Db type
// import and the three json-schema-secret-refs helpers.
//
// We anchor on the existing `import type { Db } …` line and inject the
// missing four imports right after it.

const IMPORT_OLD = `import type { Db } from "@paperclipai/db";
import {
  collectSecretRefPaths,
  isUuidSecretRef,
  readConfigValueAtPath,
} from "./json-schema-secret-refs.js";`;

const IMPORT_NEW = `import { eq } from "drizzle-orm";
import type { Db } from "@paperclipai/db";
import { companySecrets, pluginConfig } from "@paperclipai/db";
import { pluginRegistryService } from "./plugin-registry.js";
import { secretService } from "./secrets.js";
import {
  collectSecretRefPaths,
  isUuidSecretRef,
  readConfigValueAtPath,
} from "./json-schema-secret-refs.js";`;

if (src.includes(IMPORT_OLD)) {
  src = src.replace(IMPORT_OLD, IMPORT_NEW);
  console.log("[patch-plugin-secrets-handler] Patch 1 applied: imports restored");
} else if (src.includes(`import { secretService } from "./secrets.js";`)) {
  console.log(
    "[patch-plugin-secrets-handler] Patch 1 skipped: imports already present (upstream may have restored?)",
  );
} else {
  console.error(
    "[patch-plugin-secrets-handler] FAIL: cannot locate import block to extend. " +
      "Upstream restructured imports beyond recognition — manual review needed.",
  );
  process.exit(1);
}

// ── Patch 2: add the secretNotFound helper ──────────────────────────────────
//
// The pre-gate code uses a `secretNotFound(ref)` helper alongside the
// existing `invalidSecretRef(ref)` helper. Post-gate removed it (since the
// gate threw before any lookup). We inject it right before `invalidSecretRef`.

const HELPER_ANCHOR = `function invalidSecretRef(secretRef: string): Error {`;
const HELPER_INJECTION = `function secretNotFound(secretRef: string): Error {
  const err = new Error(\`Secret not found: \${secretRef}\`);
  err.name = "SecretNotFoundError";
  return err;
}

function invalidSecretRef(secretRef: string): Error {`;

if (src.includes(HELPER_INJECTION)) {
  console.log(
    "[patch-plugin-secrets-handler] Patch 2 skipped: secretNotFound already present",
  );
} else if (src.includes(HELPER_ANCHOR)) {
  src = src.replace(HELPER_ANCHOR, HELPER_INJECTION);
  console.log(
    "[patch-plugin-secrets-handler] Patch 2 applied: secretNotFound helper added",
  );
} else {
  console.error(
    "[patch-plugin-secrets-handler] FAIL: cannot locate invalidSecretRef anchor. " +
      "Upstream changed the error helper layout — manual review needed.",
  );
  process.exit(1);
}

// ── Patch 3: restore the createPluginSecretsHandler implementation ──────────
//
// The post-gate body is a thin wrapper:
//   - destructure pluginId
//   - rate limiter
//   - return { resolve(...) { /* validate, then throw */ } }
//
// We replace the entire function body with the pre-gate implementation
// (verbatim from commit 87011615^). The anchor is "createPluginSecretsHandler"
// + the post-gate body shape; we substitute the full pre-gate body.

const HANDLER_OLD = `export function createPluginSecretsHandler(
  options: PluginSecretsHandlerOptions,
): PluginSecretsService {
  const { pluginId } = options;

  // Rate limit: max 30 resolution attempts per plugin per minute
  const rateLimiter = createRateLimiter(30, 60_000);

  return {
    async resolve(params: PluginSecretsResolveParams): Promise<string> {
      const { secretRef } = params;

      // ---------------------------------------------------------------
      // 0. Rate limiting — prevent brute-force UUID enumeration
      // ---------------------------------------------------------------
      if (!rateLimiter.check(pluginId)) {
        const err = new Error("Rate limit exceeded for secret resolution");
        err.name = "RateLimitExceededError";
        throw err;
      }

      // ---------------------------------------------------------------
      // 1. Validate the ref format
      // ---------------------------------------------------------------
      if (!secretRef || typeof secretRef !== "string" || secretRef.trim().length === 0) {
        throw invalidSecretRef(secretRef ?? "<empty>");
      }

      const trimmedRef = secretRef.trim();

      if (!isUuidSecretRef(trimmedRef)) {
        throw invalidSecretRef(trimmedRef);
      }

      // Fail closed until plugin config and worker runtime both carry an
      // explicit company scope for secret bindings and resolution.
      throw new Error(PLUGIN_SECRET_REFS_DISABLED_MESSAGE);
    },
  };
}`;

const HANDLER_NEW = `export function createPluginSecretsHandler(
  options: PluginSecretsHandlerOptions,
): PluginSecretsService {
  // [AAF PATCH — patch-plugin-secrets-handler.mjs] Restored pre-gate
  // implementation (paperclipai/paperclip commit 87011615^). The post-gate
  // body throws unconditionally; the discord plugin (and any plugin that
  // resolves a secret ref during init) cannot start. See the patch script
  // header for the full rationale.
  const { db, pluginId } = options;
  const registry = pluginRegistryService(db);
  const secrets = secretService(db);

  // Rate limit: max 30 resolution attempts per plugin per minute
  const rateLimiter = createRateLimiter(30, 60_000);

  let cachedAllowedRefs: Map<string, Set<string>> | null = null;
  let cachedAllowedRefsExpiry = 0;
  const CONFIG_CACHE_TTL_MS = 30_000; // 30 seconds, matches event bus TTL

  return {
    async resolve(params: PluginSecretsResolveParams): Promise<string> {
      const { secretRef } = params;

      // ---------------------------------------------------------------
      // 0. Rate limiting — prevent brute-force UUID enumeration
      // ---------------------------------------------------------------
      if (!rateLimiter.check(pluginId)) {
        const err = new Error("Rate limit exceeded for secret resolution");
        err.name = "RateLimitExceededError";
        throw err;
      }

      // ---------------------------------------------------------------
      // 1. Validate the ref format
      // ---------------------------------------------------------------
      if (!secretRef || typeof secretRef !== "string" || secretRef.trim().length === 0) {
        throw invalidSecretRef(secretRef ?? "<empty>");
      }

      const trimmedRef = secretRef.trim();

      if (!isUuidSecretRef(trimmedRef)) {
        throw invalidSecretRef(trimmedRef);
      }

      // ---------------------------------------------------------------
      // 1b. Scope check — only allow secrets referenced in this plugin's config
      // ---------------------------------------------------------------
      const now = Date.now();
      if (!cachedAllowedRefs || now > cachedAllowedRefsExpiry) {
        const [configRow, plugin] = await Promise.all([
          db
            .select()
            .from(pluginConfig)
            .where(eq(pluginConfig.pluginId, pluginId))
            .then((rows) => rows[0] ?? null),
          registry.getById(pluginId),
        ]);

        const schema = (plugin?.manifestJson as unknown as Record<string, unknown> | null)
          ?.instanceConfigSchema as Record<string, unknown> | undefined;
        cachedAllowedRefs = extractSecretRefPathsFromConfig(configRow?.configJson, schema);
        cachedAllowedRefsExpiry = now + CONFIG_CACHE_TTL_MS;
      }

      const allowedPaths = cachedAllowedRefs.get(trimmedRef);
      if (!allowedPaths) {
        // Return "not found" to avoid leaking whether the secret exists
        throw secretNotFound(trimmedRef);
      }

      // ---------------------------------------------------------------
      // 2. Look up the secret record by UUID
      // ---------------------------------------------------------------
      const secret = await db
        .select()
        .from(companySecrets)
        .where(eq(companySecrets.id, trimmedRef))
        .then((rows) => rows[0] ?? null);

      if (!secret) {
        throw secretNotFound(trimmedRef);
      }

      return await secrets.resolveSecretValue(secret.companyId, secret.id, "latest", {
        consumerType: "plugin",
        consumerId: pluginId,
        actorType: "plugin",
        actorId: pluginId,
        pluginId,
        configPath: [...allowedPaths][0] ?? "$",
      });
    },
  };
}`;

if (src.includes(HANDLER_OLD)) {
  src = src.replace(HANDLER_OLD, HANDLER_NEW);
  console.log(
    "[patch-plugin-secrets-handler] Patch 3 applied: handler body restored",
  );
} else if (src.includes("[AAF PATCH — patch-plugin-secrets-handler.mjs]")) {
  console.log(
    "[patch-plugin-secrets-handler] Patch 3 skipped: already patched (idempotent re-run)",
  );
} else if (!src.includes("PLUGIN_SECRET_REFS_DISABLED_MESSAGE")) {
  console.log(
    "[patch-plugin-secrets-handler] Patch 3 skipped: upstream removed the fail-closed throw — verify and drop this patch script",
  );
} else {
  console.error(
    "[patch-plugin-secrets-handler] FAIL: cannot locate the post-gate handler body. " +
      "Upstream modified the throw block in a way this patch doesn't recognize. " +
      "Manual review needed — see commit history of plugin-secrets-handler.ts.",
  );
  process.exit(1);
}

writeFileSync(TARGET, src);
console.log("[patch-plugin-secrets-handler] All patches written to", TARGET);
