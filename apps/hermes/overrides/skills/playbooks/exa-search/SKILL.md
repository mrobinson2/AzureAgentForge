---
name: exa-search
description: >
  Semantic / neural web search via the Exa API. Use for conceptual, exploratory,
  or "find things like X" research where keyword search underperforms — e.g.
  "papers about agent memory", "companies similar to <X>", "blog posts arguing
  <position>". Trigger phrases: "search for", "find research/papers/articles on",
  "what's out there about", "find similar", "semantic search". Complements
  brave-search (use brave for fresh/news/exact-term). The `exa-search` CLI is on
  `$PATH` and emits the same JSON shape as brave-search.
---

# Semantic web search with `exa-search`

`exa-search` is a CLI on `$PATH` that queries the [Exa](https://exa.ai) neural
search API and returns results in the **same** ddgs-compatible JSON shape as
`brave-search` — an array of `{title, href, body}` — so it's a drop-in alongside
the other search tools.

## When to use which search backend

| Backend | Best for |
|---|---|
| **`exa-search`** | Conceptual / semantic / exploratory queries: "frameworks like LangGraph", "research on agent memory", "find articles arguing X". Neural search finds relevant pages even when they don't contain your exact words. |
| **`brave-search`** | Fresh / breaking / exact-term queries: news, a specific product page, a known title, anything time-sensitive. |

When a question has both an exploratory and a freshness angle, run **both** and merge — they surface different pages.

## Usage

```bash
# Naive form:
exa-search "open-source agent frameworks with durable execution"

# ddgs-compatible form (same flags as brave-search), e.g. 8 results:
exa-search text -k "open-source agent frameworks" -m 8 -o json
```

Output is a JSON array; each element has `title`, `href`, and `body` (a short
text excerpt from the page). Parse it the same way you parse `brave-search`
output.

## Behavior and exit codes

| Exit | Meaning |
|---|---|
| `0` | Success — JSON array on stdout (may be `[]` if Exa found nothing). |
| `1` | `EXA_API_KEY` is not set. The wrapper can't run without it. |
| `2` | Empty query. |
| `3` / `4` | Exa API error (non-2xx) or a transport failure — a clear message is printed to stderr. |

## Environment

| Variable | Purpose |
|---|---|
| `EXA_API_KEY` | **Required.** Exa API key (free tier ~1,000 searches/month). In Azure deploys it is mounted from the `exa-api-key` Key Vault secret when `exa_search_enabled = true`. |

If `exa-search` exits `1`, Exa isn't configured in this environment — fall back to
`brave-search`.
