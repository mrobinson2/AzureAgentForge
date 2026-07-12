#!/bin/sh
# Honcho memory helper — wraps the Honcho v1 API for use by PaperClip agents.
#
# Honcho is the persistent memory store for both Telegram-Hermes and Orchestrator.
# The Dialectic API (`peers/{peer_id}/chat`) returns a natural-language answer
# grounded in everything Honcho has stored about a given peer.
#
# Subcommands:
#   list-peers
#     List every peer in the workspace. Use this to discover the peer ID
#     that the Telegram bot writes to.
#
#   ask [--peer <peer_id>] --query "..."
#     Query the dialectic API. Returns Honcho's natural-language answer.
#     --peer defaults to the canonical user peer (HONCHO_USER_PEER_ID).
#
# Required env vars (already set on ca-orchestrator):
#   HONCHO_BASE_URL  — e.g. https://ca-memory.internal.<env>.azurecontainerapps.io
#   HONCHO_API_KEY   — auth token (defaults to "self-hosted" in dev)
#   HONCHO_APP_ID    — Honcho workspace name (e.g. "hermes-dev")
#   HONCHO_USER_PEER_ID — canonical user peer; the default for --peer when
#     omitted (falls back to "user"; see docs/design/memory-system.md §18)

set -e

WORKSPACE="${HONCHO_APP_ID:-hermes-dev}"
BASE="${HONCHO_BASE_URL:-http://ca-memory}"
KEY="${HONCHO_API_KEY:-self-hosted}"
# A5: canonical user peer. `ask`/`record` default --peer to this SAME
# deploy-time input the rest of the stack resolves (compose env, Terraform,
# the governor's /admit default) so a caller that forgets --peer still reads
# and writes the peer everyone else uses instead of inventing a new one.
DEFAULT_PEER="${HONCHO_USER_PEER_ID:-user}"

usage() {
  cat <<EOF
Usage:
  pc-honcho list-peers
  pc-honcho ask [--peer <peer_id>] --query "<question>"
  pc-honcho record [--peer <peer_id>] --content "<text>" [--session-id <id>]

--peer defaults to the canonical user peer (HONCHO_USER_PEER_ID, fallback "user").
Env: HONCHO_BASE_URL=$BASE  HONCHO_APP_ID=$WORKSPACE  peer default=$DEFAULT_PEER
EOF
  exit 2
}

[ "$#" -eq 0 ] && usage

CMD="$1"
shift

case "$CMD" in
  list-peers)
    # Honcho's list endpoint is POST /peers/list (with optional filter body) — not GET /peers.
    curl -sS -X POST "$BASE/v3/workspaces/$WORKSPACE/peers/list" \
      -H "Authorization: Bearer $KEY" \
      -H "Content-Type: application/json" \
      -d '{}'
    echo
    ;;

  list-workspaces)
    # Diagnostic: enumerate every workspace in this Honcho instance. Useful when
    # peers are unexpectedly missing — the Telegram bot may be writing to a
    # different workspace than the one HONCHO_APP_ID points at.
    curl -sS -X POST "$BASE/v3/workspaces/list" \
      -H "Authorization: Bearer $KEY" \
      -H "Content-Type: application/json" \
      -d '{}'
    echo
    ;;

  ask)
    PEER="$DEFAULT_PEER"
    QUERY=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --peer)  PEER="$2";  shift 2 ;;
        --query) QUERY="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
      esac
    done
    [ -z "$PEER" ]  && { echo "--peer is required (or set HONCHO_USER_PEER_ID)" >&2; exit 2; }
    [ -z "$QUERY" ] && { echo "--query is required" >&2; exit 2; }

    BODY=$(python3 -c "import json,sys; print(json.dumps({'query': sys.argv[1], 'reasoning_level': 'low'}))" "$QUERY")
    curl -sS -X POST "$BASE/v3/workspaces/$WORKSPACE/peers/$PEER/chat" \
      -H "Authorization: Bearer $KEY" \
      -H "Content-Type: application/json" \
      -d "$BODY"
    echo
    ;;

  record)
    # Post a message into a Honcho session, attributed to a peer. This is how
    # Orchestrator contributes new content (issue bodies, etc.) back to the user's
    # representation — without it, Orchestrator is read-only against upstream-built memory.
    PEER="$DEFAULT_PEER"
    CONTENT=""
    SESSION_ID="${HONCHO_DEFAULT_SESSION_ID:-orchestrator-paperclip}"
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --peer)       PEER="$2";       shift 2 ;;
        --content)    CONTENT="$2";    shift 2 ;;
        --session-id) SESSION_ID="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
      esac
    done
    [ -z "$PEER" ]    && { echo "--peer is required (or set HONCHO_USER_PEER_ID)" >&2; exit 2; }
    [ -z "$CONTENT" ] && { echo "--content is required" >&2; exit 2; }

    # Idempotently ensure the session exists with the peer attached.
    # 409 / "already exists" is fine — just keep going.
    SESSION_BODY=$(python3 -c "import json,sys; print(json.dumps({'id': sys.argv[1], 'peers': {sys.argv[2]: {}}}))" "$SESSION_ID" "$PEER")
    curl -sS -X POST "$BASE/v3/workspaces/$WORKSPACE/sessions" \
      -H "Authorization: Bearer $KEY" \
      -H "Content-Type: application/json" \
      -d "$SESSION_BODY" >/dev/null 2>&1 || true

    # Post the message attributed to the peer.
    MSG_BODY=$(python3 -c "import json,sys; print(json.dumps({'messages': [{'peer_id': sys.argv[1], 'content': sys.argv[2]}]}))" "$PEER" "$CONTENT")
    curl -sS -X POST "$BASE/v3/workspaces/$WORKSPACE/sessions/$SESSION_ID/messages" \
      -H "Authorization: Bearer $KEY" \
      -H "Content-Type: application/json" \
      -d "$MSG_BODY"
    echo
    ;;

  *)
    echo "Unknown subcommand: $CMD" >&2
    usage
    ;;
esac
