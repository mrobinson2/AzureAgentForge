# Skill: Executive-Assistant Email Triage

- **Slug:** `executive-assistant-email-triage`
- **Used by:** an executive-assistant agent (e.g. the Generalist or Orchestrator role running in personal-assistant mode)
- **Toolsets:** terminal, file
- **Trust tier:** High-Trust internal

## Purpose

In personal-assistant mode, read the operator's personal inbox, classify each
thread into one of four buckets, take only low-blast-radius actions (label —
never archive, never send), and surface the rest with optional drafted replies.
The design principle: labeling is reversible; archiving and sending are not.

## When to use

- Called by the `executive-assistant-daily-digest` skill during digest assembly.
- An ad-hoc request: "triage my inbox."
- A tracked issue assigned to the assistant whose body matches `triage` +
  `email`/`inbox`.

Not invoked on every new email — that would burn budget and create noise.

## Mode gating

**Personal-assistant mode only.** The mode is determined by issue origin:

| Origin | Mode | Applies? |
|---|---|---|
| Personal DM, chat relay, daily-digest cron | personal-assistant | yes |
| Shared team channel, work-router auto-assignment | work/engineering | no — use a read-only email-read helper if needed |

If invoked from work mode by mistake, refuse and route via the agent's
mode-routing self-check.

## Inputs

| Var | Meaning |
|---|---|
| `EMAIL_CREDENTIALS_FILE` | e.g. `/secrets/email-credentials` — the operator's **personal** mailbox credentials, distinct from any tenant/business mailbox. |
| `INBOX_LOOKBACK_HOURS` | Default `24`, overridable per issue body. |
| `HONCHO_USER_PEER_ID` | Personal memory peer, for contact recognition and pinned-contact priority. Do **not** read `customer_*` / `prospect_*` peers — those are a customer-facing sandbox this role has no business reading. |

## Buckets and actions

| Bucket | Definition | Action taken | Action surfaced |
|---|---|---|---|
| `ACT_NOW` | Needs a response within 24h. Vendor invoice with a deadline; school/parent comm; person waiting on a reply; calendar conflict. | Drafts a reply into the Drafts folder, prefixed `[assistant draft]`. Adds label `assistant-act-now`. | One bullet per thread in the digest: subject + sender + first 2 lines of the draft. |
| `WAITING` | The operator owes nothing but is waiting on someone. Last message sent by the operator, no reply for >2 days. | Adds label `assistant-waiting`. | "Waiting on others" section with last-touched date. No auto-nudge. |
| `FYI` | Newsletter, notification, mailing list, marketing, threads untouched for 30+ days. | Adds label `auto-fyi`. **Does not archive.** | Counted in the digest header. |
| `ARCHIVE_CANDIDATE` | Resolved threads — a clear "thanks!" / "no further action" pattern. | Adds label `assistant-archive-candidate`. **Does not archive.** | Surfaces a count + "reply 'archive candidates' to clear." The operator archives in their own client. |

## Procedure

1. Fetch inbox threads within `INBOX_LOOKBACK_HOURS`.
2. For each thread, skip anything in a protected/sensitive label or the
   explicit no-touch label **before reading its body** (see Guardrails).
3. Recognize the correspondent via memory (`pc-honcho ask --peer
   "$HONCHO_USER_PEER_ID"`) for priority hints; never read business peers.
4. Classify into exactly one bucket using the definitions above.
5. Apply the bucket's label (idempotent — re-running must not duplicate labels).
6. For `ACT_NOW`, compose a draft into the Drafts folder — **never send it**.
7. Write a JSON summary for the digest assembler.

## Output format

- Updated labels (idempotent).
- Drafts in the Drafts folder for `ACT_NOW` items, prefixed `[assistant draft]`.
- A JSON summary for the digest assembler, written to a run-scoped temp file:

```json
{
  "run_id": "2026-07-11T13:02:00Z",
  "lookback_hours": 24,
  "unread_total": 12,
  "act_now": [
    { "subject": "...", "sender": "vendor@example.com", "draft_preview": "..." }
  ],
  "waiting": [ { "subject": "...", "last_touched_days": 4 } ],
  "fyi_count": 9,
  "archive_candidate_count": 3,
  "protected_untouched_count": 5
}
```

## Guardrails

- **Never sends. Drafts only.** Send is gated on an explicit per-message "send
  it" in the same surface where the draft was surfaced.
- **Never archives `FYI` silently.** Labeling is reversible; archiving is
  psychologically not.
- **No filter rules.** Never create or modify inbox filter rules — they run
  unsupervised and can hide real mail.
- **Sensitive labels are off-limits.** Threads in `personal-finance`, `legal`,
  `medical`, `family-private` are never classified or drafted — just counted:
  "5 untouched threads in protected labels."
- **Never read a thread the operator explicitly hid** (a `no-touch` label).
- **Business/tenant email stays out.** Threads from business correspondents
  (domain match against business memory peers) are skipped — they belong to the
  work-mode routing, not personal triage.

## Failure handling

- **Auth/token expired** — do not guess; report "email triage skipped: token
  expired" to the caller and continue the rest of the digest.
- **Auto-replies and bounces** — classify as `FYI` even from a known contact;
  never draft a reply to a mailer-daemon.
- **Calendar invite emails** — route to `executive-assistant-calendar-prep`
  instead of drafting a reply.
- **Encrypted/PGP threads** — label `assistant-encrypted-skipped`, surface in
  the digest, do not attempt to read.
- **Reply-all chains with >5 recipients** — never draft; the operator decides
  personally.

## Related skills

- [`executive-assistant-daily-digest.md`](executive-assistant-daily-digest.md) — the caller.
- [`executive-assistant-calendar-prep.md`](executive-assistant-calendar-prep.md) — where calendar invites are routed.
