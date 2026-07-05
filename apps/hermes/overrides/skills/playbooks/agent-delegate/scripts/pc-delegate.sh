#!/usr/bin/env bash
# pc-delegate.sh — PaperClip delegation helper.
#
# Subcommands:
#   create-child   --parent <identifier> --agent <name> --title <t> --description <d> [--budget <usd>] [--quiet]
#   comment        --issue <identifier> --body <b> [--evidence <text|file>]
#   set-status     --issue <identifier> --status <s>
#   close-parent   --issue <identifier> [--require-children]
#   list-children  --parent <identifier>
#   find-agent     <name>
#   verify         --issue <identifier>
#
# Dream-backlog Tier-0 features (both gated OFF by env vars; no-op when unset):
#   §0.5 verification lane — `comment --evidence` + VERIFICATION_LANE=1 runs a
#        gpt4o-mini skeptic; UNSUPPORTED → comment does NOT post (exit 7).
#   §0.7 cost envelope — `create-child --budget <usd>` writes a "## Budget
#        envelope" block into the description; the router enforces per-run.
#
# Environment:
#   PAPERCLIP_API_KEY      required (Bearer token)
#   PAPERCLIP_COMPANY_ID   optional, defaults to 00000000-0000-0000-0000-000000000000
#   PAPERCLIP_BASE_URL     optional, defaults to http://localhost:3099
#   PAPERCLIP_ORIGIN       optional, defaults to http://localhost:3100
#
# Exit codes:
#   0   success
#   1   usage / argument error
#   2   missing dependency or credential
#   3   API non-2xx
#   4   agent name not found
#   5   parent identifier could not be resolved
#   6   close-parent --require-children: parent has no children
#   7   comment blocked by the §0.5 verification lane (evidence does not support claim)

set -u
set -o pipefail

API_BASE="${PAPERCLIP_BASE_URL:-http://localhost:3099}"
ORIGIN="${PAPERCLIP_ORIGIN:-http://localhost:3100}"
COMPANY_ID="${PAPERCLIP_COMPANY_ID:-00000000-0000-0000-0000-000000000000}"

# Pick a Python 3 interpreter. python3 is guaranteed in the Hermes container
# (python:3.12-slim base); on the Windows dev box we fall back to `py -3`.
PY=""
for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if [ "$candidate" = "py" ]; then
            PY="py -3"
        else
            PY="$candidate"
        fi
        break
    fi
done

# ---------- agent name -> UUID -------------------------------------------------
# Accepts either a short name (coder, infrastructure, ...) OR a full UUID. UUIDs are
# echoed back as-is so callers that already have one can pass it through
# without the helper rejecting it.
agent_uuid() {
    local input="$1"
    local lower
    lower=$(printf '%s' "$input" | tr '[:upper:]' '[:lower:]')
    case "$lower" in
        [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f])
                    echo "$lower" ;;
        strategy)      echo 00000000-0000-0000-0000-000000000000 ;;
        planner)       echo 00000000-0000-0000-0000-000000000000 ;;
        coder)      echo 00000000-0000-0000-0000-000000000000 ;;
        infrastructure)      echo 00000000-0000-0000-0000-000000000000 ;;
        curator)  echo 00000000-0000-0000-0000-000000000000 ;;
        researcher)    echo 00000000-0000-0000-0000-000000000000 ;;
        qa)     echo 00000000-0000-0000-0000-000000000000 ;;
        security)     echo 00000000-0000-0000-0000-000000000000 ;;
        cost-guardian)      echo 00000000-0000-0000-0000-000000000000 ;;
        coach)     echo 00000000-0000-0000-0000-000000000000 ;;
        business)     echo 00000000-0000-0000-0000-000000000000 ;;
        psychology)  echo 00000000-0000-0000-0000-000000000000 ;;
        *) return 1 ;;
    esac
}

# ---------- preflight ----------------------------------------------------------
require_cmd() {
    command -v "$1" >/dev/null 2>&1 || { echo "ERROR: required command not found: $1" >&2; exit 2; }
}

require_python() {
    if [ -z "$PY" ]; then
        echo "ERROR: no Python 3 interpreter found (tried python3, python, py). Install python3 in the runtime container." >&2
        exit 2
    fi
}

require_creds() {
    if [ -z "${PAPERCLIP_API_KEY:-}" ]; then
        echo "ERROR: PAPERCLIP_API_KEY is not set in the environment." >&2
        exit 2
    fi
}

require_cmd curl
require_python

# ---------- JSON helpers (python-backed) --------------------------------------
# We pass the helper script via `-c` instead of stdin, so the JSON input can
# flow through the pipe to sys.stdin without being clobbered by the script.

_PY_BUILD='import json,sys
args=sys.argv[1:]
if len(args)%2:sys.stderr.write("json_build: odd args\n");sys.exit(2)
sys.stdout.write(json.dumps({args[i]:args[i+1] for i in range(0,len(args),2)}))'

_PY_EXTRACT='import json,sys
path=sys.argv[1].split(".")
try:
    data=json.load(sys.stdin)
except Exception:
    sys.exit(0)
cur=data
for k in path:
    if isinstance(cur,dict) and k in cur:
        cur=cur[k]
    else:
        sys.exit(0)
if cur is None:sys.exit(0)
sys.stdout.write(str(cur))'

_PY_FILTER_CHILDREN='import json,sys
pid=sys.argv[1]
try:
    data=json.load(sys.stdin)
except Exception:
    sys.stdout.write("[]");sys.exit(0)
if not isinstance(data,list):
    sys.stdout.write("[]");sys.exit(0)
out=[{"id":x.get("id"),"identifier":x.get("identifier"),"title":x.get("title"),"status":x.get("status"),"assigneeAgentId":x.get("assigneeAgentId") or x.get("assignee_agent_id")} for x in data if (x.get("parent_id") or x.get("parentId"))==pid]
sys.stdout.write(json.dumps(out,indent=2))'

_PY_FIND_DUP='import json,sys
pid,aid,title=sys.argv[1],sys.argv[2],sys.argv[3]
try:
    data=json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(data,list):sys.exit(0)
for x in data:
    parent=x.get("parent_id") or x.get("parentId")
    assignee=x.get("assigneeAgentId") or x.get("assignee_agent_id")
    if parent==pid and assignee==aid and x.get("title")==title:
        sys.stdout.write(x.get("identifier") or "")
        sys.exit(0)'

_PY_COUNT_CHILDREN='import json,sys
pid=sys.argv[1]
try:
    data=json.load(sys.stdin)
except Exception:
    sys.stdout.write("0");sys.exit(0)
if not isinstance(data,list):
    sys.stdout.write("0");sys.exit(0)
n=sum(1 for x in data if (x.get("parent_id") or x.get("parentId"))==pid)
sys.stdout.write(str(n))'

# json_build  KEY VAL [KEY VAL ...]   -> stdout: JSON object string
json_build() {
    $PY -c "$_PY_BUILD" "$@"
}

# json_extract  PATH               -> stdin: JSON; stdout: value at dotted path
json_extract() {
    $PY -c "$_PY_EXTRACT" "$1"
}

# json_filter_children  PARENT_UUID -> stdin: JSON list; stdout: filtered list
json_filter_children() {
    $PY -c "$_PY_FILTER_CHILDREN" "$1"
}

# json_find_duplicate  PARENT_UUID AGENT_UUID TITLE
#   -> stdin: JSON list; stdout: identifier of duplicate (or empty)
json_find_duplicate() {
    $PY -c "$_PY_FIND_DUP" "$1" "$2" "$3"
}

# json_count_children  PARENT_UUID -> stdin: JSON list; stdout: integer count
json_count_children() {
    $PY -c "$_PY_COUNT_CHILDREN" "$1"
}

# ---------- low-level HTTP -----------------------------------------------------
# Captures status code and body separately so the caller can branch on status.
# Usage: api_request METHOD PATH [JSON_BODY]
# Sets: API_STATUS, API_BODY
api_request() {
    local method="$1" path="$2" body="${3:-}"
    local tmp; tmp=$(mktemp)
    local status

    if [ -n "$body" ]; then
        status=$(curl -sS -o "$tmp" -w '%{http_code}' \
            -X "$method" "${API_BASE}${path}" \
            -H "Authorization: Bearer ${PAPERCLIP_API_KEY}" \
            -H "Origin: ${ORIGIN}" \
            -H "X-Automation-Sub: paperclip-agent" \
            -H "Content-Type: application/json" \
            --data-raw "$body" 2>&1) || true
    else
        status=$(curl -sS -o "$tmp" -w '%{http_code}' \
            -X "$method" "${API_BASE}${path}" \
            -H "Authorization: Bearer ${PAPERCLIP_API_KEY}" \
            -H "Origin: ${ORIGIN}" \
            -H "X-Automation-Sub: paperclip-agent" 2>&1) || true
    fi

    API_STATUS="$status"
    API_BODY=$(cat "$tmp")
    rm -f "$tmp"
}

is_2xx() {
    case "$1" in
        2??) return 0 ;;
        *)   return 1 ;;
    esac
}

# ---------- subcommand: find-agent --------------------------------------------
cmd_find_agent() {
    local name="${1:-}"
    if [ -z "$name" ]; then
        echo "Usage: pc-delegate find-agent <name>" >&2
        exit 1
    fi
    local uuid
    if uuid=$(agent_uuid "$name"); then
        echo "$uuid"
        exit 0
    fi
    echo "ERROR: '${name}' is neither a known agent name nor a UUID." >&2
    echo "Known names: strategy, planner, coder, infrastructure, curator, researcher, qa, security, cost-guardian, coach, business, psychology" >&2
    exit 4
}

# ---------- subcommand: verify -------------------------------------------------
# Resolves an issue identifier (MRT-46) to its UUID.
resolve_issue_uuid() {
    local identifier="$1"
    api_request GET "/api/issues/${identifier}"
    if ! is_2xx "$API_STATUS"; then
        echo "ERROR: GET /api/issues/${identifier} returned ${API_STATUS}" >&2
        echo "Body: ${API_BODY}" >&2
        return 5
    fi
    local uuid
    uuid=$(printf '%s' "$API_BODY" | json_extract id)
    if [ -z "$uuid" ]; then
        echo "ERROR: response for ${identifier} did not contain .id" >&2
        echo "Body: ${API_BODY}" >&2
        return 5
    fi
    printf '%s' "$uuid"
}

cmd_verify() {
    local identifier=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --issue) identifier="$2"; shift 2 ;;
            *) echo "Usage: pc-delegate verify --issue <identifier>" >&2; exit 1 ;;
        esac
    done
    [ -z "$identifier" ] && { echo "Usage: pc-delegate verify --issue <identifier>" >&2; exit 1; }
    require_creds
    local uuid
    uuid=$(resolve_issue_uuid "$identifier") || exit 5
    echo "{\"identifier\":\"${identifier}\",\"id\":\"${uuid}\"}"
}

# ---------- subcommand: list-children -----------------------------------------
cmd_list_children() {
    local parent=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --parent) parent="$2"; shift 2 ;;
            *) echo "Usage: pc-delegate list-children --parent <identifier>" >&2; exit 1 ;;
        esac
    done
    [ -z "$parent" ] && { echo "Usage: pc-delegate list-children --parent <identifier>" >&2; exit 1; }
    require_creds

    local parent_uuid
    parent_uuid=$(resolve_issue_uuid "$parent") || exit 5

    api_request GET "/api/companies/${COMPANY_ID}/issues"
    if ! is_2xx "$API_STATUS"; then
        echo "ERROR: GET /api/companies/${COMPANY_ID}/issues returned ${API_STATUS}" >&2
        echo "Body: ${API_BODY}" >&2
        exit 3
    fi
    printf '%s' "$API_BODY" | json_filter_children "$parent_uuid"
}

# ---------- subcommand: create-child ------------------------------------------
cmd_create_child() {
    local parent="" agent_name="" title="" description="" quiet=0 budget=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --parent)      parent="$2"; shift 2 ;;
            --agent)       agent_name="$2"; shift 2 ;;
            --title)       title="$2"; shift 2 ;;
            --description) description="$2"; shift 2 ;;
            --budget)      budget="$2"; shift 2 ;;
            --quiet)       quiet=1; shift ;;
            *) echo "Unknown flag: $1" >&2; exit 1 ;;
        esac
    done

    if [ -z "$parent" ] || [ -z "$agent_name" ] || [ -z "$title" ] || [ -z "$description" ]; then
        echo "Usage: pc-delegate create-child --parent <id> --agent <name> --title <t> --description <d> [--budget <usd>] [--quiet]" >&2
        exit 1
    fi

    require_creds

    # Cost-envelope contracts (§0.7): attach a per-run budget at delegation by
    # appending a "## Budget envelope" block to the description. Lowest-friction
    # path — no PaperClip schema change, survives the camelCase-only API. The
    # router reads metadata.budget_envelope_usd at inference; the orchestrator
    # is responsible for surfacing the envelope into that metadata (e.g. via the
    # adapter). When --budget is omitted this is a pure no-op.
    if [ -n "$budget" ]; then
        case "$budget" in
            ''|*[!0-9.]*) echo "ERROR: --budget must be a USD amount (e.g. 0.10), got '${budget}'" >&2; exit 1 ;;
        esac
        description="${description}

## Budget envelope
${budget} USD max"
    fi

    local agent_id
    if ! agent_id=$(agent_uuid "$agent_name"); then
        echo "ERROR: --agent value '${agent_name}' is neither a known agent name nor a UUID." >&2
        echo "Pass a short name (e.g. infrastructure, coder, curator) OR a 36-character UUID. The helper resolves names to UUIDs internally — do not pre-resolve." >&2
        echo "Known names: strategy, planner, coder, infrastructure, curator, researcher, qa, security, cost-guardian, coach, business, psychology" >&2
        exit 4
    fi

    local parent_uuid
    parent_uuid=$(resolve_issue_uuid "$parent") || exit 5

    # Idempotency: skip if a child with same parent + agent + title already exists.
    api_request GET "/api/companies/${COMPANY_ID}/issues"
    if is_2xx "$API_STATUS"; then
        local existing
        existing=$(printf '%s' "$API_BODY" | json_find_duplicate "$parent_uuid" "$agent_id" "$title")
        if [ -n "$existing" ]; then
            if [ "$quiet" -eq 1 ]; then
                printf '%s\n' "$existing"
            else
                echo "{\"status\":\"skipped\",\"reason\":\"duplicate\",\"identifier\":\"${existing}\"}"
            fi
            exit 0
        fi
    fi

    local payload
    payload=$(json_build \
        title "$title" \
        description "$description" \
        assigneeAgentId "$agent_id" \
        parentId "$parent_uuid" \
        status "todo")

    api_request POST "/api/companies/${COMPANY_ID}/issues" "$payload"
    if ! is_2xx "$API_STATUS"; then
        echo "ERROR: create-child failed. HTTP ${API_STATUS}" >&2
        echo "Request payload: ${payload}" >&2
        echo "Response body: ${API_BODY}" >&2
        exit 3
    fi

    local new_id new_identifier returned_parent returned_status
    new_id=$(printf '%s' "$API_BODY" | json_extract id)
    new_identifier=$(printf '%s' "$API_BODY" | json_extract identifier)
    returned_parent=$(printf '%s' "$API_BODY" | json_extract parent_id)
    if [ -z "$returned_parent" ]; then
        returned_parent=$(printf '%s' "$API_BODY" | json_extract parentId)
    fi
    returned_status=$(printf '%s' "$API_BODY" | json_extract status)

    if [ -z "$new_identifier" ]; then
        echo "ERROR: create succeeded (HTTP ${API_STATUS}) but response missing .identifier" >&2
        echo "Body: ${API_BODY}" >&2
        exit 3
    fi

    if [ -z "$returned_parent" ] || [ "$returned_parent" != "$parent_uuid" ]; then
        echo "WARNING: child ${new_identifier} created but parent_id was not retained on the server (got: '${returned_parent}', expected: '${parent_uuid}'). The child exists but is not linked to ${parent}." >&2
    fi

    # Belt-and-suspenders: PaperClip's create endpoint has been observed to
    # default new issues to 'cancelled' when the create payload's status is
    # ignored. PATCH to 'todo' if the create response came back as anything
    # other than 'todo' so the assignee actually picks the task up.
    if [ "$returned_status" != "todo" ]; then
        local patch_payload
        patch_payload=$(json_build status "todo")
        api_request PATCH "/api/issues/${new_identifier}" "$patch_payload"
        if ! is_2xx "$API_STATUS"; then
            echo "WARNING: child ${new_identifier} created but PATCH status=todo failed (HTTP ${API_STATUS}, returned_status='${returned_status}'). Body: ${API_BODY}" >&2
        fi
    fi

    if [ "$quiet" -eq 1 ]; then
        printf '%s\n' "$new_identifier"
    else
        echo "{\"status\":\"created\",\"identifier\":\"${new_identifier}\",\"id\":\"${new_id}\",\"parent\":\"${parent}\",\"agent\":\"${agent_name}\"}"
    fi
}

# ---------- Adversarial verification lane (dream-backlog §0.5) -----------------
# When VERIFICATION_LANE=1 AND `comment --evidence <text|file>` is supplied, a
# cheap gpt4o-mini skeptic checks the --body claim against the cited evidence
# before the comment posts. UNSUPPORTED → the comment does NOT post (exit 7).
# This is the ONE universal chokepoint every agent comment passes through; the
# auth-proxy is bypassed (agents POST direct to :3099) so the gate must live
# here in the shell helper.
#
# Gated on the VERIFICATION_LANE env var (matches §0.2's TRACK_RECORD_ROUTING
# style). When unset/!=1, this function is a no-op and comment behaves exactly
# as before. The DB flag VERIFICATION_LANE_ENABLED (migration 0009) is the
# declarative registry / kill switch.
#
# FAIL-OPEN: verify_claim.py exits 0 (allow) on skeptic error/timeout so a
# router outage can't block every agent comment platform-wide. Exit 1 from the
# script means a clean UNSUPPORTED verdict — only then do we block.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
VERIFY_CLAIM_PY="${SCRIPT_DIR}/verify_claim.py"

# Echo a router base URL with any trailing /v1 stripped (verify_claim.py appends
# /v1/chat/completions itself), mirroring docker-entrypoint.sh's sed. Falls back
# to the in-pod sidecar default.
_router_base_url() {
    local raw="${ROUTER_BASE_URL:-${OPENAI_BASE_URL:-http://ca-agent-runtime}}"
    printf '%s' "$raw" | sed 's|/v1/*$||'
}

# verify_comment_claim CLAIM EVIDENCE
#   exit 0 → allow post (SUPPORTED, fail-open, or lane disabled)
#   exit 7 → BLOCK (clean UNSUPPORTED)
verify_comment_claim() {
    local claim="$1" evidence="$2"
    # Lane off, or no evidence to check against → allow (no gate).
    if [ "${VERIFICATION_LANE:-}" != "1" ] || [ -z "$evidence" ]; then
        return 0
    fi
    if [ ! -f "$VERIFY_CLAIM_PY" ]; then
        echo "WARNING: VERIFICATION_LANE=1 but ${VERIFY_CLAIM_PY} not found — failing open (allowing post)." >&2
        return 0
    fi
    # --evidence may be a path to a file; if so, read its contents.
    if [ -f "$evidence" ]; then
        evidence="$($PY -c 'import sys;sys.stdout.write(open(sys.argv[1],encoding="utf-8",errors="replace").read())' "$evidence" 2>/dev/null)"
    fi
    local base_url; base_url="$(_router_base_url)"
    if $PY "$VERIFY_CLAIM_PY" \
        --claim "$claim" \
        --evidence "$evidence" \
        --base-url "$base_url" \
        --key "${ROUTER_API_KEY:-}"; then
        return 0
    else
        # Exit code 1 = clean UNSUPPORTED (verify_claim.py already printed the
        # reason to stderr). Anything else (2 = arg error) we also surface but
        # still block, since a misconfigured gate shouldn't silently pass an
        # unverified claim — except the script itself fails OPEN on network
        # errors, so reaching here means the skeptic actively judged UNSUPPORTED.
        return 7
    fi
}

# ---------- subcommand: comment ------------------------------------------------
cmd_comment() {
    local issue="" body="" evidence=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --issue)    issue="$2"; shift 2 ;;
            --body)     body="$2"; shift 2 ;;
            --evidence) evidence="$2"; shift 2 ;;
            *) echo "Unknown flag: $1" >&2; exit 1 ;;
        esac
    done
    if [ -z "$issue" ] || [ -z "$body" ]; then
        echo "Usage: pc-delegate comment --issue <id> --body <text> [--evidence <text|file>]" >&2; exit 1
    fi
    require_creds

    # §0.5 adversarial verification gate (no-op unless VERIFICATION_LANE=1 and
    # --evidence is supplied). On a clean UNSUPPORTED verdict, do NOT post.
    if ! verify_comment_claim "$body" "$evidence"; then
        echo "ERROR: comment BLOCKED by verification lane — the cited evidence does not support the claim." >&2
        echo "The comment was NOT posted to ${issue}. Re-check your claim against your evidence, or post a corrected claim." >&2
        exit 7
    fi

    local payload; payload=$(json_build body "$body")
    api_request POST "/api/issues/${issue}/comments" "$payload"
    if ! is_2xx "$API_STATUS"; then
        echo "ERROR: comment failed. HTTP ${API_STATUS}" >&2
        echo "Body: ${API_BODY}" >&2
        exit 3
    fi
    echo "{\"status\":\"commented\",\"issue\":\"${issue}\"}"
}

# ---------- subcommand: set-status ---------------------------------------------
cmd_set_status() {
    local issue="" status=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --issue)  issue="$2"; shift 2 ;;
            --status) status="$2"; shift 2 ;;
            *) echo "Unknown flag: $1" >&2; exit 1 ;;
        esac
    done
    if [ -z "$issue" ] || [ -z "$status" ]; then
        echo "Usage: pc-delegate set-status --issue <id> --status <s>" >&2; exit 1
    fi
    require_creds

    case "$status" in
        backlog|todo|in_progress|done|cancelled) ;;
        *) echo "ERROR: invalid status '${status}'. Allowed: backlog|todo|in_progress|done|cancelled" >&2; exit 1 ;;
    esac

    local payload; payload=$(json_build status "$status")
    api_request PATCH "/api/issues/${issue}" "$payload"
    if ! is_2xx "$API_STATUS"; then
        echo "ERROR: set-status failed. HTTP ${API_STATUS}" >&2
        echo "Body: ${API_BODY}" >&2
        exit 3
    fi
    echo "{\"status\":\"updated\",\"issue\":\"${issue}\",\"new_status\":\"${status}\"}"
}

# ---------- subcommand: close-parent ------------------------------------------
# PATCH an issue to status=done with an optional safety guard that refuses
# unless the parent has at least one child issue. The guard protects against
# the canonical phantom-delegation failure mode where Orchestrator (or any
# orchestrator) posts "Delegated to X" without actually calling
# create-child, then closes the parent as done.
#
# Usage:
#   close-parent --issue <id>                    # unconditional close
#   close-parent --issue <id> --require-children # refuses if zero children (exit 6)
cmd_close_parent() {
    local issue="" require_children=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --issue)            issue="$2"; shift 2 ;;
            --require-children) require_children=1; shift ;;
            *) echo "Unknown flag: $1" >&2; exit 1 ;;
        esac
    done
    if [ -z "$issue" ]; then
        echo "Usage: pc-delegate close-parent --issue <id> [--require-children]" >&2
        exit 1
    fi
    require_creds

    if [ "$require_children" -eq 1 ]; then
        local parent_uuid count
        parent_uuid=$(resolve_issue_uuid "$issue") || exit 5
        api_request GET "/api/companies/${COMPANY_ID}/issues"
        if ! is_2xx "$API_STATUS"; then
            echo "ERROR: GET /api/companies/${COMPANY_ID}/issues returned ${API_STATUS}" >&2
            echo "Body: ${API_BODY}" >&2
            exit 3
        fi
        count=$(printf '%s' "$API_BODY" | json_count_children "$parent_uuid")
        if [ "$count" = "0" ]; then
            echo "ERROR: close-parent refused — issue ${issue} has 0 child issues but --require-children was set." >&2
            echo "If you claimed delegation in a comment, the claim was false; this is the canonical phantom-delegation guard." >&2
            echo "To fix: (a) run create-child for the real specialist(s) first, then re-run close-parent, OR" >&2
            echo "        (b) if this task was actually ANSWER/HANDLE/RESEARCH (no delegation), use set-status --status done instead." >&2
            exit 6
        fi
    fi

    local payload; payload=$(json_build status "done")
    api_request PATCH "/api/issues/${issue}" "$payload"
    if ! is_2xx "$API_STATUS"; then
        echo "ERROR: close-parent failed. HTTP ${API_STATUS}" >&2
        echo "Body: ${API_BODY}" >&2
        exit 3
    fi
    echo "{\"status\":\"closed\",\"issue\":\"${issue}\"}"
}

# ---------- dispatch -----------------------------------------------------------
usage() {
    cat >&2 <<'USAGE'
pc-delegate.sh — PaperClip delegation helper

Subcommands:
  create-child  --parent <id> --agent <name> --title <t> --description <d> [--budget <usd>] [--quiet]
  comment       --issue <id> --body <text> [--evidence <text|file>]
  set-status    --issue <id> --status <backlog|todo|in_progress|done|cancelled>
  close-parent  --issue <id> [--require-children]
  list-children --parent <id>
  find-agent    <name>
  verify        --issue <id>

Environment:
  PAPERCLIP_API_KEY      required
  PAPERCLIP_COMPANY_ID   optional (default 00000000-0000-0000-0000-000000000000)
  PAPERCLIP_BASE_URL     optional (default http://localhost:3099)
  PAPERCLIP_ORIGIN       optional (default http://localhost:3100)
  VERIFICATION_LANE      optional (=1 enables the §0.5 skeptic gate on `comment --evidence`)
  ROUTER_BASE_URL        optional (router for the §0.5 skeptic; falls back to OPENAI_BASE_URL)
  ROUTER_API_KEY         optional (Bearer for the router, if ROUTER_API_KEY auth is configured)
USAGE
}

if [ $# -lt 1 ]; then usage; exit 1; fi
sub="$1"; shift
case "$sub" in
    create-child)  cmd_create_child "$@" ;;
    comment)       cmd_comment "$@" ;;
    set-status)    cmd_set_status "$@" ;;
    close-parent)  cmd_close_parent "$@" ;;
    list-children) cmd_list_children "$@" ;;
    find-agent)    cmd_find_agent "$@" ;;
    verify)        cmd_verify "$@" ;;
    -h|--help|help) usage; exit 0 ;;
    *) echo "Unknown subcommand: $sub" >&2; usage; exit 1 ;;
esac
