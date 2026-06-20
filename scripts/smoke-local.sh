#!/usr/bin/env bash
# No-credentials health gate for the local full stack. Makes NO model calls, so
# it passes without any model key. Exits non-zero on first failure.
set -uo pipefail

ROUTER_PORT="${ROUTER_PORT:-8080}"
PAPERCLIP_PORT="${PAPERCLIP_PORT:-3100}"
HONCHO_PORT="${HONCHO_PORT:-8000}"
GOVERNOR_PORT="${GOVERNOR_PORT:-8090}"
GOVERNOR_API_KEY="${GOVERNOR_API_KEY:-localdev}"
POSTGRES_USER="${POSTGRES_USER:-aaf}"
POSTGRES_DB="${POSTGRES_DB:-aaf}"
COMPOSE="${COMPOSE:-docker compose}"

fail=0
pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; fail=1; }

http_is() { # url expected label
  local code; code="$(curl -s -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || echo 000)"
  [ "$code" = "$2" ] && pass "$3 ($code)" || bad "$3 (got $code, want $2)"
}

echo "smoke-local: checking the full stack (no model key required)…"

# 1. migrations applied — feature_flags seeded
if $COMPOSE exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
     'select count(*) from feature_flags;' >/tmp/aaf_ff 2>/dev/null \
   && [ "$(tr -d '[:space:]' </tmp/aaf_ff)" -ge 1 ] 2>/dev/null; then
  pass "migrations applied (feature_flags rows: $(tr -d '[:space:]' </tmp/aaf_ff))"
else
  bad "migrations applied (feature_flags missing/empty)"
fi

# 2. model-router liveness (no creds)
http_is "http://localhost:${ROUTER_PORT}/health" 200 "model-router /health"
# 3. honcho
http_is "http://localhost:${HONCHO_PORT}/openapi.json" 200 "honcho /openapi.json"
# 4. paperclip UI
http_is "http://localhost:${PAPERCLIP_PORT}/" 200 "paperclip UI /"
# 5. governor healthy + idle. /admit is key-gated (X-Governor-Key) and pydantic-
#    validates the body before the disabled short-circuit, so send a valid body.
http_is "http://localhost:${GOVERNOR_PORT}/healthz" 200 "memory-governor /healthz"
admit="$(curl -s -X POST "http://localhost:${GOVERNOR_PORT}/admit" \
  -H "X-Governor-Key: ${GOVERNOR_API_KEY}" -H 'Content-Type: application/json' \
  -d '{"content":"smoke","workspace_name":"smoke","observer":"smoke","created_by_peer":"smoke"}' \
  2>/dev/null || echo '')"
if printf '%s' "$admit" | grep -q '"status"[[:space:]]*:[[:space:]]*"disabled"'; then
  pass "memory-governor /admit → disabled (flags off)"
else
  bad "memory-governor /admit → expected disabled, got: ${admit:-<none>}"
fi

echo
[ "$fail" -eq 0 ] && { echo "smoke-local: PASS"; exit 0; } || { echo "smoke-local: FAIL"; exit 1; }
