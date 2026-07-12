#!/usr/bin/env bash
# Agent-loop canary — the e2e smoke that proves the product actually works.
#
# The health gate (scripts/smoke-local.sh) proves containers are up; it does
# NOT prove an agent can complete work. This script closes that gap with a
# canary-issue roundtrip against the full local stack + the canary overlay
# (docker-compose.canary.yml):
#
#   file issue → assignment wake → hermes_local adapter spawns the Hermes
#   runtime → model call goes through the model-router (real auth + tier
#   dispatch) → scripted stub "model" returns a terminal tool call → the REAL
#   Hermes runtime executes it → agent posts a disposition comment and marks
#   the issue done → this script asserts done + disposition + that the model
#   hop actually traversed the router.
#
# ONLY the LLM inference is stubbed (scripts/canary/anthropic_stub.py); every
# other hop is the real production code path.
#
# FAILS LOUDLY when:
#   - the agent never completes the issue (wake/adapter/model path broken)
#   - the issue lands terminal-but-not-done (e.g. cancelled)
#   - the issue is done but carries no disposition comment
#   - the issue is done but the model hop never traversed router → stub
#
# Usage:
#   docker compose -f docker-compose.yml -f docker-compose.canary.yml \
#     --profile full up -d --build
#   bash scripts/smoke-canary.sh
#
#   bash scripts/smoke-canary.sh --self-check   # offline: stub self-test +
#                                               # JWT mint check, no stack
set -uo pipefail

PAPERCLIP_PORT="${PAPERCLIP_PORT:-3100}"
ROUTER_PORT="${ROUTER_PORT:-8080}"
CANARY_STUB_PORT="${CANARY_STUB_PORT:-9785}"
CANARY_MODEL="${CANARY_MODEL:-claude-canary}"
# Deadline for the full roundtrip (wake + Hermes boot + 2 model turns + curls).
CANARY_TIMEOUT_SECONDS="${CANARY_TIMEOUT_SECONDS:-300}"
CANARY_POLL_SECONDS="${CANARY_POLL_SECONDS:-5}"
# Dev-default secrets — match docker-compose.yml / .env.example.
PAPERCLIP_AUTOMATION_JWT_SECRET="${PAPERCLIP_AUTOMATION_JWT_SECRET:-localdev-automation-secret-change-me}"
# Must match the auth-proxy's verifyJwt defaults (apps/paperclip/auth-proxy.mjs).
PAPERCLIP_AUTOMATION_JWT_ISSUER="${PAPERCLIP_AUTOMATION_JWT_ISSUER:-azureagentforge-automation}"
PAPERCLIP_AUTOMATION_JWT_AUDIENCE="${PAPERCLIP_AUTOMATION_JWT_AUDIENCE:-paperclip-api}"
# better-auth rejects TLD-less emails (admin@localhost), so the default must
# be a real email shape and must match the paperclip container's
# PAPERCLIP_ADMIN_EMAIL (the auth-proxy signs in with that account).
PAPERCLIP_ADMIN_EMAIL="${PAPERCLIP_ADMIN_EMAIL:-admin@example.com}"
PAPERCLIP_ADMIN_PASSWORD="${PAPERCLIP_ADMIN_PASSWORD:-localdev-admin-change-me}"
# In-container URL agents use for their own API calls (backend, NOT the proxy).
PAPERCLIP_INTERNAL_API_URL="${PAPERCLIP_INTERNAL_API_URL:-http://127.0.0.1:3099/api}"

BASE="http://localhost:${PAPERCLIP_PORT}"
STUB="http://127.0.0.1:${CANARY_STUB_PORT}"
ORIGIN="http://localhost:${PAPERCLIP_PORT}"
COMPANY_NAME="${CANARY_COMPANY_NAME:-AAF Canary}"
AGENT_NAME="${CANARY_AGENT_NAME:-Canary}"
MARKER="AAF-CANARY-ROUNDTRIP"
DISPOSITION_TAG="[aaf-canary] disposition:"

fail=0
pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; fail=1; }
die()  { bad "$1"; summary_and_exit; }

MODE="roundtrip"
summary_and_exit() {
  echo
  if [ "$fail" -eq 0 ]; then
    if [ "$MODE" = "self-check" ]; then
      echo "smoke-canary: PASS (self-check only — no live roundtrip was run)"
    else
      echo "smoke-canary: PASS — agent-loop roundtrip verified (model hop stubbed, loop real)"
    fi
    exit 0
  fi
  echo "smoke-canary: FAIL"
  echo "  Debug: docker compose -f docker-compose.yml -f docker-compose.canary.yml --profile full logs paperclip model-router canary-stub"
  [ -n "${STUB_STATE:-}" ] && echo "  Stub counters at exit: ${STUB_STATE}"
  exit 1
}

# ── Offline self-check mode ──────────────────────────────────────────────────
if [ "${1:-}" = "--self-check" ]; then
  MODE="self-check"
  echo "smoke-canary: self-check (offline — no stack required)…"
  if python3 "$(dirname "$0")/canary/anthropic_stub.py" --self-test; then
    pass "stub model scripted-turn self-test"
  else
    bad "stub model scripted-turn self-test"
  fi
  if python3 - "$PAPERCLIP_AUTOMATION_JWT_SECRET" <<'PY'
import base64, hashlib, hmac, json, sys, time
secret = sys.argv[1]
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
now = int(time.time())
p = b64(json.dumps({"sub": "aaf-canary", "role": "automation", "scope": ["*"],
                    "iss": "automation-agent", "aud": "paperclip-api",
                    "iat": now, "exp": now + 60}).encode())
sig = b64(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
token = f"{h}.{p}.{sig}"
assert token.count(".") == 2 and len(token) > 60
PY
  then pass "automation JWT mint"; else bad "automation JWT mint"; fi
  summary_and_exit
fi

command -v curl >/dev/null || { echo "curl required"; exit 1; }
command -v python3 >/dev/null || { echo "python3 required"; exit 1; }

echo "smoke-canary: e2e agent-loop roundtrip (stub model backend, real loop)…"

# ── Helpers ──────────────────────────────────────────────────────────────────

mint_jwt() {
  python3 - "$PAPERCLIP_AUTOMATION_JWT_SECRET" \
    "$PAPERCLIP_AUTOMATION_JWT_ISSUER" "$PAPERCLIP_AUTOMATION_JWT_AUDIENCE" <<'PY'
import base64, hashlib, hmac, json, sys, time
secret, iss, aud = sys.argv[1], sys.argv[2], sys.argv[3]
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
now = int(time.time())
# scope ["*"]: the canary exercises company/agent/issue/comment surfaces; "*"
# satisfies both mapped scopes and the proxy's unmapped-path fallback.
p = b64(json.dumps({"sub": "aaf-canary", "role": "automation", "scope": ["*"],
                    "iss": iss, "aud": aud,
                    "iat": now, "exp": now + 1800}).encode())
sig = b64(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
print(f"{h}.{p}.{sig}")
PY
}

# api METHOD PATH [JSON_BODY] → body on stdout; HTTP code in API_CODE
API_CODE=""
api() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "$method" "${BASE}${path}"
    -H "Authorization: Bearer ${JWT}"
    -H "Origin: ${ORIGIN}"
    -H "Content-Type: application/json"
    -o /tmp/aaf_canary_resp -w '%{http_code}')
  [ -n "$body" ] && args+=(-d "$body")
  API_CODE="$(curl "${args[@]}" 2>/dev/null || echo 000)"
  cat /tmp/aaf_canary_resp 2>/dev/null
}

# jget JSON PYEXPR → evaluates PYEXPR with `d` bound to parsed JSON
jget() {
  python3 -c '
import json, sys
try:
    d = json.loads(sys.argv[1] or "null")
except Exception:
    d = None
try:
    v = eval(sys.argv[2])
except Exception:
    v = ""
print(v if v is not None else "")' "$1" "$2" 2>/dev/null
}

wait_http() { # url label deadline_s
  local url="$1" label="$2" deadline=$(( $(date +%s) + ${3:-90} )) code
  while :; do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo 000)"
    [ "$code" = "200" ] && { pass "$label reachable"; return 0; }
    [ "$(date +%s)" -ge "$deadline" ] && { bad "$label unreachable (last code $code)"; return 1; }
    sleep 2
  done
}

# ── 0. Preconditions ─────────────────────────────────────────────────────────
wait_http "${BASE}/api/auth/proxy-health" "paperclip auth-proxy" 120 || summary_and_exit
wait_http "${STUB}/health" "canary stub" 60 || summary_and_exit
router_health="$(curl -s "http://localhost:${ROUTER_PORT}/health" 2>/dev/null || echo '')"
if printf '%s' "$router_health" | grep -q "\"${CANARY_MODEL}\""; then
  pass "router registered canary tier '${CANARY_MODEL}'"
else
  die "router /health does not list tier '${CANARY_MODEL}' — was docker-compose.canary.yml applied? (got: ${router_health:-<none>})"
fi

# ── 1. Admin sign-up (first user claims instance admin; idempotent) ─────────
signup_code="$(curl -sS -X POST "${BASE}/api/auth/sign-up/email" \
  -H "Content-Type: application/json" -H "Origin: ${ORIGIN}" \
  -d "{\"name\":\"Admin\",\"email\":\"${PAPERCLIP_ADMIN_EMAIL}\",\"password\":\"${PAPERCLIP_ADMIN_PASSWORD}\"}" \
  -o /tmp/aaf_canary_signup -w '%{http_code}' 2>/dev/null || echo 000)"
case "$signup_code" in
  2*) pass "admin user created (${PAPERCLIP_ADMIN_EMAIL})" ;;
  4*)
    if grep -qi "exist" /tmp/aaf_canary_signup 2>/dev/null; then
      pass "admin user already exists"
    else
      die "admin sign-up failed (${signup_code}): $(head -c 300 /tmp/aaf_canary_signup)"
    fi ;;
  *) die "admin sign-up failed (${signup_code}): $(head -c 300 /tmp/aaf_canary_signup 2>/dev/null)" ;;
esac

# ── 2. Automation JWT works end-to-end through the proxy ────────────────────
JWT="$(mint_jwt)"
whoami_resp="$(api GET /api/auth/whoami)"
if [ "$API_CODE" = "200" ] && printf '%s' "$whoami_resp" | grep -q '"jwt"'; then
  pass "automation JWT accepted by auth-proxy"
else
  die "automation JWT rejected (${API_CODE}): $(printf '%s' "$whoami_resp" | head -c 300)"
fi

# ── 2b. Claim first instance admin (idempotent) ──────────────────────────────
# Sign-up alone does NOT grant instance admin; the first browser-session user
# claims it via POST /api/bootstrap/claim (authenticated+private mode). The
# JWT proxy path rides the bootstrapped admin session, so the claim lands on
# our admin user. 409 = already claimed (fine on re-runs).
claim="$(api POST /api/bootstrap/claim '{}')"
case "$API_CODE" in
  200) pass "instance admin claimed" ;;
  409) pass "instance admin already claimed" ;;
  *) die "instance-admin claim failed (${API_CODE}): $(printf '%s' "$claim" | head -c 300)" ;;
esac

# ── 3. Find-or-create canary company ─────────────────────────────────────────
companies="$(api GET /api/companies)"
[ "$API_CODE" = "200" ] || die "list companies failed (${API_CODE}): $(printf '%s' "$companies" | head -c 300)"
COMPANY_ID="$(jget "$companies" "next((c['id'] for c in (d if isinstance(d, list) else (d or {}).get('companies', d if isinstance(d, list) else [])) if isinstance(c, dict) and c.get('name') == '${COMPANY_NAME}'), '')")"
if [ -n "$COMPANY_ID" ]; then
  pass "company '${COMPANY_NAME}' exists (${COMPANY_ID})"
else
  created="$(api POST /api/companies "{\"name\":\"${COMPANY_NAME}\",\"description\":\"Canary company for the e2e agent-loop smoke\"}")"
  COMPANY_ID="$(jget "$created" "(d or {}).get('id') or ((d or {}).get('company') or {}).get('id')")"
  [ -n "$COMPANY_ID" ] || die "create company failed (${API_CODE}): $(printf '%s' "$created" | head -c 300)"
  pass "company created (${COMPANY_ID})"
fi

# ── 4. Find-or-create canary agent (hermes_local on the stub model tier) ────
agents="$(api GET "/api/companies/${COMPANY_ID}/agents")"
[ "$API_CODE" = "200" ] || die "list agents failed (${API_CODE}): $(printf '%s' "$agents" | head -c 300)"
AGENT_ID="$(jget "$agents" "next((a['id'] for a in (d if isinstance(d, list) else (d or {}).get('agents', [])) if isinstance(a, dict) and a.get('name') == '${AGENT_NAME}'), '')")"
if [ -n "$AGENT_ID" ]; then
  pass "agent '${AGENT_NAME}' exists (${AGENT_ID})"
else
  # provider "auto" is load-bearing: it stops the adapter inferring
  # --provider anthropic from the claude-* model name, so the container's
  # Hermes config.yaml (provider custom → router, api_mode
  # anthropic_messages) controls routing.
  agent_payload="$(python3 -c '
import json, sys
print(json.dumps({
    "name": sys.argv[1],
    "role": "general",
    "title": "Canary roundtrip agent",
    "adapterType": "hermes_local",
    "adapterConfig": {
        "model": sys.argv[2],
        "provider": "auto",
        "timeoutSec": 240,
        "persistSession": False,
        "quiet": True,
        "maxTurnsPerRun": 6,
        "enabledToolsets": ["terminal"],
    },
    "budgetMonthlyCents": 100,
}))' "$AGENT_NAME" "$CANARY_MODEL")"
  created="$(api POST "/api/companies/${COMPANY_ID}/agents" "$agent_payload")"
  AGENT_ID="$(jget "$created" "(d or {}).get('id') or ((d or {}).get('agent') or {}).get('id')")"
  [ -n "$AGENT_ID" ] || die "create agent failed (${API_CODE}): $(printf '%s' "$created" | head -c 300)"
  pass "agent created (${AGENT_ID})"
fi

# ── 5. File the canary issue in backlog (no wake yet), arm stub, release ────
ts="$(date -u +%Y%m%dT%H%M%SZ)"
issue_payload="$(python3 -c '
import json, sys
print(json.dumps({
    "title": f"[{sys.argv[1]}] agent-loop roundtrip {sys.argv[2]}",
    "description": (
        f"{sys.argv[1]}: This is the e2e agent-loop canary "
        "(scripts/smoke-canary.sh). Post the disposition comment on this "
        "issue and mark it done. A scripted stub model backend drives the "
        "agent; the loop itself is real."
    ),
    "status": "backlog",
    "priority": "high",
    "assigneeAgentId": sys.argv[3],
}))' "$MARKER" "$ts" "$AGENT_ID")"
created="$(api POST "/api/companies/${COMPANY_ID}/issues" "$issue_payload")"
ISSUE_ID="$(jget "$created" "(d or {}).get('id') or ((d or {}).get('issue') or {}).get('id')")"
ISSUE_IDENT="$(jget "$created" "(d or {}).get('identifier') or ((d or {}).get('issue') or {}).get('identifier')")"
[ -n "$ISSUE_ID" ] || die "create issue failed (${API_CODE}): $(printf '%s' "$created" | head -c 300)"
[ -n "$ISSUE_IDENT" ] || ISSUE_IDENT="$ISSUE_ID"
pass "canary issue filed (${ISSUE_IDENT}, backlog)"

arm="$(curl -sS -X POST "${STUB}/canary-config" -H "Content-Type: application/json" \
  -d "{\"issueIdentifier\":\"${ISSUE_IDENT}\",\"apiBase\":\"${PAPERCLIP_INTERNAL_API_URL}\"}" 2>/dev/null || echo '')"
printf '%s' "$arm" | grep -q '"armed"' || die "failed to arm canary stub: ${arm:-<no response>}"
pass "stub armed for ${ISSUE_IDENT}"

release="$(api PATCH "/api/issues/${ISSUE_IDENT}" '{"status":"todo"}')"
[ "$API_CODE" = "200" ] || die "release to todo failed (${API_CODE}): $(printf '%s' "$release" | head -c 300)"
pass "issue released to todo — assignment wake queued"

# ── 6. Poll until terminal status or deadline ────────────────────────────────
deadline=$(( $(date +%s) + CANARY_TIMEOUT_SECONDS ))
STATUS=""
last_logged=""
while :; do
  issue="$(api GET "/api/issues/${ISSUE_IDENT}")"
  STATUS="$(jget "$issue" "(d or {}).get('status') or ((d or {}).get('issue') or {}).get('status')")"
  if [ "$STATUS" != "$last_logged" ] && [ -n "$STATUS" ]; then
    echo "    status: ${STATUS} ($(( deadline - $(date +%s) ))s left)"
    last_logged="$STATUS"
  fi
  case "$STATUS" in
    done|cancelled) break ;;
  esac
  if [ "$(date +%s)" -ge "$deadline" ]; then
    STUB_STATE="$(curl -s "${STUB}/canary-state" 2>/dev/null || echo '{}')"
    die "TIMEOUT after ${CANARY_TIMEOUT_SECONDS}s — agent never completed the canary issue (last status: ${STATUS:-unknown}). The wake→adapter→router→model→disposition path is broken."
  fi
  sleep "$CANARY_POLL_SECONDS"
done

STUB_STATE="$(curl -s "${STUB}/canary-state" 2>/dev/null || echo '{}')"

# ── 7. Assertions ────────────────────────────────────────────────────────────
if [ "$STATUS" = "done" ]; then
  pass "issue reached status=done"
else
  die "issue terminal but NOT done (status=${STATUS}) — the agent gave up or the issue was cancelled"
fi

comments="$(api GET "/api/issues/${ISSUE_IDENT}/comments")"
[ "$API_CODE" = "200" ] || die "list comments failed (${API_CODE}): $(printf '%s' "$comments" | head -c 300)"
disposition="$(jget "$comments" "next((c.get('body','') for c in (d if isinstance(d, list) else (d or {}).get('comments', [])) if isinstance(c, dict) and '${DISPOSITION_TAG}' in (c.get('body') or '')), '')")"
if [ -n "$disposition" ]; then
  pass "disposition comment present"
  echo "    ${disposition}" | head -c 240; echo
else
  die "issue is done but carries NO disposition comment ('${DISPOSITION_TAG}') — done-without-work"
fi

model_requests="$(jget "$STUB_STATE" "(d or {}).get('model_requests', 0)")"
tool_turns="$(jget "$STUB_STATE" "(d or {}).get('tool_turns', 0)")"
if [ "${model_requests:-0}" -ge 1 ] 2>/dev/null && [ "${tool_turns:-0}" -ge 1 ] 2>/dev/null; then
  pass "model hop traversed router → stub (requests=${model_requests}, tool turns=${tool_turns})"
else
  die "issue closed but the model hop never traversed the router → stub (state: ${STUB_STATE}) — something closed the issue out-of-band"
fi

summary_and_exit
