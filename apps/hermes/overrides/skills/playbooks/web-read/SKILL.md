---
name: web-read
description: >
  Read a web page as clean Markdown instead of raw HTML. Use whenever you need
  the actual content of a specific URL — an article, vendor/pricing page,
  changelog, docs page, or any link a search result pointed you to. Trigger
  phrases: "read this page", "what does <url> say", "fetch/open this link",
  "get the article", "extract the content", or any time you would otherwise
  `curl` a URL to read it. The `web-read` CLI is on `$PATH`.
---

# Read a web page as clean Markdown with `web-read`

`web-read` is a CLI on `$PATH` that fetches a URL through [Jina Reader](https://r.jina.ai)
and returns clean, readable **Markdown** — the page's actual content with the
HTML tags, scripts, and navigation chrome stripped server-side.

> Prefer `web-read` over raw `curl` when your goal is to **read** a page. Raw
> `curl` returns the HTML source (tags and all), which buries the content and
> wastes context. Use `curl` only for API calls or when `web-read` can't reach a page.

## Usage

```bash
# Clean Markdown to stdout — capture it so you can grep/quote it later:
web-read "https://example.com/article" > /tmp/page.md
head -c 5000 /tmp/page.md

# Structured JSON ({title, url, content, ...}) instead of Markdown:
web-read --json "https://example.com/article"
```

Only `http://` and `https://` URLs are accepted. Quote the URL.

## Behavior and exit codes

| Exit | Meaning |
|---|---|
| `0` | Success — page content is on stdout. |
| `2` | Usage / validation error (no URL, bad scheme, unknown flag). Fix the call. |
| `3` | Fetch failed — non-2xx from Jina or a timeout. A clear error is printed to **stderr**. Treat as a failed fetch: try another URL or stop; **do not** fabricate content. |

All errors go to **stderr**, so `web-read "$url" > /tmp/page.md` leaves `/tmp/page.md`
holding clean content (or empty on failure) — never an error string mixed into the text.

## Environment

| Variable | Purpose |
|---|---|
| `JINA_API_KEY` | Optional. Sent as a Bearer token for a higher rate limit. Anonymous (no key) still works. |
| `WEB_READ_TIMEOUT` | Optional. Per-request timeout in seconds (default `20`). |

## Privacy note

The target URL transits `r.jina.ai`, a third-party reader service. That is fine
for **public** research. Do **not** point `web-read` at private, internal, or
regulated content — fetch those with `curl` directly instead.
