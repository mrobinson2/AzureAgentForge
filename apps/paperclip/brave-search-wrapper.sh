#!/bin/sh
# Smart wrapper around the Brave Search API.
#
# Replaces ddgs as the primary web-search tool because DuckDuckGo silently
# blocks Azure cloud IPs (returns empty results from inside ACA). Brave
# Search has a free tier (2,000 queries/month) and accepts cloud-IP traffic.
#
# Usage forms accepted (mirrors ddgs-wrapper.sh so AGENTS.md examples
# don't have to change beyond the binary name):
#   brave-search "query"                       # naive form
#   brave-search text -k "query" -m 5 -o json  # ddgs-compatible form
#
# Output: JSON array of {title, href, body} objects (ddgs-compatible shape)
# so downstream parsing in agent prompts continues to work.
#
# Requires BRAVE_SEARCH_API_KEY in the environment.

set -e

if [ -z "$BRAVE_SEARCH_API_KEY" ]; then
  echo "ERROR: BRAVE_SEARCH_API_KEY env var not set" >&2
  exit 1
fi

# Parse args. Default count = 5.
QUERY=""
COUNT=5

case "$1" in
  text|news|web|"")
    # ddgs-compatible form: brave-search text -k "query" -m 5 -o json
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
    echo "Usage: brave-search \"query\"  OR  brave-search text -k \"query\" -m 5 -o json" >&2
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

# URL-encode the query and call Brave. python3 is in the base image.
exec python3 - "$QUERY" "$COUNT" <<'PYEOF'
import json
import os
import sys
import urllib.parse
import urllib.request

query = sys.argv[1]
count = int(sys.argv[2])
api_key = os.environ["BRAVE_SEARCH_API_KEY"]

url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({
    "q": query,
    "count": count,
})

req = urllib.request.Request(url, headers={
    "X-Subscription-Token": api_key,
    "Accept": "application/json",
    "User-Agent": "azureagentforge-brave-search/1.0",
})

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
except urllib.error.HTTPError as e:
    print(f"ERROR: Brave API returned HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}", file=sys.stderr)
    sys.exit(3)
except Exception as e:
    print(f"ERROR: Brave API call failed: {e}", file=sys.stderr)
    sys.exit(4)

results = []
for item in (data.get("web") or {}).get("results", [])[:count]:
    results.append({
        "title": item.get("title", ""),
        "href": item.get("url", ""),
        "body": item.get("description", ""),
    })

print(json.dumps(results, ensure_ascii=False))
PYEOF
