"""Daily memory digest -> Discord webhook.

Runs as a scheduled job off the memory-governor image (command
``python -m governor.digest_post``), mirroring the watchdog job. It fetches the
governor's own ``/digest`` and posts the rendered text to a Discord webhook —
the operator-curation flywheel's delivery step.

No-ops cleanly (exit 0) when ``DIGEST_WEBHOOK_URL`` is unset, mirroring the
platform's "feature gated by a secret's presence" convention, so the job is
safe to schedule before the webhook is provisioned.

Stdlib only (urllib/json) — no new image dependency.

Env:
  GOVERNOR_BASE_URL    in-mesh short name, e.g. http://memory-governor:8090
  GOVERNOR_API_KEY     shared X-Governor-Key the governor expects
  DIGEST_WEBHOOK_URL   Discord webhook URL (unset -> no-op exit 0)
  DIGEST_WINDOW_HOURS  digest window in hours (default 24, clamped 1..168)
"""

import json
import os
import sys
import urllib.request

# Discord caps a message at 2000 chars; leave headroom.
_MAX_CONTENT = 1990


def build_payload(digest: dict) -> dict:
    """Pure: turn a /digest response into a Discord webhook JSON body.

    Falls back to a sensible line if ``text`` is missing, and truncates to
    Discord's content limit so a pathologically long digest can't 400 the post.
    """
    text = (digest or {}).get("text") or "📋 Memory digest: (no summary available)"
    if len(text) > _MAX_CONTENT:
        text = text[: _MAX_CONTENT - 1] + "…"
    return {"content": text}


def _clamp_window(raw: str) -> int:
    try:
        return max(1, min(168, int(raw)))
    except (TypeError, ValueError):
        return 24


def fetch_digest(base_url: str, api_key: str, window_hours: int) -> dict:
    url = f"{base_url.rstrip('/')}/digest?window_hours={int(window_hours)}"
    req = urllib.request.Request(url, headers={"X-Governor-Key": api_key})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — fixed in-mesh URL
        return json.loads(resp.read().decode("utf-8"))


def post_to_discord(webhook_url: str, payload: dict) -> int:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
        return resp.status


def main() -> int:
    webhook = os.environ.get("DIGEST_WEBHOOK_URL", "").strip()
    if not webhook:
        print("digest_post: DIGEST_WEBHOOK_URL unset — no-op", flush=True)
        return 0

    base = os.environ.get("GOVERNOR_BASE_URL", "http://localhost:8090")
    api_key = os.environ.get("GOVERNOR_API_KEY", "")
    window = _clamp_window(os.environ.get("DIGEST_WINDOW_HOURS", "24"))

    try:
        digest = fetch_digest(base, api_key, window)
    except Exception as exc:  # noqa: BLE001 — log + non-zero exit on any fetch error
        print(f"digest_post: failed to fetch digest: {exc}", file=sys.stderr, flush=True)
        return 1

    payload = build_payload(digest)
    try:
        status = post_to_discord(webhook, payload)
    except Exception as exc:  # noqa: BLE001
        print(f"digest_post: failed to post to webhook: {exc}", file=sys.stderr, flush=True)
        return 1

    print(f"digest_post: posted digest to webhook (HTTP {status})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
