---
name: agent-delegate
description: >
  Delegate tasks to other agents in the team by creating real PaperClip child
  issues. Covers single-agent delegation and multi-agent coordination, with
  failure handling and idempotency. Trigger phrases: any task outside your
  direct capabilities (email/calendar/drive) that should be routed to Coder,
  Infrastructure, Strategy, Planner, Researcher, Curator, QA, Security, CostGuardian, Coach,
  Business, or Psychology — or any task that says "coordinate", "delegate",
  "split this", "create child issues", or "assign to".
---

# Delegate or Coordinate Tasks via PaperClip Child Issues

> A comment that says "I delegated this to X" is **not delegation**. The other
> agent has no way to see your comment. The only way to delegate is to create
> a real PaperClip issue with that agent's `assigneeAgentId`.

This skill covers two patterns:

- **Single-agent delegation** — one parent task, one child issue, one specialist.
- **Coordination** — one parent task, two or more child issues, multiple specialists.

Both patterns use the same API and the same helper script.

---

## Runtime environment

### Required environment variables

| Variable | Purpose |
|---|---|
| `PAPERCLIP_API_KEY` | Bearer token for the PaperClip API (`ctx.authToken` for embedded Orchestrator; automation JWT for standalone Hermes) |
| `PAPERCLIP_BASE_URL` | PaperClip API base URL — varies by runtime (see table below) |
| `PAPERCLIP_ORIGIN` | `Origin` header value sent with every API request |
| `PAPERCLIP_COMPANY_ID` | PaperClip company UUID (default: `00000000-0000-0000-0000-000000000000`) |

`PAPERCLIP_API_KEY` is **required**. If the others are unset the helper defaults apply:
- `PAPERCLIP_BASE_URL` → `http://localhost:3099` (direct backend; correct for PaperClip-embedded Orchestrator)
- `PAPERCLIP_ORIGIN` → `http://localhost:3100`
- `PAPERCLIP_COMPANY_ID` → `00000000-0000-0000-0000-000000000000`

### Helper path — resolve before first use each session

The helper script lives in different locations depending on which runtime the agent is executing in. **Always discover the path dynamically; never hardcode it.**

```bash
DELEGATE_HELPER=""
for _p in \
  "/paperclip/.hermes/skills/playbooks/agent-delegate/scripts/pc-delegate.sh" \
  "/app/skills/playbooks/agent-delegate/scripts/pc-delegate.sh" \
  "/opt/data/skills/playbooks/agent-delegate/scripts/pc-delegate.sh"; do
  if [ -x "$_p" ]; then DELEGATE_HELPER="$_p"; break; fi
done
[ -z "$DELEGATE_HELPER" ] && { echo "ERROR: pc-delegate.sh not found in any known location" >&2; exit 2; }
```

| Runtime | Expected path |
|---|---|
| PaperClip-embedded Orchestrator (`ca-orchestrator`) | `/paperclip/.hermes/skills/…/pc-delegate.sh` |
| Standalone Hermes gateway (`ca-agent-runtime`) | `/app/skills/…/pc-delegate.sh` |
| Persistent-volume fallback | `/opt/data/skills/…/pc-delegate.sh` |

All examples below use `bash "$DELEGATE_HELPER"`. The script is **not on `$PATH`** — calling it as `pc-delegate` returns exit code 127.

---

## Use the helper, not raw curl

The helper (resolved via `$DELEGATE_HELPER` above):

- Knows the correct API route (`POST /api/companies/{companyId}/issues`).
- Looks up agent UUIDs by name (no need to memorize the table below).
- Sets `parent_id` correctly so child→parent linkage shows in the PaperClip UI.
- Lists existing children before creating, to prevent duplicates.
- Returns a structured success/failure result and **exits nonzero on failure**.
- Hides credential handling — uses `$PAPERCLIP_API_KEY` and `$PAPERCLIP_COMPANY_ID` from the environment.

If `pc-delegate.sh` is missing, fall back to the raw `curl` recipes at the bottom of this document. **Prefer the helper.**

---

## Single-agent delegation

Use when one specialist should handle the entire task.

```bash
# Example: delegate a research task to Curator, parent issue is MRT-46.
bash "$DELEGATE_HELPER" create-child \
  --parent MRT-46 \
  --agent curator \
  --title "Research current Azure Container App scale-to-zero behavior" \
  --description "$(cat <<'EOF'
Research how Azure Container Apps handles scale-to-zero today (2026-04-24):
- Cold-start latency for our Hermes container
- Whether minReplicas=0 is recommended for production
- Cost implications

Sources to check: official Microsoft Learn docs, Azure blog, our own dev cluster metrics.

## Acceptance criteria
- A markdown report at /paperclip/obsidian-vault/Documents/research/aca-scale-to-zero-2026-04.md
- A summary comment posted on this issue with the report path
- Cite at least 3 sources
EOF
)"
```

The helper prints the new child's identifier (e.g. `MRT-47`) on success.

After the child is created:

1. Comment on the parent: `Delegated to Curator. See MRT-47.`
2. **Mark the parent done only if the helper exited 0.** If it exited nonzero, leave the parent open and comment with the failure detail.

---

## Multi-agent coordination

Use when one parent task requires work from two or more specialists.

> **Critical rule:** the parent issue is NOT done until every child issue
> has been created and verified. A coordination task with one failed child
> create is a coordination task that has failed — leave the parent open.

### Step 1 — Plan in your head, not in a comment

Before creating anything, list every child issue you intend to create:

- Target agent (e.g. Infrastructure, Coder)
- Title
- Body / instructions
- Acceptance criteria

If the parent issue's body is ambiguous about who should do what or what "done" means, **ask the principal one clarifying question and stop.** Do not guess. A bad coordination plan is worse than no plan.

### Step 2 — Idempotency check

```bash
bash "$DELEGATE_HELPER" list-children --parent MRT-46
```

Output is JSON: every existing child of MRT-46. If any of your planned children already exists (same target agent + similar title), **skip that create**. Do not duplicate.

### Step 3 — Create each child

```bash
INFRA_CHILD=$(bash "$DELEGATE_HELPER" create-child \
  --parent MRT-46 \
  --agent infrastructure \
  --title "Expose router /health via internal ACA ingress" \
  --description "..." \
  --quiet)

CODER_CHILD=$(bash "$DELEGATE_HELPER" create-child \
  --parent MRT-46 \
  --agent coder \
  --title "Add FastAPI /health endpoint to model router" \
  --description "..." \
  --quiet)
```

`--quiet` makes the helper print only the new identifier (e.g. `MRT-47`) on success, useful for capturing into shell variables.

### Step 4 — Verify every create

The helper exits nonzero on failure. Check `$?` after every call:

```bash
if [ $? -ne 0 ]; then
  # Comment on the parent with the failure detail.
  bash "$DELEGATE_HELPER" comment \
    --issue MRT-46 \
    --body "Delegation to Infrastructure FAILED. Helper exit nonzero. Re-run after fix; do not mark this issue done."
  # STOP. Do not continue. Do not mark parent done.
  exit 1
fi
```

### Step 5 — Update the parent

After every child is verified, post a single summary comment on the parent:

```bash
bash "$DELEGATE_HELPER" comment \
  --issue MRT-46 \
  --body "Coordination plan executed:
- $INFRA_CHILD assigned to Infrastructure: Expose router /health via internal ACA ingress
- $CODER_CHILD assigned to Coder: Add FastAPI /health endpoint to model router

Both children include explicit acceptance criteria. This parent will move to done after the children complete."
```

### Step 6 — Decide parent status

Most coordination parents represent ongoing work — set status to `in_progress`:

```bash
bash "$DELEGATE_HELPER" set-status --issue MRT-46 --status in_progress
```

Mark `done` only if the parent's *only* purpose was to spawn delegations and the principal explicitly said so. **When in doubt, leave it `in_progress`** — the principal can close it.

If you do close a coordination parent as done, use `close-parent --require-children` rather than `set-status --status done` — it refuses the close (exit 6) if zero children exist, which is the canonical phantom-delegation guard:

```bash
bash "$DELEGATE_HELPER" close-parent --issue MRT-46 --require-children
```

`set-status --status done` remains available for non-coordination flows (single ANSWER, RESEARCH, HANDLE), where there are legitimately no children.

---

## Failure handling rules

| Symptom | What to do |
|---|---|
| Helper exits nonzero on `create-child` | Comment on parent with `--body` containing failure detail; do **NOT** mark parent done; stop. |
| HTTP 4xx or 5xx from API | Same as above. The helper extracts the response body into the failure message. |
| Helper exits **124** (timeout) | Command timed out. Do **not** assume the operation partially succeeded. Run `list-children` to check, then retry (see policy below). |
| Same child issue would be created twice (idempotency check finds duplicate) | Skip the create. Note the existing child's identifier in the parent's summary comment. |
| Child create succeeds but `parent_id` was rejected | Helper logs warning. The child exists but is not linked to the parent. Comment on the parent with both child identifiers manually. |
| Cannot resolve agent name | Run `bash "$DELEGATE_HELPER" find-agent <name>`. If unknown, fall back to the agent table in this file or ask the principal. |
| `DELEGATE_HELPER` resolver finds no path | Run `ls -la /paperclip/.hermes/skills/playbooks/ /app/skills/playbooks/ /opt/data/skills/playbooks/ 2>&1` to diagnose, then report to the principal. |

**Retry and stop policy:** Retry the same operation at most **twice**. On the third consecutive failure (any exit code, including 124), stop immediately and post a comment on the parent that includes: the exact command, the exit code, and a one-line summary of the error. Do **not** mark the parent done.

---

## Acceptance criteria template

Every child issue body should end with an `## Acceptance criteria` section. This is what the receiving agent uses to know when they're done.

```markdown
## Acceptance criteria
- <observable outcome 1>
- <observable outcome 2>
- <test or validation step>
- A summary comment posted on this issue when complete
```

Vague criteria like "the work is good" are not acceptance criteria. Specific criteria like "endpoint returns 200 with `{ status: 'ok' }` and is reachable from the cluster's internal DNS" are.

---

## Agent IDs

The helper resolves these by name (`--agent infrastructure`, `--agent coder`, …). The table is here for reference and for the fallback path when the helper is unavailable.

| Agent | Role | ID |
|-------|------|-----|
| Strategy | Strategic Commander | 00000000-0000-0000-0000-000000000000 |
| Planner | Tactical Planner | 00000000-0000-0000-0000-000000000000 |
| Coder | Application Coder | 00000000-0000-0000-0000-000000000000 |
| Infrastructure | Infrastructure Lead | 00000000-0000-0000-0000-000000000000 |
| Curator | Knowledge Librarian | 00000000-0000-0000-0000-000000000000 |
| Researcher | Research Analyst | 00000000-0000-0000-0000-000000000000 |
| QA | QA Engineer | 00000000-0000-0000-0000-000000000000 |
| Security | Security Analyst | 00000000-0000-0000-0000-000000000000 |
| CostGuardian | FinOps Monitor | 00000000-0000-0000-0000-000000000000 |
| Coach | Career Coach | 00000000-0000-0000-0000-000000000000 |
| Business | Business Strategy | 00000000-0000-0000-0000-000000000000 |
| Psychology | Behavioral Psychologist | 00000000-0000-0000-0000-000000000000 |

## Routing guide

| Domain | Route To |
|--------|----------|
| Code, APIs, bugs | Coder |
| Terraform, Azure, CI/CD | Infrastructure |
| Architecture, big decisions | Strategy |
| Task breakdown, planning | Planner |
| Research, benchmarks, write-ups | Curator or Researcher |
| Code review, QA | QA |
| Security audit | Security |
| Azure costs, budgets | CostGuardian |
| Career, leadership | Coach |
| Business strategy | Business |
| Psychology, stress | Psychology |

---

## Hard rules

- **Do NOT delegate GWS tasks.** Email/calendar/Drive only Orchestrator has the credentials for; other agents do not. Handle those yourself.
- **Do NOT mark a coordination parent done if any child create returned nonzero.**
- **Do NOT skip verification.** Always check the helper's exit code or HTTP status before continuing.
- **Do NOT invent agent UUIDs.** Use the table above or `pc-delegate find-agent`.
- **Do NOT create duplicate children for the same parent + agent + title.** Always run `list-children` first.

---

## Fallback: raw curl recipes

Use these only if `pc-delegate.sh` is missing or unreachable. The helper does these correctly; these are documented here so an agent can recover if the helper itself breaks.

### Look up the company ID

```bash
COMPANY_ID="${PAPERCLIP_COMPANY_ID:-00000000-0000-0000-0000-000000000000}"
```

### Look up the parent's UUID (needed for parent_id)

The PaperClip API uses the `identifier` (e.g. `MRT-46`) in URLs but the `id` (UUID) for the `parent_id` field on child creation.

```bash
_PC_BASE="${PAPERCLIP_BASE_URL:-http://localhost:3099}"
_PC_ORIGIN="${PAPERCLIP_ORIGIN:-http://localhost:3100}"

PARENT_UUID=$(curl -s "${_PC_BASE}/api/issues/MRT-46" \
  -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
  -H "Origin: ${_PC_ORIGIN}" \
  -H "X-Automation-Sub: paperclip-agent" | jq -r .id)
```

### Create a child issue

```bash
curl -s -X POST "${_PC_BASE}/api/companies/$COMPANY_ID/issues" \
  -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
  -H "Origin: ${_PC_ORIGIN}" \
  -H "X-Automation-Sub: paperclip-agent" \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg title "Child issue title" \
    --arg description "Child issue body with acceptance criteria" \
    --arg assigneeAgentId "AGENT_UUID_FROM_TABLE" \
    --arg parent_id "$PARENT_UUID" \
    '{title: $title, description: $description, assigneeAgentId: $assigneeAgentId, parent_id: $parent_id}')"
```

**Field name notes (PaperClip OSS quirks):**
- The issue body field is `description`. Some older docs say `body`; that may also work as an alias, but `description` is the canonical name (matches the OSS schema).
- The comment body field is `body` (different from issue creation). Don't confuse them.
- `parent_id` takes a UUID, not an identifier.

### Post a comment

```bash
curl -s -X POST "${_PC_BASE}/api/issues/MRT-46/comments" \
  -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
  -H "Origin: ${_PC_ORIGIN}" \
  -H "X-Automation-Sub: paperclip-agent" \
  -H "Content-Type: application/json" \
  -d '{"body":"Comment text here"}'
```

### Update issue status

```bash
curl -s -X PATCH "${_PC_BASE}/api/issues/MRT-46" \
  -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
  -H "Origin: ${_PC_ORIGIN}" \
  -H "X-Automation-Sub: paperclip-agent" \
  -H "Content-Type: application/json" \
  -d '{"status":"in_progress"}'
```

Valid statuses observed in the wild: `backlog`, `todo`, `in_progress`, `done`, `cancelled`. Test with the helper or `GET /api/issues/<id>` to see what's actually accepted.

### List existing children of a parent

```bash
curl -s "${_PC_BASE}/api/companies/$COMPANY_ID/issues" \
  -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
  -H "Origin: ${_PC_ORIGIN}" \
  -H "X-Automation-Sub: paperclip-agent" \
  | jq --arg pid "$PARENT_UUID" '[.[] | select(.parent_id == $pid)]'
```
