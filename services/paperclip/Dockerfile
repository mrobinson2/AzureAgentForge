# syntax=docker/dockerfile:1.7
# Paperclip AI Orchestrator — Azure Container Apps build
#
# Builds Paperclip from the official GitHub source at a pinned release tag,
# then patches in the hermes-paperclip-adapter so Hermes runs as a managed
# employee inside the Paperclip company.
#
# Authentication secrets are never baked in — they are injected at runtime
# via Azure Key Vault secret volume mounts and environment variables.
#
# Port: 3100 (Paperclip default)
# Data: /paperclip (mount an Azure File Share here for persistence)

ARG PAPERCLIP_VERSION=v2026.517.0
# Guard against git-tag drift: the build fails if the cloned tag resolves to a
# commit other than this. Resolve with:
#   git ls-remote https://github.com/paperclipai/paperclip refs/tags/<tag>
ARG PAPERCLIP_EXPECTED_SHA=3e6610fb938d04638fa578a1fc0d119b434fa2e4
ARG NODE_VERSION=lts-trixie-slim

# ── Stage 0: upstream-style base image ────────────────────────────────────────
FROM node:${NODE_VERSION} AS base

ARG USER_UID=1000
ARG USER_GID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    ca-certificates gosu curl git wget ripgrep python3 \
 && mkdir -p -m 755 /etc/apt/keyrings \
 && wget -nv -O /etc/apt/keyrings/githubcli-archive-keyring.gpg https://cli.github.com/packages/githubcli-archive-keyring.gpg \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && mkdir -p -m 755 /etc/apt/sources.list.d \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

RUN corepack enable

# Match UID/GID to host (avoids volume permission issues on Azure File Share)
RUN usermod -u ${USER_UID} --non-unique node \
 && groupmod -g ${USER_GID} --non-unique node \
 && usermod -g ${USER_GID} -d /paperclip node

# ── Stage 1: clone + install deps ─────────────────────────────────────────────
FROM base AS deps

ARG PAPERCLIP_VERSION
ARG PAPERCLIP_EXPECTED_SHA

WORKDIR /app

# Clone the pinned release tag, then verify the resolved commit matches the
# expected SHA. A git tag is not immutable; this fails the build on tag drift.
# CACHE_BUST invalidates Docker layer cache so a version change actually takes effect.
ARG CACHE_BUST=2026-05-23
RUN echo "cache-bust=${CACHE_BUST}" \
 && git clone --depth=1 --branch ${PAPERCLIP_VERSION} \
    https://github.com/paperclipai/paperclip.git . \
 && ACTUAL_SHA="$(git rev-parse HEAD)" \
 && { [ -z "${PAPERCLIP_EXPECTED_SHA}" ] || [ "${ACTUAL_SHA}" = "${PAPERCLIP_EXPECTED_SHA}" ] || \
      { echo "ERROR: paperclip ${PAPERCLIP_VERSION} resolved to ${ACTUAL_SHA}," \
             "expected ${PAPERCLIP_EXPECTED_SHA} (tag drift — verify upstream and update the pin)" >&2; \
        exit 1; }; }

# Install hermes-paperclip-adapter into the server workspace before
# dependency resolution so pnpm can hoist it correctly
RUN cd server && npm pkg set dependencies.hermes-paperclip-adapter="latest"

RUN pnpm install --frozen-lockfile || pnpm install --no-frozen-lockfile

# ── Stage 2: patch adapter + build ────────────────────────────────────────────
FROM deps AS build

# (patch-registry.mjs deleted as part of v2026.517.0 bump — upstream
# registers hermes-paperclip-adapter natively since v2026.403.0;
# the patch had been a build-time no-op for the entire lifetime of
# this pin. See docs/research/paperclip-version-upgrade-analysis.md §3.)

# Fix plugin SDK gap: sessions.sendMessage/agents.invoke don't deliver prompt to agent.
# Patch plugin-host-services.ts to materialize a backing issue per call so the existing
# assignee-wake path delivers the prompt via issue body.
COPY apps/paperclip/patch-plugin-host.mjs /tmp/patch-plugin-host.mjs
RUN node /tmp/patch-plugin-host.mjs

# Restore pre-gate plugin secret-refs resolver. A later upstream change
# ("fail closed plugin secret refs") replaced the entire
# createPluginSecretsHandler.resolve() body with an unconditional throw,
# breaking every plugin that resolves a secret-ref at init (e.g. the
# discord plugin's discordBotTokenRef). The stated rationale is
# cross-tenancy isolation, which doesn't apply to AzureAgentForge's single-operator
# deployment. The patch restores the pre-gate implementation verbatim
# from commit 87011615^. Drop this patch once upstream lands "company-
# scoped plugin config" and the gate's followup work.
COPY apps/paperclip/patch-plugin-secrets-handler.mjs /tmp/patch-plugin-secrets-handler.mjs
RUN node /tmp/patch-plugin-secrets-handler.mjs

# Extend plugin-worker-manager env-var allow-list for paperclip-plugin-discord.
# Secrets stay file-mounted under /secrets/<name>; this only passes through
# NON-SECRET config (Discord IDs, Azure Voice Live endpoint, etc.). See the
# patch script header for the full rationale + drop conditions.
COPY apps/paperclip/patch-plugin-worker-manager.mjs /tmp/patch-plugin-worker-manager.mjs
RUN node /tmp/patch-plugin-worker-manager.mjs

# Build all packages
RUN pnpm --filter @paperclipai/ui build
RUN pnpm --filter @paperclipai/plugin-sdk build
RUN pnpm --filter @paperclipai/server build
RUN test -f server/dist/index.js || (echo "ERROR: server build output missing" && exit 1)

# ── Stage 2b: minimal production deploy ───────────────────────────────────────
# pnpm deploy creates a self-contained deployment with only the server's
# production runtime deps and bundled workspace packages — no dev deps,
# no unrelated workspace packages, no source files.
# This reduces the layer copied into production from ~800 MB → ~150 MB.
RUN pnpm --filter @paperclipai/server deploy --prod /server-prod

# Copy compiled output (pnpm deploy does not include build artifacts)
RUN cp -r server/dist /server-prod/dist

# The compiled server imports workspace packages by their TypeScript source
# paths (e.g. @paperclipai/db/src/index.ts). tsx JIT-compiles these at
# runtime. For each @paperclipai/* in the prod bundle, find the matching
# workspace directory (at any depth) and copy its src/ tree.
RUN for pkg in /server-prod/node_modules/@paperclipai/*/; do \
      name=$(basename "$pkg"); \
      src_dir=$(find /app -path "*/node_modules" -prune -o -name "package.json" -print \
        | xargs grep -l "\"name\": \"@paperclipai/${name}\"" 2>/dev/null \
        | head -1 | xargs dirname 2>/dev/null); \
      if [ -n "$src_dir" ] && [ -d "$src_dir/src" ]; then \
        cp -r "$src_dir/src" "$pkg/src"; \
        echo "Copied src for @paperclipai/${name} from ${src_dir}"; \
      fi; \
    done

# NOTE:
# v2026.403.0 includes an upstream x-forwarded-host related fix.
# Leaving this patch commented out for now is safer than double-patching.
# If you still hit origin/host issues behind Cloudflare Tunnel or ACA ingress,
# uncomment this block and rebuild.
#
# RUN sed -i 's/const host = req.header("host")/const host = req.header("x-forwarded-host") || req.header("host")/' \
#     /server-prod/dist/middleware/board-mutation-guard.js

# Automation bypass: requests carrying X-Automation-Sub skip the browser-origin
# CSRF check. Port 3099 is not publicly exposed, so this header can only be set
# by processes running inside the container (Orchestrator via pc-delegate.sh, or the
# auth-proxy for external JWT clients). JWT-authenticated requests also carry
# this header (set by auth-proxy.mjs), so both paths are covered.
#
# Pre-check: fail the build if the expected guard string is absent (e.g. upstream
# renamed the error message).  sed returns 0 even on zero matches, so without this
# the patch would silently be a no-op and automation writes would still get 403.
RUN grep -q 'Board mutation requires trusted browser origin' \
      /server-prod/dist/middleware/board-mutation-guard.js \
    || { echo "ERROR: board-mutation-guard.js does not contain the expected CSRF guard string." \
              "The upstream error message may have changed — verify and update the sed pattern." >&2; exit 1; } \
 && sed -i '/Board mutation requires trusted browser origin/i\\    if (req.headers["x-automation-sub"]) return next();' \
    /server-prod/dist/middleware/board-mutation-guard.js

# Patch plugin-loader npm install command to add --no-bin-links.
# Azure Files SMB mount at /paperclip can't create symlinks; npm's default
# .bin/<binary> → ../node_modules/.../dist/cli.js symlinks fail with EACCES.
# Without this, `POST /api/plugins/install` always 400s with errno -13.
# Required for the PaperClip Discord plugin install step. The plugin
# runtime loads via require() from manifest.js/worker.js paths and does NOT
# depend on .bin/ entries — those are dev-tool shortcuts only. See
# docs/discord-plugin-adoption/02-integration-plan.md § 1.1 and
# upstream paperclipai/paperclip:server/src/services/plugin-loader.ts:837.
#
# Pre-check: fail the build if the expected install-args string is absent.
RUN grep -q '"--save", "--ignore-scripts"' \
      /server-prod/dist/services/plugin-loader.js \
    || { echo "ERROR: plugin-loader.js does not contain the expected npm install" \
              "args '--save', '--ignore-scripts'. Upstream may have changed the" \
              "install command; re-inspect plugin-loader.ts and update this patch." >&2; exit 1; } \
 && sed -i 's|"--save", "--ignore-scripts"|"--save", "--ignore-scripts", "--no-bin-links"|' \
    /server-prod/dist/services/plugin-loader.js

# Patch hermes-paperclip-adapter at build time.
# Fixes: provider routing (gpt-4→auto), auth token injection (ctx.authToken),
# and replaces the fragile prompt template with one that enforces real task
# completion (no generic "success" comments, no marking done without output).
COPY apps/paperclip/patch-adapter.mjs /tmp/patch-adapter.mjs
# patch-adapter.mjs imports ./patch-adapter-router-env.mjs (§0.7 cost-envelope),
# so the sibling must be present in the same dir for the node run to resolve it.
COPY apps/paperclip/patch-adapter-router-env.mjs /tmp/patch-adapter-router-env.mjs
RUN node /tmp/patch-adapter.mjs

# tsx is a devDependency but the upstream CMD uses it as a runtime loader
# for path-alias resolution. Install in a clean temp dir (npm can't parse
# pnpm workspace:* refs in server-prod/package.json) then merge all of
# tsx + its transitive deps into server-prod/node_modules.
RUN mkdir -p /tmp/tsx-pkg && cd /tmp/tsx-pkg \
 && echo '{"private":true}' > package.json \
 && npm install tsx --no-fund --no-audit \
 && cp -rn node_modules/* /server-prod/node_modules/ \
 && rm -rf /tmp/tsx-pkg

# ── Stage 2c: Hermes CLI (Python) ─────────────────────────────────────────────
# Build Hermes Agent from the local source (not PyPI).
# Installs into /opt/hermes so it's independent of a specific Python venv path.
FROM python:3.13-slim AS hermes-cli

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc git \
 && rm -rf /var/lib/apt/lists/*

COPY apps/hermes/src/ /tmp/hermes-src/

# Patch hermes source before installing — redirects SessionDB away from
# the Azure File Share (SMB) to a path set by HERMES_STATE_DB_PATH.
# Fixes "database is locked" on every session start.
COPY apps/paperclip/patch-hermes-src.py /tmp/patch-hermes-src.py
RUN python3 /tmp/patch-hermes-src.py /tmp/hermes-src

RUN pip install --no-cache-dir --prefix=/opt/hermes /tmp/hermes-src/[messaging,honcho] \
 && pip install --no-cache-dir --prefix=/opt/hermes ddgs \
 && rm -rf /tmp/hermes-src/ /tmp/patch-hermes-src.py \
 && sed -i '1s|.*|#!/usr/bin/env python3|' /opt/hermes/bin/hermes \
 && sed -i '1s|.*|#!/usr/bin/env python3|' /opt/hermes/bin/ddgs

# ── Stage 3: production image ─────────────────────────────────────────────────
FROM base AS production

ARG USER_UID=1000
ARG USER_GID=1000

WORKDIR /app

# Server: minimal production bundle (runtime deps + workspace packages bundled in)
COPY --chown=node:node --from=build /server-prod /app/server

# UI: built static assets served by the Paperclip server when SERVE_UI=true
# Server looks for ui/dist relative to its parent directory (/app/ui/dist)
COPY --chown=node:node --from=build /app/ui/dist /app/ui/dist

# Hermes CLI: copy pre-built packages from the hermes-cli stage
COPY --from=hermes-cli /opt/hermes /opt/hermes
ENV PYTHONPATH="/opt/hermes/lib/python3.13/site-packages"

RUN ln -s /opt/hermes/bin/hermes /usr/local/bin/hermes \
 && chmod +x /usr/local/bin/hermes

# ddgs wrapper — the native CLI needs a subcommand (text/news/etc.) but
# agents typically call it as `ddgs "query"`. The wrapper accepts the naive
# form and translates it to `ddgs text -k "query" -m 5 -o json`.
COPY apps/paperclip/ddgs-wrapper.sh /usr/local/bin/ddgs
RUN chmod +x /usr/local/bin/ddgs

# Brave Search wrapper — DuckDuckGo silently blocks Azure cloud IPs, so this
# is the primary web-search path. Same call shape as ddgs (accepts both naive
# and ddgs-compatible forms) and emits ddgs-compatible JSON. Requires
# BRAVE_SEARCH_API_KEY to be set in the container env (KV-backed).
COPY apps/paperclip/brave-search-wrapper.sh /usr/local/bin/brave-search
RUN chmod +x /usr/local/bin/brave-search

# Hermes skills: pip install doesn't include skills/, copy them separately.
# Placed at /opt/hermes-skills so they're baked into the image. The entrypoint
# syncs these to ~/.hermes/skills on the persistent volume.
COPY apps/hermes/src/skills /opt/hermes-skills

# Parent-repo skill overrides: overlay on top of the upstream submodule copy.
# These are platform-specific customizations (e.g. automation headers, dynamic
# helper path resolution) that must not be pushed upstream to NousResearch.
# Adding files here is the correct way to customize a built-in skill without
# forking the Hermes submodule.
COPY apps/hermes/overrides/skills /opt/hermes-skills

# Skill-helper wrappers on $PATH. The fresh-shell-per-terminal-call model
# in Hermes means env-var-resolved helper paths (e.g. $DELEGATE_HELPER) get
# lost between calls, leading to exit-127 loops. Symlinking the helper
# scripts into /usr/local/bin gives agents stable callable names.
RUN chmod +x /opt/hermes-skills/playbooks/agent-delegate/scripts/pc-delegate.sh \
 && ln -sf /opt/hermes-skills/playbooks/agent-delegate/scripts/pc-delegate.sh /usr/local/bin/pc-delegate \
 && chmod +x /opt/hermes-skills/playbooks/honcho-memory/scripts/pc-honcho.sh \
 && ln -sf /opt/hermes-skills/playbooks/honcho-memory/scripts/pc-honcho.sh /usr/local/bin/pc-honcho

# Optional skills: staged by Stage-Skills.ps1, copied alongside built-in ones.
# Kept in a separate image directory so the entrypoint can distinguish
# built-in vs optional for editability checks.
COPY apps/hermes/src/optional-skills /opt/hermes-optional-skills

# Build-time manifests (generated by Stage-Skills.ps1) for the Skills API.
# Baked into the image so the UI can show inventory without live agent calls.
COPY build/skills/skills-manifest.json /opt/hermes-skills-manifest.json
COPY build/skills/agent-skill-mapping.json /opt/hermes-agent-skill-mapping.json

RUN mkdir -p /paperclip \
 && chown node:node /paperclip

COPY apps/paperclip/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# JWT auth proxy for automation API access (automation scripts, CI/CD)
COPY apps/paperclip/auth-proxy.mjs /app/auth-proxy.mjs

# Skills management UI — self-contained HTML page served by the auth-proxy
COPY apps/paperclip/skills-ui.html /app/skills-ui.html

# ── Google Workspace CLI (gws) ───────────────────────────────────────────────
# Binary-only install. Credentials are injected at runtime via Key Vault secret mount.
RUN curl -L "https://github.com/googleworkspace/cli/releases/download/v0.22.3/google-workspace-cli-x86_64-unknown-linux-gnu.tar.gz" \
    -o /tmp/gws.tar.gz \
    && tar -xzf /tmp/gws.tar.gz --strip-components=1 -C /usr/local/bin \
    && chmod +x /usr/local/bin/gws \
    && rm /tmp/gws.tar.gz

# ── Runtime configuration ─────────────────────────────────────────────────────
# All secrets are injected via Azure Key Vault secret volume mounts.
# DATABASE_URL and PAPERCLIP_PUBLIC_URL must be set via Container App env vars.
ENV NODE_ENV=production \
    HOME=/paperclip \
    HOST=0.0.0.0 \
    PORT=3100 \
    SERVE_UI=true \
    PAPERCLIP_HOME=/paperclip \
    PAPERCLIP_INSTANCE_ID=default \
    USER_UID=${USER_UID} \
    USER_GID=${USER_GID} \
    PAPERCLIP_CONFIG=/paperclip/instances/default/config.json \
    PAPERCLIP_DEPLOYMENT_MODE=authenticated \
    PAPERCLIP_DEPLOYMENT_EXPOSURE=private \
    OPENCODE_ALLOW_ALL_MODELS=true

VOLUME ["/paperclip"]
EXPOSE 3100

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["node", "--import", "./server/node_modules/tsx/dist/loader.mjs", "server/dist/index.js"]