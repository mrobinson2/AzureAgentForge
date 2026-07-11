# Skill: Executive-Assistant Calendar Prep

- **Slug:** `executive-assistant-calendar-prep`
- **Used by:** an executive-assistant agent (e.g. the Generalist or Orchestrator role running in personal-assistant mode)
- **Toolsets:** terminal, file
- **Trust tier:** High-Trust internal

## Purpose

Before each meeting on the operator's personal calendar, produce a
one-paragraph briefing they can read in 30 seconds: who is attending, what
memory recalls about them, what the agenda doc says, and what conflicts to flag.

This is a meeting **primer** (before-the-fact), not a meeting **summary**
(after-the-fact).

## When to use

Two trigger patterns:

1. **Day-before sweep** (preferred) — after the `executive-assistant-daily-digest`
   assembler runs, it queues a small one-off issue for each of tomorrow's
   meetings that lacks a prep brief. The assistant processes these at low
   priority through the day.
2. **Just-in-time** (fallback) — a 30-minute-interval cron during business hours
   scans the next 60 minutes for meetings without a brief and creates issues.
   Bounded to the next hour; beyond that, the day-before sweep covered it.

## Mode gating

**Personal-assistant mode only.** This operates against the operator's
**personal** calendar. Business meetings, customer calls, and any
customer-facing intake calendar are out of scope — those route through the
work/engineering surfaces, not personal-life prep.

## Inputs

| Var | Meaning |
|---|---|
| `CALPREP_AUTO_REMINDER` | Default `0`; set `1` to insert a reminder event at meeting-time minus 5 min. |
| `CALPREP_LOOKAHEAD_HOURS` | Default `24` for the day-before sweep, `1` for just-in-time. |
| `HONCHO_USER_PEER_ID` | The operator's personal memory peer (read-only). |
| Event id | The calendar event to brief. |

## Procedure

1. **Pull the event** — `calendar get --event-id <id>` → attendees, time,
   attached docs, organizer.
2. **For each attendee** — `pc-honcho ask --peer "$HONCHO_USER_PEER_ID"
   --query "what do you know about <attendee email>?"`. Truncate to one sentence
   per attendee. Drop any attendee the operator flagged sensitive
   (`personal_contact:no_briefing`) — list them only as "<name> (external)".
3. **For each attached doc** — pull **metadata only** (title, last-edited,
   owner). **Do not pull the body.**
4. **Suggested talking points:**
   - Agenda doc exists → extract top-level headings (a structural-only API
     call) and present them as candidate topics.
   - No agenda doc but memory has context → paraphrase what's outstanding from
     the most recent thread.
   - Neither → just the attendee list. Do not fabricate.
5. **Conflict detection** — check for other reminders or events overlapping this
   one. On overlap, surface a flag and (if enabled) an auto-snooze action.
6. **Post the brief, then mark the prep issue done.** Do **not** modify the
   actual calendar event.

## Output format

```markdown
## <Meeting Title> — Tomorrow 9:00–9:30

**Attendees** (3):
- Sam Rivera (PM at Contoso) — last touched 3 weeks ago re: Q4 roadmap
- Alex Kim (Eng) — frequent collaborator, no recent context
- Jordan Lee (external, first meeting) — no memory context

**Agenda doc:** "Q4 roadmap review" (last edited yesterday) — [link]

**Suggested talking points** (from agenda doc + memory):
- Confirm the billing migration moved to Q1
- Decision needed on whether to deprecate the v1 API by year-end

**Flags:**
- ⚠ No agenda doc (you're hosting — consider sending one)
- ⚠ Conflict: 9:15 reminder "call the dentist" overlaps; auto-snoozed to 10:30
```

## Guardrails

- **The assistant never modifies the meeting itself.** No RSVP changes, no time
  changes, no attendee changes.
- **It may add a reminder to the operator's own calendar** at meeting-time minus
  5 min, but only if `CALPREP_AUTO_REMINDER=1`. Default off.
- **No agenda-doc body extraction.** Headings and metadata only.
- **Read-only against memory.** No memory writes from this skill; the
  post-meeting summary flow (future work) is where writes happen.
- **Never write briefings to a shared team channel.** Personal-life prep stays
  on the personal surface.
- **Drafts, not sends.** If the operator later asks the assistant to send an
  agenda, that is a separate, approval-gated drafting step.

## Failure handling

- **No agenda doc and no memory context** — post the attendee list only; do not
  invent talking points.
- **Permission denied on a doc** — surface a flag ("agenda doc exists but the
  assistant can't read it — likely not shared with the assistant's account"),
  do not guess its contents.
- **Memory service down** — attendee lines drop to "no memory context
  available"; still post the brief.
- **Cancelled event** — if cancelled after the prep issue was created, post
  "Cancelled before prep — no action" and mark done.

## Edge cases

- **Recurring meetings** — treat each occurrence separately; de-dupe by
  event-instance id.
- **All-day events / OOO / focus blocks** — skip; no prep needed.
- **Meetings with only the operator attending** — skip; these are personal time
  blocks.

## Related skills

- [`executive-assistant-daily-digest.md`](executive-assistant-daily-digest.md) — the day-before sweep is queued off its cron; build the digest first so this skill's scheduling comes for free.
- [`executive-assistant-email-triage.md`](executive-assistant-email-triage.md) — routes calendar-invite emails here.
