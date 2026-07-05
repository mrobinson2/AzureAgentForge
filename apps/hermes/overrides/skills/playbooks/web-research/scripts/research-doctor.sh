#!/bin/sh
# research-doctor — probe each research backend wrapper and report its health.
#
# Borrows Agent-Reach's "doctor" idea: one command that tells you which research
# backends are actually working right now (search, page-read, video-transcript),
# so an agent or operator can route around a dead one instead of misreading empty
# results as "nothing found".
#
# Usage:
#   research-doctor            # human-readable per-backend status
#   research-doctor --json     # [{name, ok, detail, status}] for machine parsing
#
# A backend with no configured key is reported "skip" (not in use here), not
# "down". Exit codes: 0 = no backend is DOWN (some may be skipped) · 1 = at least
# one configured backend is DOWN.

set -u

JSON=0
[ "${1:-}" = "--json" ] && JSON=1

# Accumulate "name<TAB>status<TAB>detail" rows (status: ok | down | skip).
ROWS=""
row() { ROWS="${ROWS}${1}	${2}	${3}
"; }

# ── web-read (Jina Reader) — anonymous, always probeable ─────────────────────
if command -v web-read >/dev/null 2>&1; then
  if out="$(web-read https://example.com 2>&1)" && printf '%s' "$out" | grep -qi "example domain"; then
    row "web-read" "ok" ""
  else
    row "web-read" "down" "probe of example.com failed: $(printf '%s' "$out" | head -n1)"
  fi
else
  row "web-read" "down" "web-read not on PATH"
fi

# ── exa-search — only when EXA_API_KEY is configured ─────────────────────────
if [ -z "${EXA_API_KEY:-}" ]; then
  row "exa-search" "skip" "EXA_API_KEY not set"
elif ! command -v exa-search >/dev/null 2>&1; then
  row "exa-search" "down" "exa-search not on PATH"
elif exa-search "health check" >/dev/null 2>&1; then
  row "exa-search" "ok" ""
else
  row "exa-search" "down" "exa-search returned non-zero (key/quota/upstream?)"
fi

# ── brave-search — only when BRAVE_SEARCH_API_KEY is configured ──────────────
if [ -z "${BRAVE_SEARCH_API_KEY:-}" ]; then
  row "brave-search" "skip" "BRAVE_SEARCH_API_KEY not set"
elif ! command -v brave-search >/dev/null 2>&1; then
  row "brave-search" "down" "brave-search not on PATH"
elif brave-search "health" >/dev/null 2>&1; then
  row "brave-search" "ok" ""
else
  row "brave-search" "down" "brave-search returned non-zero (key/quota/upstream?)"
fi

# ── video-transcript — backend is "up" if yt-dlp is installed and runnable ───
# (A real transcript fetch is slow/flaky and YouTube-dependent; presence of a
# working yt-dlp is the meaningful liveness signal. video-transcript also runs
# its own per-call extraction.)
if ! command -v video-transcript >/dev/null 2>&1; then
  row "video-transcript" "down" "video-transcript not on PATH"
elif command -v yt-dlp >/dev/null 2>&1 && ytver="$(yt-dlp --version 2>/dev/null)"; then
  row "video-transcript" "ok" "yt-dlp ${ytver}"
else
  row "video-transcript" "down" "yt-dlp not installed or not runnable"
fi

# ── emit ─────────────────────────────────────────────────────────────────────
down=0
if [ "$JSON" -eq 1 ]; then
  # python3 is in the paperclip image; use it for safe JSON encoding.
  printf '%s' "$ROWS" | python3 -c '
import json, sys
out = []
for line in sys.stdin.read().splitlines():
    if not line.strip():
        continue
    name, status, detail = (line.split("\t") + ["", "", ""])[:3]
    out.append({"name": name, "ok": status == "ok", "status": status, "detail": detail})
print(json.dumps(out))
'
else
  printf '%s' "$ROWS" | while IFS='	' read -r name status detail; do
    [ -z "$name" ] && continue
    case "$status" in
      ok)   printf '  OK    %-18s %s\n' "$name" "$detail" ;;
      skip) printf '  SKIP  %-18s %s\n' "$name" "$detail" ;;
      *)    printf '  DOWN  %-18s %s\n' "$name" "$detail" ;;
    esac
  done
fi

# Exit nonzero if any backend is DOWN (skip does not count).
printf '%s' "$ROWS" | grep -q '	down	' && down=1
exit "$down"
