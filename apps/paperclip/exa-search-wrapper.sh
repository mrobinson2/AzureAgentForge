#!/bin/sh
# Smart wrapper around the Exa search API (https://exa.ai).
#
# Research-grade semantic web search, alongside brave-search. Exa is neural/
# semantic — strong for conceptual queries ("frameworks like X", "papers about
# Y") where keyword search underperforms. Keep brave-search for fresh/news and
# exact-term queries; this complements it.
#
# Usage forms accepted (mirrors brave-search-wrapper.sh / ddgs so AGENTS.md
# examples don't have to change beyond the binary name):
#   exa-search "query"                       # naive form
#   exa-search text -k "query" -m 5 -o json  # ddgs-compatible form
#
# Output: JSON array of {title, href, body} objects (ddgs-compatible shape) so
# downstream parsing in agent prompts continues to work.
#
# Requires EXA_API_KEY in the environment (free tier ~1,000 searches/month).

set -e

if [ -z "${EXA_API_KEY:-}" ]; then
  echo "ERROR: EXA_API_KEY env var not set" >&2
  exit 1
fi

# Parse args. Default count = 5.
QUERY=""
COUNT=5

case "$1" in
  text|news|web|"")
    # ddgs-compatible form: exa-search text -k "query" -m 5 -o json
    shift
    while [ $# -gt 0 ]; do
      case "$1" in
        -k|--keywords) QUERY="$2"; shift 2 ;;
        -m|--max-results) COUNT="$2"; shift 2 ;;
        -o|--output) shift 2 ;;  # ignore; we always emit JSON
        *) shift ;;
      esac
    done
    ;;
  --help|-h)
    echo "Usage: exa-search \"query\"  OR  exa-search text -k \"query\" -m 5 -o json" >&2
    exit 0
    ;;
  *)
    # Naive form: everything after the binary is the query.
    QUERY="$*"
    ;;
esac

if [ -z "$QUERY" ]; then
  echo "ERROR: empty query" >&2
  exit 2
fi

# Call Exa. python3 is in the base image.
exec python3 - "$QUERY" "$COUNT" <<'PYEOF'
import json
import os
import sys
import urllib.error
import urllib.request

query = sys.argv[1]
try:
    count = int(sys.argv[2])
except ValueError:
    count = 5
api_key = os.environ["EXA_API_KEY"]

payload = json.dumps({
    "query": query,
    "numResults": count,
    # Ask for a short text excerpt so each result has a usable snippet (body).
    "contents": {"text": {"maxCharacters": 800}},
}).encode("utf-8")

req = urllib.request.Request(
    "https://api.exa.ai/search",
    data=payload,
    method="POST",
    headers={
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "azureagentforge-exa-search/1.0",
    },
)

try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
except urllib.error.HTTPError as e:
    print(f"ERROR: Exa API returned HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}", file=sys.stderr)
    sys.exit(3)
except Exception as e:
    print(f"ERROR: Exa API call failed: {e}", file=sys.stderr)
    sys.exit(4)

results = []
for item in (data.get("results") or [])[:count]:
    body = (item.get("text") or item.get("summary") or "").strip()
    # Collapse whitespace and cap the snippet so output stays compact.
    body = " ".join(body.split())[:500]
    results.append({
        "title": item.get("title") or "",
        "href": item.get("url") or "",
        "body": body,
    })

print(json.dumps(results, ensure_ascii=False))
PYEOF
