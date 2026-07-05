#!/bin/sh
# Paperclip Docker entrypoint — Azure Container Apps variant
# Mirrors the upstream docker-entrypoint.sh behaviour but adds Azure-specific
# config initialisation from environment variables / secret mounts.

set -e

# ── Ensure data directory is owned by the node user ───────────────────────────
# ACA Azure File Share mounts as root; gosu lets us drop privileges cleanly.
PAPERCLIP_HOME="${PAPERCLIP_HOME:-/paperclip}"
mkdir -p "${PAPERCLIP_HOME}/instances/default"
chown -R node:node "${PAPERCLIP_HOME}" 2>/dev/null || true

# ── Per-agent workspace dir: redirect to /tmp (real POSIX) ────────────────────
# Agents write scratch files under /paperclip/instances/<id>/workspaces/<agent-id>/.
# That tree lives on Azure File Share (SMB), where mount-time mode/uid options
# are immutable per-share — chmod/chown are no-ops, so a Node process running
# as `node` can hit EACCES even though the path nominally exists.
#
# When PAPERCLIP_WORKSPACES_TMPFS=1, swap the workspace base dir for a symlink
# into /tmp (tmpfs, full POSIX). Workspace contents are ephemeral scratch so
# loss on container restart is acceptable. Real persistence already lives
# elsewhere: comments → PaperClip DB, code → git, secrets → KV.
#
# Default off so this lands without changing behavior; flip to "1" via Terraform
# (env block on ca-paperclip) once verified end-to-end.
if [ "${PAPERCLIP_WORKSPACES_TMPFS:-0}" = "1" ]; then
  WS_INSTANCE="${PAPERCLIP_INSTANCE_ID:-default}"
  WS_REAL="/tmp/paperclip-workspaces"
  WS_LINK="${PAPERCLIP_HOME}/instances/${WS_INSTANCE}/workspaces"
  mkdir -p "${WS_REAL}"
  chmod 1777 "${WS_REAL}"
  mkdir -p "$(dirname "${WS_LINK}")"
  # If an existing real (non-symlink) workspaces dir survived from a prior boot
  # under the SMB-only path, drop it. Contents were inaccessible anyway.
  if [ -d "${WS_LINK}" ] && [ ! -L "${WS_LINK}" ]; then
    rm -rf "${WS_LINK}" 2>/dev/null || true
  fi
  if [ ! -L "${WS_LINK}" ]; then
    ln -sf "${WS_REAL}" "${WS_LINK}"
  fi
  echo "[entrypoint] Workspaces redirected: ${WS_LINK} -> ${WS_REAL} (PAPERCLIP_WORKSPACES_TMPFS=1)"
else
  echo "[entrypoint] Workspaces on persistent SMB mount (PAPERCLIP_WORKSPACES_TMPFS not set; agents must default file writes to /tmp/)"
fi

# ── Write config.json from env vars if it doesn't already exist ───────────────
CONFIG_FILE="${PAPERCLIP_HOME}/instances/default/config.json"
if [ ! -f "${CONFIG_FILE}" ]; then
  cat > "${CONFIG_FILE}" <<EOF
{
  "instanceId": "${PAPERCLIP_INSTANCE_ID:-default}",
  "publicUrl": "${PAPERCLIP_PUBLIC_URL:-http://localhost:3100}",
  "allowedHostnames": "${PAPERCLIP_ALLOWED_HOSTNAMES:-localhost}",
  "deploymentMode": "${PAPERCLIP_DEPLOYMENT_MODE:-authenticated}",
  "deploymentExposure": "${PAPERCLIP_DEPLOYMENT_EXPOSURE:-private}"
}
EOF
  echo "[entrypoint] Initialised Paperclip config at ${CONFIG_FILE}"
fi

# Sync env-var-controlled fields into an existing config.
# The file is only created above on first start, so env changes on subsequent
# restarts (e.g. after az containerapp update) would otherwise be silently ignored.
python3 - "${CONFIG_FILE}" <<'PYEOF'
import json, os, sys
path = sys.argv[1]
try:
    with open(path) as f:
        cfg = json.load(f)
    changed = False
    for key, env_var in [
        ("publicUrl",          "PAPERCLIP_PUBLIC_URL"),
        ("allowedHostnames",   "PAPERCLIP_ALLOWED_HOSTNAMES"),
        ("deploymentMode",     "PAPERCLIP_DEPLOYMENT_MODE"),
        ("deploymentExposure", "PAPERCLIP_DEPLOYMENT_EXPOSURE"),
    ]:
        val = os.environ.get(env_var)
        if not val:
            continue
        if key == "allowedHostnames":
            # Parse comma-separated list; write only the first valid hostname.
            # The board-mutation-guard does a direct string comparison against
            # this value — a comma-joined string is treated as one hostname and
            # never matches a real Host header.  Use a single canonical hostname
            # (the public URL hostname).  Update Terraform to pass one value.
            parsed = [h.strip() for h in val.split(",") if h.strip()]
            new_val = parsed[0] if parsed else val
            if len(parsed) > 1:
                print("[entrypoint] WARN: PAPERCLIP_ALLOWED_HOSTNAMES has "
                      + str(len(parsed)) + " hostnames; writing first value only ("
                      + new_val + "). Update Terraform var to a single hostname.",
                      file=sys.stderr)
        else:
            new_val = val
        if cfg.get(key) != new_val:
            cfg[key] = new_val
            changed = True
            print("[entrypoint]   config.json: " + key + " -> " + new_val, file=sys.stderr)
    if changed:
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)
        print("[entrypoint] Synced Paperclip config.json from env vars", file=sys.stderr)
except Exception as e:
    print("[entrypoint] WARNING: could not sync config: " + str(e), file=sys.stderr)
PYEOF

# ── Redirect Hermes SQLite DBs to /tmp (real filesystem, no SMB locking) ─────
# Azure File Share (SMB) does not support the POSIX byte-range locks that
# SQLite requires. Redirect the session-search DB to /tmp so it uses the
# container's local filesystem instead of the SMB mount.
export HERMES_STATE_DB_PATH=/tmp/hermes-state.db

# Clean up per-session working DB files from previous container run.
# /tmp persists across sessions within one container lifetime but is cleared
# on restart/redeploy. This sweep handles any leftovers from an unclean exit.
rm -f /tmp/hermes-*.db 2>/dev/null || true

# ── Fix GWS CLI config permissions ───────────────────────────────────────────
# Azure File Share (SMB) always shows 0777 which can't be chmod'd. The gws CLI
# warns about this for its encryption key. Fix: copy the config to /tmp (real
# filesystem) where chmod works, then point GWS_CONFIG_DIR there.
GWS_PERSISTENT="/paperclip/.config/gws"
GWS_LOCAL="/tmp/gws-config"
if [ -d "${GWS_PERSISTENT}" ]; then
  mkdir -p "${GWS_LOCAL}"
  cp -r "${GWS_PERSISTENT}/." "${GWS_LOCAL}/"
  chmod 700 "${GWS_LOCAL}"
  chmod 600 "${GWS_LOCAL}/.encryption_key" 2>/dev/null || true
  chmod 600 "${GWS_LOCAL}/token_cache.json" 2>/dev/null || true
  chown -R node:node "${GWS_LOCAL}" 2>/dev/null || true
  export GOOGLE_WORKSPACE_CLI_CONFIG_DIR="${GWS_LOCAL}"
  echo "[entrypoint] GWS config copied to ${GWS_LOCAL} with correct permissions"
fi

# ── Sync Hermes skills from image to persistent volume ──────────────────────
# Skills are baked into the image at /opt/hermes-skills (built-in) and
# /opt/hermes-optional-skills (optional). Copy any new/updated skills to the
# persistent volume so agents can access them via skill_view().
SKILLS_SRC="/opt/hermes-skills"
OPT_SKILLS_SRC="/opt/hermes-optional-skills"
SKILLS_DST="${HERMES_HOME:-/paperclip/.hermes}/skills"
mkdir -p "${SKILLS_DST}"

if [ -d "${SKILLS_SRC}" ]; then
  cp -rn "${SKILLS_SRC}/." "${SKILLS_DST}/" 2>/dev/null || cp -r "${SKILLS_SRC}/." "${SKILLS_DST}/"
  echo "[entrypoint] Synced built-in Hermes skills to ${SKILLS_DST}"
fi

if [ -d "${OPT_SKILLS_SRC}" ]; then
  cp -rn "${OPT_SKILLS_SRC}/." "${SKILLS_DST}/" 2>/dev/null || cp -r "${OPT_SKILLS_SRC}/." "${SKILLS_DST}/"
  echo "[entrypoint] Synced optional Hermes skills to ${SKILLS_DST}"
fi

# Honour the .deleted marker: skills deleted via the PaperClip Skills UI are
# recorded here so they don't reappear after an image-based sync.
DELETED_FILE="${SKILLS_DST}/.deleted"
if [ -f "${DELETED_FILE}" ]; then
  while IFS= read -r skill_path; do
    if [ -n "$skill_path" ] && [ -d "${SKILLS_DST}/${skill_path}" ]; then
      rm -rf "${SKILLS_DST}/${skill_path}"
      echo "[entrypoint] Removed deleted skill: ${skill_path}"
    fi
  done < "${DELETED_FILE}"
fi

chown -R node:node "${SKILLS_DST}" 2>/dev/null || true

# ── Copy build-time manifests to a well-known location for the Skills API ───
MANIFESTS_DIR="${HERMES_HOME:-/paperclip/.hermes}/manifests"
mkdir -p "${MANIFESTS_DIR}"
cp /opt/hermes-skills-manifest.json "${MANIFESTS_DIR}/skills-manifest.json" 2>/dev/null || true
cp /opt/hermes-agent-skill-mapping.json "${MANIFESTS_DIR}/agent-skill-mapping.json" 2>/dev/null || true
chown -R node:node "${MANIFESTS_DIR}" 2>/dev/null || true
echo "[entrypoint] Skills manifests available at ${MANIFESTS_DIR}"

# ── Write Hermes config.yaml so the CLI uses the model router ────────────────
# This is the ONLY reliable way to control Hermes's provider routing.
# Env vars (HERMES_INFERENCE_PROVIDER) and adapter patches don't fully work
# because the CLI's own model detection overrides them for known model names.
# config.yaml provider has priority #2 in resolve_requested_provider() — after
# explicit --provider CLI arg, but before env vars and auto-detection.
HERMES_CONFIG_DIR="${HERMES_HOME:-/paperclip/.hermes}"
HERMES_CONFIG="${HERMES_CONFIG_DIR}/config.yaml"
mkdir -p "${HERMES_CONFIG_DIR}"
# Hermes' Anthropic transport (api_mode: anthropic_messages) uses the
# Anthropic SDK, which appends "/v1/messages" to base_url. So base_url here
# is the bare scheme://host with NO "/v1" suffix — including it would yield
# "/v1/v1/messages" 404s (the documented double-/v1 bug, see Hermes
# run_agent.py:2664). The matching server route is "POST /v1/messages" on
# the model router. OPENAI_BASE_URL is preserved for the legacy
# /chat/completions path; HERMES_BASE_URL overrides anthropic_messages base.
HERMES_BASE_URL_DEFAULT="${HERMES_BASE_URL:-${OPENAI_BASE_URL:-http://ca-hermes-dev/v1}}"
HERMES_ANTHROPIC_BASE_URL="${HERMES_ANTHROPIC_BASE_URL:-$(echo "${HERMES_BASE_URL_DEFAULT}" | sed 's|/v1/*$||')}"
cat > "${HERMES_CONFIG}" <<HERMES_EOF
# Auto-generated by docker-entrypoint.sh — do not edit manually.
# Routes all model requests through the model router sidecar on ca-hermes-dev,
# which forwards to Anthropic. The model name in Paperclip's agent config is
# passed through as-is.
#
# api_mode: anthropic_messages tells Hermes to POST to <base_url>/v1/messages
# using the Anthropic-native body shape instead of OpenAI /chat/completions.
# This is the ONLY path that lets Hermes' _anthropic_prompt_cache_policy()
# inject cache_control markers — otherwise every turn re-bills the full
# ~10k-token system prefix even though the router would happily pass cache
# headers through. The matching /v1/messages route lives on the model router.
#
# prompt_caching.cache_ttl: 1h asks Hermes to use the
# "extended-cache-ttl-2025-04-11" Anthropic beta (1-hour TTL on the cache
# breakpoints). The 5m default still helps within a single agent run but
# evaporates between issues; 1h spans most operator workflows.
model:
  provider: custom
  base_url: ${HERMES_ANTHROPIC_BASE_URL}
  api_mode: anthropic_messages
prompt_caching:
  cache_ttl: 1h
HERMES_EOF
chown node:node "${HERMES_CONFIG}" 2>/dev/null || true
echo "[entrypoint] Wrote Hermes config at ${HERMES_CONFIG} (base_url=${OPENAI_BASE_URL:-http://ca-hermes-dev/v1})"

# ── JWT Auth Proxy for automation API access ─────────────────────────────────
# When PAPERCLIP_AUTOMATION_JWT_SECRET is set, the auth proxy sits on port 3100
# (the public port) and forwards to Paperclip on port 3099 (internal).
# Browser traffic passes through transparently. JWT bearer tokens are validated
# and translated to session cookies for Paperclip.
if [ -n "${PAPERCLIP_AUTOMATION_JWT_SECRET:-}" ]; then
  # Move Paperclip to internal port 3099 so the auth proxy can own port 3100
  export PAPERCLIP_INTERNAL_PORT=3099
  ORIGINAL_PORT="${PORT:-3100}"
  export PORT="${PAPERCLIP_INTERNAL_PORT}"

  echo "[entrypoint] Auth proxy enabled — Paperclip on :${PAPERCLIP_INTERNAL_PORT}, proxy on :${ORIGINAL_PORT}"

  # Start Paperclip server in background on the internal port
  gosu node "$@" &
  PAPERCLIP_PID=$!
  echo "[entrypoint] Paperclip server started (PID $PAPERCLIP_PID) on :${PAPERCLIP_INTERNAL_PORT}"

  # Wait for Paperclip to be ready
  echo "[entrypoint] Waiting for Paperclip backend..."
  for i in $(seq 1 60); do
    if wget -q --spider "http://127.0.0.1:${PAPERCLIP_INTERNAL_PORT}/api/health" 2>/dev/null; then
      echo "[entrypoint] Paperclip backend ready"
      break
    fi
    if [ $i -eq 60 ]; then
      echo "[entrypoint] WARNING: Paperclip backend not ready after 60s, starting proxy anyway"
    fi
    sleep 1
  done

  # ── Self-heal: re-enable the Discord plugin if a restart left it stopped ─────
  # A fresh paperclip revision boots with installed plugins NOT running
  # ("plugin-loader: no ready plugins to load"); nothing re-spawns the Discord
  # Gateway worker, so every deploy silently takes Discord offline. This
  # background task waits for the auth proxy, then re-enables the plugin via a
  # short-lived automation JWT IF it isn't already running. Idempotent and
  # fail-open (never affects serving). Toggle with DISCORD_PLUGIN_SELFHEAL_ENABLED=0.
  if [ "${DISCORD_PLUGIN_SELFHEAL_ENABLED:-1}" = "1" ] && [ -f /app/discord-plugin-selfheal.mjs ]; then
    ( SELFHEAL_BASE_URL="http://127.0.0.1:${ORIGINAL_PORT}" gosu node node /app/discord-plugin-selfheal.mjs || true ) &
    echo "[entrypoint] Discord plugin self-heal scheduled (background)"
  fi

  # Start auth proxy on the original public port (foreground)
  export PORT="${ORIGINAL_PORT}"
  exec gosu node node /app/auth-proxy.mjs
else
  echo "[entrypoint] Auth proxy disabled (PAPERCLIP_AUTOMATION_JWT_SECRET not set)"
  # ── Drop to node user and exec the server directly ─────────────────────────
  exec gosu node "$@"
fi
