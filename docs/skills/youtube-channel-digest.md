# Skill: YouTube Channel Digest

- **Slug:** `youtube-channel-digest`
- **Used by:** the content-digest agent (e.g. the Researcher role)
- **Toolsets:** terminal, file, browser
- **Trust tier:** High-Trust internal

## Purpose

Replace manually scrubbing a curated set of YouTube channels for new uploads.
On a schedule, produce one digest covering every new video published since the
last run: a few key-point bullets per video and a link to watch. Delivered by
email, grouped by theme.

## When to use

- A scheduled content roll-up (e.g. twice daily) for a fixed list of channels
  the operator follows.
- Not a live subscription feed — it batches new uploads between runs.

## Inputs

| Var / secret | Meaning |
|---|---|
| `TRANSCRIPT_API_KEY` | Secret for a transcript API used to fetch captions. Store in your secret manager, mount as a file. |
| `DIGEST_EMAIL_TO` | Recipient, e.g. `operator@example.com`. |
| Email sender creds | SMTP username + app password for the sending account (e.g. `assistant@example.com`), stored as secrets. |
| `channels.json` | The channel registry (below), mounted read-only. |
| `DIGEST_VIDEO_CAP` | Max videos summarized per run. Default `10`. |
| `DIGEST_TZ` | IANA timezone for the schedule guard, e.g. `America/Chicago`. The cron alone is not authoritative. |

### Channel registry

A config file mounted read-only. `channelId` is the stable `UC...` id, resolved
once from the handle; `theme` drives digest grouping:

```json
[
  { "handle": "@exampleAI",     "channelId": "UCxxxx", "theme": "AI" },
  { "handle": "@exampleLeader", "channelId": "UCyyyy", "theme": "Leadership" }
]
```

## Procedure

Split the pipeline: a helper runs the mechanical, cost-bearing steps (1–4) and
emits structured JSON; the agent does the editorial and delivery steps (5–7).

1. **Discover.** For each channel, read the YouTube RSS feed
   `https://www.youtube.com/feeds/videos.xml?channel_id=<id>` (free, no API
   credits) and keep entries published after that channel's watermark. If RSS
   returns empty (some cloud-provider IPs are blocked by certain endpoints),
   fall back to the transcript API's channel endpoint.
2. **Select.** Drop videos already in processed-state. Newest first, capped at
   `DIGEST_VIDEO_CAP`. Record the overflow count.
3. **Transcript.** For each selected video, fetch the transcript via the
   transcript API. If a video has no captions, mark `status: no-transcript` and
   skip summarization.
4. **Summarize.** Send each transcript to the model router on the **economy
   tier** with a strict prompt that returns 3–5 key-point bullets. Attach title,
   channel, theme, duration, `publishedAt`, and url.
5. **Assemble** (agent, standard tier). Compose the digest grouped by theme,
   rendering only themes that have videos this run, ordered by channel then
   recency, with light editorial framing. Keep the grouping even if only one
   theme is active today, so adding channels later is a registry edit only.
   Overflow and per-video errors become a footer line.
6. **Deliver** (email). Send with the runtime's SMTP email tool (`smtplib` +
   `MIMEText` over your SMTP relay) from the sending account to
   `DIGEST_EMAIL_TO`. Subject: `<theme set> digest: <slot> YYYY-MM-DD
   (<n> videos)`. Body: the assembled digest as HTML.
7. **Commit state and close.** Update the processed-state file **only after**
   delivery succeeds. Post the digest as a single PaperClip comment and mark the
   issue done.

### Helper output

```json
{
  "slot": "morning",
  "videos": [
    { "title": "...", "channel": "...", "theme": "AI", "duration": "18:42",
      "url": "https://youtu.be/<id>", "bullets": ["...","..."], "status": "ok" }
  ],
  "overflow": 0,
  "errors": []
}
```

## Output format (email body)

```markdown
# AI + Leadership digest: morning 2026-06-28 (6 videos)

## AI (4)

### <Title> · <Channel> · 18:42
- key point
- key point
- key point
[Watch](https://youtu.be/<id>)

## Leadership (2)

### <Title> · <Channel> · 41:05
- key point
- key point
[Watch](https://youtu.be/<id>)

---
3 more videos exceeded today's cap of 10. 1 video had no transcript and was skipped.
```

## State and dedup

`state/youtube-digest-state.json` on a persistent share:

```json
{
  "channels": { "UCxxxx": { "lastPublishedAt": "2026-06-28T05:30:00Z", "lastVideoId": "abc" } },
  "processed": { "abc": "2026-06-28T11:00:12Z" }
}
```

- A video is marked processed **only after** the digest is delivered, so a
  crashed run re-processes cleanly on the next slot.
- Prune `processed` entries older than 14 days (RSS won't resurface a video that
  old).
- First-ever run: default each channel watermark to now minus 24 hours, so the
  first digest isn't a giant backlog.

## Cost controls

- Cap videos per slot (`DIGEST_VIDEO_CAP`). RSS discovery spends no transcript
  credits. Per-video summaries run on the **economy tier**; the higher tier is
  used only for the final assembly. Set a daily budget for the agent generous
  enough for the expected transcript count (roughly 10–20/day for ~15 channels).
- Never spend a transcript credit on an already-processed video.

## Guardrails

- **Draft-to-owner delivery only.** The digest goes to the operator, not to any
  external audience — this skill never publishes outward or replies to third
  parties. If an external-facing variant is ever added, it must be
  approval-gated before send.
- **One digest per slot.** Detect the existing slot issue and no-op duplicates.
- **Single comment per issue.** No follow-ups unless the operator replies.
- **Timezone correctness** comes from `DIGEST_TZ`, not the cron alone.
- **An empty digest is still delivered** ("No new videos since the last
  digest").
- **State is committed only after a successful send.**

## Failure handling

Deliver what you have; be honest about what you couldn't fetch; never fabricate
a summary for a video you couldn't read.

- **RSS empty from a blocked IP** — fall back to the transcript API's channel
  endpoint; if both are empty, treat as no new videos.
- **Transcript API down or rate-limited** — deliver whatever summarized, list
  the rest under "could not fetch, will retry next slot," and do **not** mark
  those processed.
- **No captions on a video** — mark `no-transcript`, note it in the footer, mark
  it processed so it isn't retried forever.
- **Email send fails** — post the digest as the PaperClip comment anyway so it
  isn't lost, and leave the issue open with an error label for retry.
- **Budget exhausted mid-run** — summarize fewer videos, deliver a partial
  digest with a note, mark only the delivered videos processed.
- **Channel handle unresolvable** — skip it, note it once, keep going.

## Optional: a short push nudge

An optional second delivery channel (SMS, an iMessage bridge, or a chat push)
can send a short nudge — video count, top 3 titles, the links, and "full digest
in your email." Gate it on a one-time connectivity test. If the push channel is
unreachable at send time, the email still goes and the nudge is skipped with a
logged note.
