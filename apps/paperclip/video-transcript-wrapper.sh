#!/bin/sh
# Pull a transcript from a video URL (YouTube, Bilibili, Vimeo, and ~1800 other
# sites yt-dlp supports) for summarization/research.
#
# Strategy:
#   1. Captions — fetch human or auto-generated subtitles via yt-dlp (no API key,
#      covers the vast majority of YouTube videos), strip the VTT to clean,
#      de-duplicated plain text.
#   2. Whisper fallback (optional) — if there are NO captions AND GROQ_API_KEY is
#      set, download the audio and transcribe it with Groq Whisper. Needs ffmpeg.
#
# Usage:
#   video-transcript https://www.youtube.com/watch?v=...        # plain-text transcript
#   video-transcript --lang es https://...                       # prefer a subtitle language
#
# Env:
#   VIDEO_TRANSCRIPT_MAX_CHARS  cap on output length (default 100000)
#   VIDEO_TRANSCRIPT_LANG       default subtitle language (default "en")
#   GROQ_API_KEY                optional; enables the Whisper fallback for
#                               caption-less videos (requires ffmpeg)
#
# Exit codes: 0 ok · 2 usage/validation · 3 fetch/transcribe failed ·
#             4 no captions and no Whisper fallback available · 5 missing tool.
#
# Requires yt-dlp (and ffmpeg for the Whisper fallback) in the image.

set -eu

MAX_CHARS="${VIDEO_TRANSCRIPT_MAX_CHARS:-100000}"
LANG_PREF="${VIDEO_TRANSCRIPT_LANG:-en}"
# YouTube increasingly gates caption/format access behind a "PO token" for the
# default web client (worse from datacenter IPs), silently yielding no subtitles.
# The android client currently bypasses this. Passed only to the YouTube
# extractor; ignored for the other ~1800 sites. Overridable as YouTube churns.
YT_CLIENT="${VIDEO_TRANSCRIPT_YT_CLIENT:-android,web}"
URL=""

while [ $# -gt 0 ]; do
  case "$1" in
    --lang)
      [ $# -ge 2 ] || { echo "ERROR: --lang needs a value" >&2; exit 2; }
      LANG_PREF="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: video-transcript [--lang xx] <url>" >&2
      echo "  Extract a plain-text transcript from a video URL via yt-dlp captions" >&2
      echo "  (Whisper fallback for caption-less videos when GROQ_API_KEY is set)." >&2
      exit 0
      ;;
    -*)
      echo "ERROR: unknown option: $1" >&2
      echo "Usage: video-transcript [--lang xx] <url>" >&2
      exit 2
      ;;
    *)
      if [ -n "$URL" ]; then
        echo "ERROR: more than one URL given. Quote the URL." >&2
        exit 2
      fi
      URL="$1"
      shift
      ;;
  esac
done

if [ -z "$URL" ]; then
  echo "ERROR: no URL provided. Usage: video-transcript [--lang xx] <url>" >&2
  exit 2
fi

case "$URL" in
  http://*|https://*) ;;
  *)
    echo "ERROR: URL must start with http:// or https:// (got: $URL)" >&2
    exit 2
    ;;
esac

if ! command -v yt-dlp >/dev/null 2>&1; then
  echo "ERROR: yt-dlp is not installed in this environment." >&2
  exit 5
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT INT TERM

# ── Stage 1: captions (human subs first, then auto-subs) ─────────────────────
# `|| true`: a video with no subs makes yt-dlp exit non-zero; that's not fatal —
# we detect "no .vtt produced" below and decide on the fallback.
yt-dlp --quiet --no-warnings --skip-download \
  --write-subs --write-auto-subs \
  --sub-langs "${LANG_PREF}.*,${LANG_PREF},en" --sub-format vtt \
  --extractor-args "youtube:player_client=${YT_CLIENT}" \
  --output "${workdir}/sub.%(ext)s" "$URL" >/dev/null 2>&1 || true

vtt="$(find "$workdir" -name '*.vtt' 2>/dev/null | head -n1)"

if [ -n "$vtt" ]; then
  python3 - "$vtt" "$MAX_CHARS" <<'PYEOF'
import re, sys
vtt_path = sys.argv[1]
max_chars = int(sys.argv[2]) if len(sys.argv) > 2 else 100000
out, last = [], None
with open(vtt_path, encoding="utf-8", errors="ignore") as f:
    for raw in f:
        line = raw.rstrip("\n")
        s = line.strip()
        if not s:
            continue
        if s.startswith("WEBVTT") or s.startswith("Kind:") or s.startswith("Language:"):
            continue
        if "-->" in s:
            continue
        if re.fullmatch(r"\d+", s):        # bare cue index
            continue
        line = re.sub(r"<[^>]+>", "", line)  # inline timing/markup tags
        line = line.replace("&nbsp;", " ")
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line == last:        # drop rolling duplicates
            continue
        out.append(line)
        last = line
text = "\n".join(out)
if len(text) > max_chars:
    text = text[:max_chars].rstrip() + "\n...[transcript truncated]"
print(text)
PYEOF
  exit 0
fi

# ── Stage 2: Whisper fallback (optional, gated on GROQ_API_KEY) ───────────────
if [ -n "${GROQ_API_KEY:-}" ]; then
  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ERROR: no captions for ${URL}; the Whisper fallback needs ffmpeg, which is not installed." >&2
    exit 5
  fi
  yt-dlp --quiet --no-warnings -x --audio-format mp3 --audio-quality 5 \
    --extractor-args "youtube:player_client=${YT_CLIENT}" \
    --output "${workdir}/audio.%(ext)s" "$URL" >/dev/null 2>&1 \
    || { echo "ERROR: no captions and audio download failed for ${URL}." >&2; exit 3; }
  audio="$(find "$workdir" -name 'audio.*' 2>/dev/null | head -n1)"
  if [ -z "$audio" ]; then
    echo "ERROR: no captions and no audio could be extracted for ${URL}." >&2
    exit 3
  fi
  resp_file="$(mktemp)"
  trap 'rm -rf "$workdir" "$resp_file"' EXIT INT TERM
  http_code="$(curl --silent --show-error --max-time 180 \
    --output "$resp_file" --write-out '%{http_code}' \
    --header "Authorization: Bearer ${GROQ_API_KEY}" \
    --form "model=whisper-large-v3" \
    --form "response_format=text" \
    --form "file=@${audio}" \
    https://api.groq.com/openai/v1/audio/transcriptions || true)"
  case "$http_code" in
    2??)
      python3 - "$resp_file" "$MAX_CHARS" <<'PYEOF'
import sys
text = open(sys.argv[1], encoding="utf-8", errors="ignore").read().strip()
max_chars = int(sys.argv[2]) if len(sys.argv) > 2 else 100000
if len(text) > max_chars:
    text = text[:max_chars].rstrip() + "\n...[transcript truncated]"
print(text)
PYEOF
      exit 0
      ;;
    *)
      echo "ERROR: Whisper transcription failed for ${URL} (Groq HTTP ${http_code:-000})." >&2
      head -c 300 "$resp_file" 1>&2 || true
      echo "" >&2
      exit 3
      ;;
  esac
fi

echo "ERROR: no captions available for ${URL}. Set GROQ_API_KEY to enable the Whisper fallback for caption-less videos." >&2
exit 4
