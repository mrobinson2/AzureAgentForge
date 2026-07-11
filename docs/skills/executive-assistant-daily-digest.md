# Skill: Executive-Assistant Daily Digest

- **Slug:** `executive-assistant-daily-digest`
- **Used by:** an executive-assistant agent (e.g. the Generalist or Orchestrator role running in personal-assistant mode)
- **Toolsets:** terminal, file
- **Trust tier:** High-Trust internal

## Purpose

Produce one issue per day, early in the operator's local morning, summarizing
the day ahead and the personal backlog: calendar, triaged email, unreplied
messages, and reminders due. It replaces the "scroll through everything when I
wake up" pattern with a single scannable brief.

## When to use

- A once-a-day scheduled run (cron) in personal-assistant mode.
- Not on every new event — the digest is a daily roll-up, not a live feed.

## Mode gating

**Personal-assistant mode only.** The scheduled trigger tags the issue for
personal-assistant mode. If the digest ever fires while the agent is in
work/engineering mode (it shouldn't — separate surfaces), refuse and surface a
misconfiguration rather than mixing personal and work context.

## Inputs

| Var | Meaning |
|---|---|
| `DIGEST_TZ` | IANA timezone string, e.g. `America/New_York`. **Mandatory** — the cron alone is not authoritative for local time. |
| `DIGEST_RUN_SUNDAY` | Default `0`; set `1` to enable Sunday digests. Default cadence is Mon–Sat. |
| `DIGEST_INCLUDE_WAITING` | Default `1`; set `0` to suppress the "waiting on others" section (e.g. during vacation). |
| `HONCHO_USER_PEER_ID` | The operator's personal memory peer, for attendee/context lookups. |

## Trigger

A scheduled job (a native scheduler Routine, or an Azure Container Apps Job
cron adjusted for `DIGEST_TZ`) creates one PaperClip issue assigned to the
assistant agent, tagged for personal-assistant mode and marked auto-created:

```
POST /api/companies/$COMPANY_ID/issues
{
  "title": "Morning digest YYYY-MM-DD",
  "body":  "Auto-created by the daily-digest schedule. The assistant will reply with the digest.",
  "assigneeAgentId": "<assistant-agent-id>",
  "labels": ["assistant-digest", "personal-assistant", "auto-created"]
}
```

The scheduler wakes the agent; the agent recognizes the label, switches to
personal-assistant mode, runs the assembler, posts a single comment, marks the
issue done. The trigger container is tiny — it mints the issue and exits; the
agent does the assembly.

## Procedure

1. **Calendar.** List today's events (`calendar list --from "today 00:00"
   --to "today 23:59"`). For each event: look up attendees via the memory helper
   (`pc-honcho ask --peer "$HONCHO_USER_PEER_ID" --query "what do you remember
   about <attendee>?"`), truncate to one line each. If an event has an attached
   doc, pull **metadata only** (title, last-edited) — never the body. Flag any
   event with no agenda doc and more than two external attendees.
2. **Email triage.** Invoke the `executive-assistant-email-triage` skill with a
   24-hour lookback and read its JSON summary (unread count + `ACT_NOW` items).
3. **Unreplied messages.** Fetch bridged chat/SMS issues created since the last
   digest that are still unreplied (a chat bridge is agent-agnostic
   infrastructure — the bridge handler creates these issues).
4. **Reminders due today.** Fetch issues assigned to the assistant with
   `due_date=today` and `status=todo`.
5. **Compose** the digest (format below).
6. **Post + mark done.**

## Output format

```markdown
## Today (YYYY-MM-DD)

**Calendar** (3 events):
- 9:00–9:30 — 1:1 with Sam Rivera ([brief context from memory])
- 11:00–12:00 — Vendor dinner prep (no agenda doc; flagged)
- 14:00–14:30 — Team standup (no prep needed)

**Email since yesterday** (12 unread, 3 ACT_NOW):
- ACT_NOW: invoice from <vendor> due Friday
- ACT_NOW: school pickup change request
- ACT_NOW: <subject>
- 9 FYI labeled `auto-fyi`

**Waiting on others** (2):
- <person> — re: <topic> (last touched 4 days ago)

**Reminders due today** (1):
- Renew domain registration (set Tuesday)

**Anything I should not have done?** Reply "undo <line>" to revert.
```

## Guardrails

- **Draft only; the operator approves before anything external is sent.** The
  digest itself is an internal post; any `ACT_NOW` reply it surfaces stays a
  draft until the operator says send.
- **One issue per day.** If the schedule fires twice, detect the existing day's
  issue (`label=assistant-digest AND created_today`) and no-op the duplicate.
- **Single comment per issue.** No follow-ups unless the operator replies.
- **Timezone correctness matters.** `DIGEST_TZ` is mandatory; derive local time
  from it, not from the cron expression.
- **An empty digest is still posted.** "Nothing on the calendar today, inbox is
  clear" is a valid, honest digest.
- **Never include sensitive-label content.** Replace with a count —
  "5 untouched in protected labels" — never the subject lines.
- **Never bridge personal data into work-mode artifacts.** Don't write digest
  content into a shared team channel.

## Failure handling

Post the digest with an honest header line for each degraded source; never
fabricate the missing section.

- **Email token expired** — header "⚠️ Email triage skipped: token expired";
  continue with calendar + reminders.
- **Memory service down** — attendee context lines drop to "no memory context
  available".
- **Chat/SMS bridge offline** — "Messages section skipped — bridge unreachable"
  (an infra failure; route the fix to the platform/ops role).
- **Nothing to report** — post the empty digest, don't skip the run.

## Cron sketch (generic)

```hcl
resource "azurerm_container_app_job" "assistant_digest" {
  count                        = var.assistant_personal_enabled ? 1 : 0
  name                         = "ca-assistant-digest-${var.environment}"
  resource_group_name          = var.resource_group_name
  location                     = var.location
  container_app_environment_id = local.container_app_environment_id

  schedule_trigger_config {
    cron_expression          = "0 14 * * 1-6" # adjust to DIGEST_TZ
    parallelism              = 1
    replica_completion_count = 1
  }
  replica_timeout_in_seconds = 300
}
```

## Related skills

- [`executive-assistant-email-triage.md`](executive-assistant-email-triage.md) — invoked in step 2.
- [`executive-assistant-calendar-prep.md`](executive-assistant-calendar-prep.md) — the day-before sweep is queued off this digest's cron.
