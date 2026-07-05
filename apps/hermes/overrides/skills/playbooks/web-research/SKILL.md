---
name: web-research
description: >
  How to do web research with the right tool and a fallback when one fails:
  which search backend to use, how to read a page cleanly, how to get a video's
  transcript, and how to check that the backends are healthy. Use at the start of
  any research/lookup/"find out about X" task, or when a research tool returns
  empty/erroring results and you need the fallback. Pulls together exa-search,
  brave-search, web-read, video-transcript, and the research-doctor health check.
---

# Web research: pick the right backend, with fallbacks

The platform exposes several research CLIs on `$PATH`. Use the one that fits the
job; if it fails or returns empty, fall back down the chain rather than giving up
(or fabricating).

## Routing table

| Need | Preferred | Fallback | Notes |
|---|---|---|---|
| **Search** — conceptual / "things like X" / exploratory | `exa-search` | `brave-search` → `ddgs` | Exa is semantic; finds relevant pages without exact words. |
| **Search** — fresh / news / exact term | `brave-search` | `exa-search` → `ddgs` | Brave accepts cloud IPs (DuckDuckGo blocks them). |
| **Read a page** — get the actual content of a URL | `web-read` | raw `curl` + strip | `web-read` returns clean Markdown; `curl` returns raw HTML. |
| **Video** — transcript of a YouTube/Vimeo/etc. link | `video-transcript` | (Whisper, auto) | yt-dlp captions; Whisper fallback runs automatically if `GROQ_API_KEY` is set. |

All search tools emit the same JSON shape (`[{title, href, body}]`), so you can
swap one for another without changing how you parse the result. When a question
has both an exploratory and a freshness angle, run **both** search backends and
merge — they surface different pages.

## Fallback rule

If your preferred tool returns an **empty result or a non-zero exit**, try the
fallback before concluding "nothing found". An empty result from one backend is
not evidence that the information doesn't exist — it's often that backend being
blocked, rate-limited, or unconfigured. Only after the preferred tool **and** its
fallback come up empty should you treat the query as genuinely unanswerable.

## Health check — `research-doctor`

Before a research-heavy task (or when a tool behaves oddly), check which backends
are actually up:

```bash
research-doctor          # human-readable: OK / SKIP / DOWN per backend
research-doctor --json   # [{name, ok, status, detail}] for scripting
```

- **OK** — the backend responded.
- **SKIP** — not configured here (e.g. no API key); not an error, just unavailable.
- **DOWN** — configured but failing; route around it and, if persistent, flag it
  to Infrastructure. `research-doctor` exits non-zero when any backend is DOWN.

The watchdog runs the equivalent probe on a schedule (opt-in via
`WATCHDOG_RESEARCH_PROBE`) and files an issue when a reachable backend goes down,
so a degraded research capability gets surfaced even if no agent hit it yet.

## Environment / keys

| Tool | Key | If unset |
|---|---|---|
| `exa-search` | `EXA_API_KEY` | exits 1 → fall back to `brave-search` |
| `brave-search` | `BRAVE_SEARCH_API_KEY` | exits 1 → fall back to `ddgs` |
| `web-read` | `JINA_API_KEY` (optional) | works anonymously (lower rate limit) |
| `video-transcript` | `GROQ_API_KEY` (optional) | captions only; no Whisper fallback |
