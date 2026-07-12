#!/bin/sh
# Governed memory helper — wraps the memory-governor API for PaperClip agents
# and the operator.
#
# This is the classed write path (Phase 1A §10.1): instead of an untyped
# honcho write, observations go through the governor's admission pipeline
# (classify -> validate -> dedupe -> three-outcome retention). When
# MEMORY_CLASSES_ENABLED is off the governor answers {"status":"disabled"}
# and callers should fall back to `pc-honcho record` (zero behavior change).
#
# Subcommands:
#   record --content "<text>" [--class <memory_class>] [--scope-kind task
#          --scope-id MRT-123] [--session-id <id>] [--source <source_type>]
#          [--pin-request] [--confidence 0.9]
#     Submit an observation through admission. Prints the admission verdict.
#
#   recall --query "<text>" [--scope-kind task --scope-id MRT-123]
#          [--session-id <id>]
#     Ask the retrieval planner for the governed memory package.
#
#   session-note --session-id <id> --content "<text>"
#     Plane D ephemeral write (session_memory, 24h TTL, dies with session).
#
#   list [--class X] [--state Y] [--pin-candidates] | show <doc_id> | audit
#   pin <doc_id> | demote <doc_id> [--to <class>] | confirm <doc_id>
#   dispute <doc_id> | supersede <doc_id> | rm <doc_id>     (operator actions)
#
# Required env vars (set on ca-orchestrator by Terraform):
#   GOVERNOR_BASE_URL — e.g. http://ca-memory-governor-dev
#   GOVERNOR_API_KEY  — shared key (KV: memory-governor-api-key)
#   GOVERNOR_WORKSPACE — workspace name (defaults to HONCHO_APP_ID or hermes-dev)
#   PAPERCLIP_AGENT_SLUG — used as observer/created_by identity
#   HONCHO_USER_PEER_ID — canonical user peer `record` writes facts about
#     (defaults to "user"; see docs/design/memory-system.md §18)

set -e

BASE="${GOVERNOR_BASE_URL:-http://ca-memory-governor-dev}"
KEY="${GOVERNOR_API_KEY:-}"
WORKSPACE="${GOVERNOR_WORKSPACE:-${HONCHO_APP_ID:-hermes-dev}}"
AGENT="${PAPERCLIP_AGENT_SLUG:-${GOVERNOR_AGENT_SLUG:-operator}}"
# A5: canonical user peer. `record` sends `observed` EXPLICITLY instead of
# leaning on the governor's server-side default — an omitted field is how
# writes fragment across peers when any component's default drifts. The same
# deploy-time input (HONCHO_USER_PEER_ID) feeds compose, Terraform, and the
# governor, so writer and reader always name the same peer.
OBSERVED="${HONCHO_USER_PEER_ID:-user}"

jpost() {
  curl -sS -X POST "$BASE$1" \
    -H "Content-Type: application/json" \
    -H "X-Governor-Key: $KEY" \
    -d "$2"
  echo
}

jget() {
  curl -sS "$BASE$1" -H "X-Governor-Key: $KEY"
  echo
}

# Build a JSON string value safely (escape backslash, quote, newline)
jstr() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | awk '{printf "%s\\n", $0}' | sed -e '$ s/\\n$//'
}

usage() {
  sed -n '5,30p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

[ "$#" -eq 0 ] && usage

CMD="$1"
shift

case "$CMD" in
  record)
    CONTENT=""; CLASS=""; SCOPE_KIND=""; SCOPE_ID=""; SESSION=""; SOURCE=""
    PIN="false"; CONF=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --content)     CONTENT="$2"; shift 2 ;;
        --class)       CLASS="$2"; shift 2 ;;
        --scope-kind)  SCOPE_KIND="$2"; shift 2 ;;
        --scope-id)    SCOPE_ID="$2"; shift 2 ;;
        --session-id)  SESSION="$2"; shift 2 ;;
        --source)      SOURCE="$2"; shift 2 ;;
        --confidence)  CONF="$2"; shift 2 ;;
        --pin-request) PIN="true"; shift ;;
        *) usage ;;
      esac
    done
    [ -z "$CONTENT" ] && { echo "ERROR: --content required" >&2; exit 2; }
    BODY="{\"content\":\"$(jstr "$CONTENT")\",\"workspace_name\":\"$WORKSPACE\",\"observer\":\"$AGENT\",\"observed\":\"$(jstr "$OBSERVED")\",\"created_by_peer\":\"$AGENT\",\"pin_request\":$PIN"
    [ -n "$CLASS" ]      && BODY="$BODY,\"memory_class\":\"$CLASS\""
    [ -n "$SCOPE_KIND" ] && BODY="$BODY,\"scope_kind\":\"$SCOPE_KIND\""
    [ -n "$SCOPE_ID" ]   && BODY="$BODY,\"scope_id\":\"$(jstr "$SCOPE_ID")\""
    [ -n "$SESSION" ]    && BODY="$BODY,\"session_id\":\"$(jstr "$SESSION")\""
    [ -n "$SOURCE" ]     && BODY="$BODY,\"source_type\":\"$SOURCE\""
    [ -n "$CONF" ]       && BODY="$BODY,\"confidence_score\":$CONF"
    BODY="$BODY}"
    jpost "/admit" "$BODY"
    ;;

  recall)
    QUERY=""; SCOPE_KIND=""; SCOPE_ID=""; SESSION=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --query)      QUERY="$2"; shift 2 ;;
        --scope-kind) SCOPE_KIND="$2"; shift 2 ;;
        --scope-id)   SCOPE_ID="$2"; shift 2 ;;
        --session-id) SESSION="$2"; shift 2 ;;
        *) usage ;;
      esac
    done
    [ -z "$QUERY" ] && { echo "ERROR: --query required" >&2; exit 2; }
    BODY="{\"query\":\"$(jstr "$QUERY")\",\"workspace_name\":\"$WORKSPACE\",\"agent_slug\":\"$AGENT\""
    [ -n "$SCOPE_KIND" ] && BODY="$BODY,\"active_scope_kind\":\"$SCOPE_KIND\""
    [ -n "$SCOPE_ID" ]   && BODY="$BODY,\"active_scope_id\":\"$(jstr "$SCOPE_ID")\""
    [ -n "$SESSION" ]    && BODY="$BODY,\"session_id\":\"$(jstr "$SESSION")\""
    BODY="$BODY}"
    jpost "/plan-retrieval" "$BODY"
    ;;

  session-note)
    SESSION=""; CONTENT=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --session-id) SESSION="$2"; shift 2 ;;
        --content)    CONTENT="$2"; shift 2 ;;
        *) usage ;;
      esac
    done
    [ -z "$SESSION" ] || [ -z "$CONTENT" ] && { echo "ERROR: --session-id and --content required" >&2; exit 2; }
    jpost "/session-memory" "{\"workspace_name\":\"$WORKSPACE\",\"session_id\":\"$(jstr "$SESSION")\",\"content\":\"$(jstr "$CONTENT")\",\"created_by_peer\":\"$AGENT\"}"
    ;;

  list)
    QS="workspace_name=$WORKSPACE"
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --class)          QS="$QS&memory_class=$2"; shift 2 ;;
        --state)          QS="$QS&verification_state=$2"; shift 2 ;;
        --pin-candidates) QS="$QS&pin_candidates=true"; shift ;;
        *) usage ;;
      esac
    done
    jget "/memory?$QS"
    ;;

  show)   [ -z "${1:-}" ] && usage; jget "/memory/$1" ;;
  audit)  jget "/memory/audit" ;;
  digest) jget "/digest" ;;

  pin|confirm|dispute|supersede|rm)
    DOC="${1:-}"; [ -z "$DOC" ] && usage; shift || true
    NOTE="${2:-}"
    jpost "/memory/$DOC/action" "{\"action\":\"$CMD\",\"actor\":\"$AGENT\",\"note\":\"$(jstr "${NOTE:-}")\"}"
    ;;

  demote)
    DOC="${1:-}"; [ -z "$DOC" ] && usage; shift || true
    TO="durable_fact"
    [ "${1:-}" = "--to" ] && TO="$2"
    jpost "/memory/$DOC/action" "{\"action\":\"demote\",\"actor\":\"$AGENT\",\"demote_to\":\"$TO\"}"
    ;;

  *) usage ;;
esac
