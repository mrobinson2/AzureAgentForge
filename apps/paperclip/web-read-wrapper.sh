#!/bin/sh
# Smart wrapper around Jina Reader (https://r.jina.ai) for clean Markdown
# extraction of any web page.
#
# Why: an agent that `curl`s a page gets raw HTML — tags, scripts, nav chrome —
# which buries the actual content and wastes context. Jina Reader fetches the
# URL server-side and returns clean, readable Markdown (the content a human
# would read). This is the "for page content, use web-read, not raw curl" path.
#
# Usage forms:
#   web-read https://example.com/article          # clean Markdown to stdout
#   web-read --json https://example.com/article    # JSON {title, url, content, ...}
#
# Anonymous by default (no key needed). Set JINA_API_KEY in the container env for
# a higher rate limit — the wrapper sends it as a Bearer token; without it the
# request is still served anonymously.
#
# Output: page content on stdout; all errors on stderr so a caller capturing
# stdout (e.g. `web-read "$url" > /tmp/page.md`) gets clean content or nothing.
#
# Exit codes: 0 ok · 2 usage/validation error · 3 fetch failed (non-2xx or timeout).
#
# Privacy note: the target URL transits r.jina.ai (a third-party reader). Fine
# for public research; flag before pointing it at regulated/private content.
#
# Requires curl (present in the paperclip base image). No API key required.

set -eu

TIMEOUT="${WEB_READ_TIMEOUT:-20}"
JSON=0
URL=""

while [ $# -gt 0 ]; do
  case "$1" in
    --json)
      JSON=1
      shift
      ;;
    -h|--help)
      echo "Usage: web-read [--json] <url>" >&2
      echo "  Clean Markdown extraction of a web page via Jina Reader." >&2
      echo "  --json   return structured JSON instead of Markdown." >&2
      echo "  Env: JINA_API_KEY (optional, higher rate limit); WEB_READ_TIMEOUT (default 20s)." >&2
      exit 0
      ;;
    -*)
      echo "ERROR: unknown option: $1" >&2
      echo "Usage: web-read [--json] <url>" >&2
      exit 2
      ;;
    *)
      if [ -n "$URL" ]; then
        echo "ERROR: more than one URL given. Quote the URL if it contains spaces." >&2
        echo "Usage: web-read [--json] <url>" >&2
        exit 2
      fi
      URL="$1"
      shift
      ;;
  esac
done

if [ -z "$URL" ]; then
  echo "ERROR: no URL provided. Usage: web-read [--json] <url>" >&2
  exit 2
fi

# Only http(s): Jina won't read file://, and we don't forward odd schemes.
case "$URL" in
  http://*|https://*) ;;
  *)
    echo "ERROR: URL must start with http:// or https:// (got: $URL)" >&2
    exit 2
    ;;
esac

# Build curl args. Anonymous unless JINA_API_KEY is set.
set -- \
  --silent --show-error --location \
  --max-time "$TIMEOUT" \
  --header "User-Agent: azureagentforge-web-read/1.0"
if [ "$JSON" -eq 1 ]; then
  set -- "$@" --header "Accept: application/json"
fi
if [ -n "${JINA_API_KEY:-}" ]; then
  set -- "$@" --header "Authorization: Bearer ${JINA_API_KEY}"
fi

# r.jina.ai takes the target URL verbatim as a path suffix:
#   https://r.jina.ai/https://example.com/article
body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT INT TERM

# `|| true` so a curl transport failure (e.g. timeout, exit 28) doesn't trip
# `set -e` before we can read the HTTP status and emit a clear message.
http_code="$(curl "$@" --output "$body_file" --write-out '%{http_code}' "https://r.jina.ai/${URL}" || true)"

case "$http_code" in
  2??)
    cat "$body_file"
    ;;
  000|"")
    echo "ERROR: web-read could not reach r.jina.ai for ${URL} (timed out after ${TIMEOUT}s or a network error)." >&2
    exit 3
    ;;
  *)
    echo "ERROR: web-read got HTTP ${http_code} reading ${URL} via r.jina.ai." >&2
    head -c 400 "$body_file" 1>&2 || true
    echo "" >&2
    exit 3
    ;;
esac
