---
name: honcho-memory
description: Query the Honcho persistent memory store for facts about peers (users or agents). Use this for any "what do you know about me / the user" style question instead of fabricating from training data.
version: 1.0.0
metadata:
  hermes:
    tags: [memory, honcho, dialectic, recall]
---

# Honcho Memory

Query the Honcho persistent memory store via its Dialectic API. Honcho holds long-running facts and observations about peers (users and agents) that have been built up across many conversations.

## When to use this skill

- The user asks "what do you know about me?"
- The user asks for personal preferences, habits, history, relationships, or anything specific to them
- You need to recall something the user has previously told an agent
- ANY question where the honest answer requires knowing the user

**Do NOT fabricate personal facts from training data or from words present in your system prompt.** If `pc-honcho` returns an empty or unsure answer, say so plainly.

## Helper path — resolve dynamically once per session

The helper script lives under the Hermes skills directory but the absolute path varies between containers. Resolve it before use:

```bash
HONCHO_HELPER=""
for _p in \
  "/paperclip/.hermes/skills/playbooks/honcho-memory/scripts/pc-honcho.sh" \
  "/app/skills/playbooks/honcho-memory/scripts/pc-honcho.sh" \
  "/opt/data/skills/playbooks/honcho-memory/scripts/pc-honcho.sh"; do
  if [ -x "$_p" ]; then HONCHO_HELPER="$_p"; break; fi
done
[ -z "$HONCHO_HELPER" ] && { echo "honcho-memory helper not found"; exit 2; }
```

## Required environment variables (already set on ca-orchestrator)

| Variable | Purpose |
|---|---|
| `HONCHO_BASE_URL` | Honcho service URL (internal ACA FQDN) |
| `HONCHO_API_KEY`  | Auth token; "self-hosted" in dev |
| `HONCHO_APP_ID`   | Honcho workspace name, e.g. "hermes-dev" |
| `HONCHO_USER_PEER_ID` | The canonical peer ID for the principal user. Discover with `list-peers` if unset. |

## Subcommands

### `list-peers`

List every peer registered in the workspace. Use this to discover the user's peer ID — it will match whatever ID the upstream channel (e.g. the Telegram bot) uses when writing messages to Honcho.

```bash
bash "$HONCHO_HELPER" list-peers
```

Output is JSON. Look for a peer whose name/id refers to the principal user (e.g. their messaging username or numeric user_id).

### `ask --peer <id> --query "..."`

Run a Dialectic query and get a natural-language answer grounded in everything Honcho knows about that peer.

```bash
bash "$HONCHO_HELPER" ask \
  --peer "$HONCHO_USER_PEER_ID" \
  --query "What do you know about the user?"
```

Response shape:
```json
{ "content": "The user lives in <city>, works at <company>, prefers ..." }
```

Parse `.content` and use that as your answer to the user.

### `record --peer <id> --content "..." [--session-id <id>]`

Append a message to a Honcho session, attributed to a peer. Use this to feed the user's content (issue bodies, comments) into Honcho so the deriver can incorporate it into the user's representation. Without `record`, the agent is purely a *reader* of memory built by the upstream channel — that channel is the only one writing.

```bash
bash "$HONCHO_HELPER" record \
  --peer "$HONCHO_USER_PEER_ID" \
  --content "$ISSUE_TITLE\n\n$ISSUE_BODY"
# Default session_id = "orchestrator-paperclip" — one stable session for all PaperClip
# interactions. Override via --session-id or HONCHO_DEFAULT_SESSION_ID env var.
```

The session is created idempotently if it doesn't exist. Records are processed asynchronously by the deriver (now running hourly as a Container Apps Job), so new facts surface in the next dialectic call after a deriver run.

**What to record**:
- The original issue title + body that the user wrote (attributed to the user) — biographical content
- Optionally Orchestrator's final answer/summary (attributed to orchestrator peer) — for multi-peer session log

**What NOT to record**:
- Per-step tool output traces, curl commands, etc. — noise, not memory

## Pattern for personal-info questions

```bash
# 1. Resolve helper
# 2. Query Honcho
RESPONSE=$(bash "$HONCHO_HELPER" ask --peer "$HONCHO_USER_PEER_ID" --query "$USER_QUESTION")

# 3. Extract the .content field with python (NOT jq — may not be installed)
ANSWER=$(printf '%s' "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('content','') or '(no answer)')")

# 4. Post that exact answer as the comment. Do not paraphrase, do not embellish.
```

## Pitfalls

- **Wrong peer ID** — the most common failure. If `ask` returns "I don't know" or sparse content, you're querying the wrong peer. Run `list-peers` and confirm.
- **Don't substitute or augment** — return Honcho's answer as-is. Do not append things you "also know" — that's the fabrication path the skill exists to replace.
- **Empty content is a real result** — if Honcho says it doesn't know, say "I don't have a record of that." Do not fall back to training data.
