---
name: video-transcript
description: >
  Pull a plain-text transcript from a video URL (YouTube, Vimeo, Bilibili, and
  ~1800 other sites) so you can read, summarize, quote, or research its content
  without watching it. Use whenever a source or request is a video link.
  Trigger phrases: "summarize this video", "what does this talk/video say",
  "transcript of <url>", "get the captions", or any task pointing at a youtube.com
  / youtu.be / vimeo / video URL. The `video-transcript` CLI is on `$PATH`.
---

# Get a video's transcript with `video-transcript`

`video-transcript` is a CLI on `$PATH` that extracts a clean, plain-text
transcript from a video URL using [yt-dlp](https://github.com/yt-dlp/yt-dlp).
It fetches subtitles/captions (human or auto-generated), strips the timing markup,
and de-duplicates the rolling caption lines into readable prose.

## Usage

```bash
# Print the transcript — capture it so you can grep/quote/summarize it:
video-transcript "https://www.youtube.com/watch?v=VIDEO_ID" > /tmp/page.md
head -c 4000 /tmp/page.md

# Prefer a subtitle language (falls back to English):
video-transcript --lang es "https://www.youtube.com/watch?v=VIDEO_ID"
```

Only `http(s)` URLs are accepted. Output (and errors) follow the same convention
as `web-read`: transcript on stdout, errors on stderr.

## How it gets the text

1. **Captions** (default, no key) — human subs first, then auto-generated. Covers
   most YouTube videos.
2. **Whisper fallback** (optional) — if a video has **no** captions *and*
   `GROQ_API_KEY` is set, it downloads the audio and transcribes it with Groq
   Whisper (needs `ffmpeg`). Without the key, a caption-less video returns exit `4`.

## Behavior and exit codes

| Exit | Meaning |
|---|---|
| `0` | Success — transcript on stdout. |
| `2` | Usage / validation error (no URL, bad scheme, bad flag). |
| `3` | Fetch or transcription failed (clear error on stderr). |
| `4` | No captions and no Whisper fallback (`GROQ_API_KEY` unset). Try a different source. |
| `5` | A required tool (`yt-dlp`, or `ffmpeg` for the fallback) is missing. |

## Environment

| Variable | Purpose |
|---|---|
| `VIDEO_TRANSCRIPT_MAX_CHARS` | Cap on transcript length (default `100000`; long videos are truncated with a marker). |
| `VIDEO_TRANSCRIPT_LANG` | Default subtitle language (default `en`). |
| `VIDEO_TRANSCRIPT_YT_CLIENT` | yt-dlp YouTube `player_client` (default `android,web`). YouTube gates captions behind a PO token for the web client from datacenter IPs; the android client bypasses it. Override if YouTube extraction breaks. |
| `GROQ_API_KEY` | Optional. Enables the Whisper fallback for caption-less videos. |

## Caveat

YouTube extraction is a moving target (PO tokens, SABR streaming, client churn).
If transcripts suddenly stop working, it's usually YouTube-side — the pinned
yt-dlp may need bumping and `VIDEO_TRANSCRIPT_YT_CLIENT` may need adjusting.
For private/internal videos, don't rely on this — it goes through public extractors.
