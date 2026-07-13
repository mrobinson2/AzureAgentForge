# Slack Bridge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a flag-gated `slack-bridge` service that brings Slack to parity with the existing Discord / Telegram / Microsoft Teams surfaces — a Slack Events API messaging endpoint that turns inbound Slack messages into Orchestrator/PaperClip issues and replies via `chat.postMessage` — plus the Terraform to deploy it behind a `slack_enabled` variable (internal ingress, KV-stored bot token).

**Architecture:** A near-exact structural mirror of `services/teams-bridge/`. A stateless FastAPI app exposes `POST /slack/events`. Slack delivers inbound traffic as HTTP POSTs carrying a `type`: a `url_verification` handshake (echo the `challenge`) or an `event_callback` whose `event.type == "message"`. The reference's Bot Framework JWT validation is replaced by its Slack analog — **signing-secret HMAC-SHA256 verification** of `X-Slack-Signature` over the `v0:<timestamp>:<raw_body>` basestring, with a `X-Slack-Request-Timestamp` replay window. A `message` event becomes a camelCase PaperClip issue (`surface: slack`); the parse / payload helpers are pure and unit-tested offline; the PaperClip POST is injectable (`issue_poster`) so the endpoint is testable without a live API. The endpoint **never returns 5xx** to Slack — Slack retry-storms on non-2xx — so downstream failures are acked with a body flag. Disabled by default (`var.slack_enabled = false`); internal ingress by design; bot token mounted from Key Vault.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, httpx, `hmac`/`hashlib` (stdlib, for signing-secret verification — NO pyjwt; Slack uses HMAC, not JWT). Tests: `pytest` with `fastapi.testclient.TestClient` + `monkeypatch` (mirrors teams-bridge exactly). Terraform: `azurerm` Container App, UAI + AcrPull + KV Secrets User role assignments, internal ingress.

---

## File structure (mirrors `services/teams-bridge/` file-for-file)

```
services/slack-bridge/
├── main.py                 # FastAPI app: /health + /slack/events (challenge, HMAC verify, parse, file, reply)
├── Dockerfile              # python:3.12-slim, non-root appuser, uvicorn on :3978
├── requirements.txt        # fastapi, uvicorn[standard], httpx   (NO pyjwt — Slack uses HMAC)
├── requirements-dev.txt    # -r requirements.txt + pytest
├── README.md               # service docs (endpoints, env, security note)
└── tests/
    └── test_bridge.py      # pytest — pure helpers + endpoint contract + HMAC verification, offline

integrations/slack/
└── README.md               # end-to-end setup (Slack app, signing secret, bot token, enable, expose)

infrastructure/modules/container-apps/
├── slack_bridge.tf         # NEW — mirrors teams_bridge.tf (UAI, roles, container app, internal ingress)
└── variables.tf            # MODIFY — add slack_enabled, slack_bridge_image_tag, slack_orchestrator_agent_id
```

The Slack analog of teams-bridge's pieces:

| teams-bridge | slack-bridge |
|---|---|
| `POST /api/messages` (Bot Framework) | `POST /slack/events` (Slack Events API) |
| Bot Framework JWT (RS256, JWKS) | Signing-secret HMAC-SHA256 (`X-Slack-Signature`) |
| `parse_activity` (Bot Framework activity) | `parse_event` (Slack event envelope) |
| Adaptive Card reply | `chat.postMessage` reply |
| `url_verification` — n/a | `url_verification` challenge handshake (Slack-specific) |
| `surface: teams` | `surface: slack` |
| `TEAMS_APP_ID` / JWKS env | `SLACK_SIGNING_SECRET` / `SLACK_BOT_TOKEN` env |

---

### Task 1: Scaffold the service skeleton (FastAPI app, `/health`, deps, Dockerfile)

**Files:**
- Create: `services/slack-bridge/main.py`
- Create: `services/slack-bridge/requirements.txt`
- Create: `services/slack-bridge/requirements-dev.txt`
- Create: `services/slack-bridge/Dockerfile`
- Create: `services/slack-bridge/tests/test_bridge.py`

- [ ] **Step 1: Write the failing test** (`services/slack-bridge/tests/test_bridge.py`)

```python
"""Offline tests for the Slack bridge — pure helpers + the endpoint contract.

No network: the PaperClip POST is swapped via the injectable `issue_poster`,
and the Slack reply is swapped via `reply_poster`.
Run: pip install -r requirements-dev.txt && pytest
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(main.app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["surface"] == "slack"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/slack-bridge && python -m pytest -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'main'`.

- [ ] **Step 3: Write `requirements.txt`** (note: NO pyjwt — Slack uses HMAC, which is stdlib)

```
fastapi>=0.110
uvicorn[standard]>=0.29
httpx>=0.27
```

- [ ] **Step 4: Write `requirements-dev.txt`**

```
-r requirements.txt
pytest>=8.0
```

- [ ] **Step 5: Write the minimal `main.py`** (app + `/health` only; handlers land in Tasks 2–5)

```python
"""Slack bridge (AzureAgentForge) — Slack chat surface.

A minimal Slack Events API messaging endpoint that bridges Slack to the agent
platform, at parity with the Discord plugin, the Telegram gateway, and the
Teams bridge: an inbound Slack message becomes a PaperClip issue routed to the
Orchestrator, and the agent's reply returns to the channel via chat.postMessage.
Disabled by default; enable with the `slack_enabled` Terraform variable.

The parse / payload helpers are pure and unit-tested offline. The PaperClip POST
is injectable (`issue_poster`) and the Slack reply is injectable (`reply_poster`)
so the endpoint is testable without a live API, and the endpoint NEVER returns
5xx to Slack (that triggers an aggressive retry storm) — failures are acked with
a body flag.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

PAPERCLIP_API_URL = os.getenv("PAPERCLIP_API_URL", "http://paperclip:3000")
PAPERCLIP_COMPANY_ID = os.getenv("PAPERCLIP_COMPANY_ID", "")
PAPERCLIP_API_KEY = os.getenv("PAPERCLIP_API_KEY", "")
ORCHESTRATOR_AGENT_ID = os.getenv("ORCHESTRATOR_AGENT_ID", "")

app = FastAPI(title="slack-bridge", version="1.0.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "surface": "slack"}
```

- [ ] **Step 6: Write the `Dockerfile`** (mirrors teams-bridge — `python:3.12-slim`, non-root, uvicorn on :3978)

```dockerfile
# Slack bridge — Slack chat surface (Slack Events API messaging endpoint).
# Disabled by default; deployed only when the `slack_enabled` Terraform variable
# is true. Bridges inbound Slack messages to PaperClip issues and replies via
# chat.postMessage. Stateless FastAPI app behind ACA internal ingress.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

RUN useradd --uid 1001 --create-home appuser
COPY --chown=appuser:appuser main.py /app/main.py
USER appuser

# Same conventional messaging port as the Teams bridge for a uniform ingress shape.
EXPOSE 3978
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3978"]
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd services/slack-bridge && pip install -r requirements-dev.txt && python -m pytest -q`
Expected: PASS (1 test).

- [ ] **Step 8: Commit**

```bash
git add services/slack-bridge/
git commit -m "feat(slack-bridge): scaffold FastAPI service skeleton + /health"
```

---

### Task 2: `parse_event` — pull the routable bits from a Slack event envelope

**Files:**
- Modify: `services/slack-bridge/main.py`
- Modify: `services/slack-bridge/tests/test_bridge.py`

Slack `event_callback` envelopes wrap the inner event in `event`. We route only
`message` events with non-empty text, and we **drop bot/self messages** (any
envelope carrying `bot_id`, or `subtype` set — `bot_message`, `message_changed`,
etc.) so the bridge never loops on its own `chat.postMessage` replies.

- [ ] **Step 1: Write the failing test**

```python
# ── parse_event ──────────────────────────────────────────────────────────────

def test_parse_event_extracts_fields():
    body = {
        "type": "event_callback",
        "team_id": "T123",
        "event": {
            "type": "message",
            "text": "  deploy the staging stack  ",
            "user": "U99",
            "channel": "C42",
            "ts": "1700000000.000100",
        },
    }
    parsed = main.parse_event(body)
    assert parsed == {
        "text": "deploy the staging stack",
        "user": "U99",
        "channel": "C42",
        "ts": "1700000000.000100",
        "team_id": "T123",
    }


def test_parse_event_ignores_non_message_and_empty():
    assert main.parse_event({"type": "event_callback", "event": {"type": "reaction_added"}}) is None
    assert main.parse_event({"type": "event_callback", "event": {"type": "message", "text": "   "}}) is None
    assert main.parse_event({"type": "url_verification", "challenge": "x"}) is None
    assert main.parse_event("not a dict") is None
    assert main.parse_event({}) is None


def test_parse_event_drops_bot_and_subtyped_messages():
    # Never re-ingest our own chat.postMessage replies, edits, or joins.
    assert main.parse_event({"type": "event_callback",
                             "event": {"type": "message", "text": "hi", "bot_id": "B1"}}) is None
    assert main.parse_event({"type": "event_callback",
                             "event": {"type": "message", "text": "hi", "subtype": "message_changed"}}) is None


def test_parse_event_falls_back_to_default_user():
    p = main.parse_event({"type": "event_callback",
                          "event": {"type": "message", "text": "hi", "channel": "C1", "ts": "1.1"}})
    assert p["user"] == "slack-user"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/slack-bridge && python -m pytest -q -k parse_event`
Expected: FAIL — `AttributeError: module 'main' has no attribute 'parse_event'`.

- [ ] **Step 3: Write the implementation** (add to `main.py`)

```python
def parse_event(body: Any) -> Optional[dict]:
    """Pull the routable bits from a Slack Events API envelope. Returns None for
    anything that isn't a non-empty user `message` (reactions, joins, the
    url_verification handshake, …), and for bot/self or subtyped messages, so
    those are silently acked and ignored (and we never loop on our own replies)."""
    if not isinstance(body, dict) or body.get("type") != "event_callback":
        return None
    event = body.get("event") or {}
    if event.get("type") != "message":
        return None
    # Drop our own bot replies and edits/joins/etc. to avoid feedback loops.
    if event.get("bot_id") or event.get("subtype"):
        return None
    text = (event.get("text") or "").strip()
    if not text:
        return None
    return {
        "text": text,
        "user": event.get("user") or "slack-user",
        "channel": event.get("channel") or "",
        "ts": event.get("ts") or "",
        "team_id": body.get("team_id") or "",
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/slack-bridge && python -m pytest -q -k parse_event`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add services/slack-bridge/main.py services/slack-bridge/tests/test_bridge.py
git commit -m "feat(slack-bridge): parse_event extracts routable message fields, drops bot/self"
```

---

### Task 3: `build_issue_payload` — the camelCase PaperClip issue an inbound Slack message creates

**Files:**
- Modify: `services/slack-bridge/main.py`
- Modify: `services/slack-bridge/tests/test_bridge.py`

camelCase matters — the PaperClip API's validation drops snake_case fields (same
constraint as teams-bridge).

- [ ] **Step 1: Write the failing test**

```python
# ── build_issue_payload ──────────────────────────────────────────────────────

def test_build_issue_payload_is_camelcase_and_tagged():
    parsed = {"text": "x" * 200, "user": "U7", "channel": "C9", "ts": "1.2", "team_id": "T1"}
    p = main.build_issue_payload(parsed, "co-1", agent_id="agent-9")
    assert p["companyId"] == "co-1"            # camelCase, not company_id
    assert p["assigneeId"] == "agent-9"
    assert p["status"] == "todo"
    assert len(p["title"]) == 120              # truncated
    assert p["metadata"] == {"surface": "slack", "channel": "C9", "ts": "1.2"}
    assert "via Slack — U7" in p["description"]


def test_build_issue_payload_omits_assignee_when_unset():
    p = main.build_issue_payload(
        {"text": "t", "user": "u", "channel": "", "ts": "", "team_id": ""}, "co")
    assert "assigneeId" not in p
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/slack-bridge && python -m pytest -q -k build_issue_payload`
Expected: FAIL — no `build_issue_payload`.

- [ ] **Step 3: Write the implementation** (add to `main.py`)

```python
def build_issue_payload(parsed: dict, company_id: str, agent_id: str = "") -> dict:
    """The camelCase PaperClip issue an inbound Slack message creates (camelCase
    matters — the API's validation drops snake_case fields)."""
    payload: dict = {
        "title": parsed["text"][:120],
        "description": (
            f"{parsed['text']}\n\n"
            f"_via Slack — {parsed['user']} "
            f"(channel `{parsed['channel']}`)_"
        ),
        "status": "todo",
        "companyId": company_id,
        "metadata": {"surface": "slack", "channel": parsed["channel"], "ts": parsed["ts"]},
    }
    if agent_id:
        payload["assigneeId"] = agent_id
    return payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/slack-bridge && python -m pytest -q -k build_issue_payload`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add services/slack-bridge/main.py services/slack-bridge/tests/test_bridge.py
git commit -m "feat(slack-bridge): build_issue_payload (camelCase, surface=slack)"
```

---

### Task 4: Signing-secret HMAC verification (the Slack analog of Bot Framework JWT)

**Files:**
- Modify: `services/slack-bridge/main.py`
- Modify: `services/slack-bridge/tests/test_bridge.py`

Slack signs every request: `X-Slack-Signature = "v0=" + HMAC_SHA256(signing_secret,
"v0:" + X-Slack-Request-Timestamp + ":" + raw_body)`. We verify the signature
with a **constant-time compare** and reject stale timestamps (replay window,
default 300s). Enforced when `SLACK_SIGNING_SECRET` is set; with it unset the
endpoint is unauthenticated (local/dev only) and logs a warning.
`SLACK_AUTH_DISABLED=1` is an explicit local escape — mirrors teams-bridge's
`authenticate()` contract exactly, including the warning and the env escape.

- [ ] **Step 1: Write the failing test**

```python
# ── Slack signing-secret HMAC verification ───────────────────────────────────

import hashlib  # noqa: E402
import hmac  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"


def _sign(secret: str, ts: str, body: bytes) -> str:
    base = b"v0:" + ts.encode() + b":" + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return "v0=" + digest


def test_verify_signature_accepts_valid():
    ts = str(int(time.time()))
    body = b'{"type":"event_callback"}'
    sig = _sign(_SECRET, ts, body)
    assert main.verify_signature(_SECRET, ts, body, sig) is True


def test_verify_signature_rejects_tampered_body():
    ts = str(int(time.time()))
    sig = _sign(_SECRET, ts, b'{"type":"event_callback"}')
    assert main.verify_signature(_SECRET, ts, b'{"type":"evil"}', sig) is False


def test_verify_signature_rejects_wrong_secret():
    ts = str(int(time.time()))
    body = b"x"
    sig = _sign("the-real-secret", ts, body)
    assert main.verify_signature(_SECRET, ts, body, sig) is False


def test_verify_signature_rejects_stale_timestamp():
    ts = str(int(time.time()) - 1000)  # 1000s old, beyond the 300s window
    body = b"x"
    sig = _sign(_SECRET, ts, body)
    assert main.verify_signature(_SECRET, ts, body, sig) is False


def test_verify_signature_rejects_malformed_header():
    ts = str(int(time.time()))
    assert main.verify_signature(_SECRET, ts, b"x", "") is False
    assert main.verify_signature(_SECRET, ts, b"x", "garbage") is False


def test_authenticate_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(main, "SLACK_SIGNING_SECRET", "")
    monkeypatch.setattr(main, "SLACK_AUTH_DISABLED", False)
    main.authenticate("ts", b"body", "")  # warns, does not raise


def test_authenticate_raises_when_required_and_bad(monkeypatch):
    monkeypatch.setattr(main, "SLACK_SIGNING_SECRET", _SECRET)
    monkeypatch.setattr(main, "SLACK_AUTH_DISABLED", False)
    with pytest.raises(main.AuthError):
        main.authenticate(str(int(time.time())), b"body", "v0=deadbeef")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/slack-bridge && python -m pytest -q -k "signature or authenticate"`
Expected: FAIL — no `verify_signature` / `authenticate` / `AuthError`.

- [ ] **Step 3: Write the implementation** (add to `main.py`, near the top imports add `hashlib`, `hmac`, `time`)

```python
import hashlib
import hmac
import time

# ── Slack signing-secret verification ────────────────────────────────────────
# Inbound Slack traffic carries an `X-Slack-Signature` HMAC-SHA256 over
# "v0:<X-Slack-Request-Timestamp>:<raw_body>" keyed by the app's signing secret.
# Verify it before acting on the event: constant-time digest compare + a replay
# window on the timestamp. Enforced when SLACK_SIGNING_SECRET is set; with it
# unset the endpoint is unauthenticated (local/dev only) and logs a warning.
# SLACK_AUTH_DISABLED=1 is an explicit local escape.
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_AUTH_DISABLED = os.getenv("SLACK_AUTH_DISABLED", "").lower() in ("1", "true", "yes")
SLACK_REPLAY_WINDOW_SECONDS = int(os.getenv("SLACK_REPLAY_WINDOW_SECONDS", "300"))


class AuthError(Exception):
    """Raised when an inbound request fails Slack signing-secret verification."""


def verify_signature(secret: str, timestamp: str, raw_body: bytes, signature: str,
                     *, window: int = SLACK_REPLAY_WINDOW_SECONDS,
                     now: Optional[float] = None) -> bool:
    """Validate a Slack request signature. Pure (no network). Checks the HMAC-SHA256
    digest with a constant-time compare and rejects stale timestamps (replay
    window). Returns True only on a fully valid, fresh signature."""
    if not secret or not signature.startswith("v0="):
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(now if now is not None else time.time())
    if abs(current - ts) > window:
        return False
    base = b"v0:" + timestamp.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def authenticate(timestamp: str, raw_body: bytes, signature: str) -> None:
    """Enforce Slack signing-secret auth on an inbound request when configured.
    No-op (with a warning) when SLACK_SIGNING_SECRET is unset, or when
    SLACK_AUTH_DISABLED. Raises AuthError on a bad/stale signature."""
    if SLACK_AUTH_DISABLED:
        return
    if not SLACK_SIGNING_SECRET:
        print("[slack-bridge] WARNING: SLACK_SIGNING_SECRET unset — /slack/events is "
              "UNAUTHENTICATED. Set SLACK_SIGNING_SECRET (the Slack app's signing "
              "secret) before exposing this endpoint.")
        return
    if not verify_signature(SLACK_SIGNING_SECRET, timestamp, raw_body, signature):
        raise AuthError("invalid or stale Slack signature")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/slack-bridge && python -m pytest -q -k "signature or authenticate"`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add services/slack-bridge/main.py services/slack-bridge/tests/test_bridge.py
git commit -m "feat(slack-bridge): signing-secret HMAC verification + replay window"
```

---

### Task 5: The `/slack/events` endpoint — challenge handshake, verify, file, reply

**Files:**
- Modify: `services/slack-bridge/main.py`
- Modify: `services/slack-bridge/tests/test_bridge.py`

The endpoint must, in order: (1) answer the `url_verification` challenge **before**
auth (Slack sends it during app setup; it's signed, but answering the plaintext
challenge is the documented handshake) — actually we verify the signature first
since it's present on every request, then branch; (2) verify the HMAC; (3) parse;
(4) file the PaperClip issue via the injectable `issue_poster`; (5) reply via the
injectable `reply_poster` (`chat.postMessage`); (6) **never 5xx** — Slack
retry-storms on non-2xx, so downstream failures ack `200 {"queued": false}`.

- [ ] **Step 1: Write the failing test**

```python
# ── reply (chat.postMessage) ─────────────────────────────────────────────────

def test_build_reply_payload():
    p = main.build_reply_payload("C42", "done ✅")
    assert p == {"channel": "C42", "text": "done ✅"}


# ── endpoint contract ────────────────────────────────────────────────────────

def _post(body: bytes, *, signed=True, ts=None):
    import json as _json
    ts = ts or str(int(time.time()))
    headers = {"Content-Type": "application/json"}
    if signed:
        headers["X-Slack-Request-Timestamp"] = ts
        headers["X-Slack-Signature"] = _sign(_SECRET, ts, body)
    return client.post("/slack/events", content=body, headers=headers)


def test_url_verification_echoes_challenge(monkeypatch):
    monkeypatch.setattr(main, "SLACK_SIGNING_SECRET", _SECRET)
    monkeypatch.setattr(main, "SLACK_AUTH_DISABLED", False)
    body = b'{"type":"url_verification","challenge":"abc123"}'
    r = _post(body)
    assert r.status_code == 200 and r.json() == {"challenge": "abc123"}


def test_message_creates_issue_and_replies(monkeypatch):
    monkeypatch.setattr(main, "SLACK_SIGNING_SECRET", _SECRET)
    monkeypatch.setattr(main, "SLACK_AUTH_DISABLED", False)
    seen, replied = {}, {}
    monkeypatch.setattr(main, "issue_poster", lambda payload: seen.update(payload) or 201)
    monkeypatch.setattr(main, "reply_poster", lambda channel, text: replied.update({"channel": channel, "text": text}) or 200)
    body = b'{"type":"event_callback","team_id":"T1","event":{"type":"message","text":"run the audit","user":"U7","channel":"C42","ts":"1.1"}}'
    r = _post(body)
    assert r.status_code == 200 and r.json()["queued"] is True
    assert seen["title"] == "run the audit"
    assert replied["channel"] == "C42"


def test_non_message_event_is_ignored_with_200(monkeypatch):
    monkeypatch.setattr(main, "SLACK_SIGNING_SECRET", _SECRET)
    monkeypatch.setattr(main, "SLACK_AUTH_DISABLED", False)
    monkeypatch.setattr(main, "issue_poster", lambda p: (_ for _ in ()).throw(AssertionError("should not post")))
    body = b'{"type":"event_callback","event":{"type":"reaction_added"}}'
    r = _post(body)
    assert r.status_code == 200 and r.json()["ignored"] is True


def test_bad_signature_returns_401(monkeypatch):
    monkeypatch.setattr(main, "SLACK_SIGNING_SECRET", _SECRET)
    monkeypatch.setattr(main, "SLACK_AUTH_DISABLED", False)
    body = b'{"type":"event_callback","event":{"type":"message","text":"hi","channel":"C","ts":"1"}}'
    ts = str(int(time.time()))
    r = client.post("/slack/events", content=body, headers={
        "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": "v0=deadbeef"})
    assert r.status_code == 401


def test_poster_failure_never_5xxes_slack(monkeypatch):
    monkeypatch.setattr(main, "SLACK_SIGNING_SECRET", _SECRET)
    monkeypatch.setattr(main, "SLACK_AUTH_DISABLED", False)
    def boom(_):
        raise RuntimeError("paperclip down")
    monkeypatch.setattr(main, "issue_poster", boom)
    monkeypatch.setattr(main, "reply_poster", lambda c, t: 200)
    body = b'{"type":"event_callback","event":{"type":"message","text":"hi","channel":"C","ts":"1"}}'
    r = _post(body)
    assert r.status_code == 200 and r.json()["queued"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/slack-bridge && python -m pytest -q`
Expected: FAIL — no `build_reply_payload` / `reply_poster` / `/slack/events`.

- [ ] **Step 3: Write the implementation** (add to `main.py`)

```python
def build_reply_payload(channel: str, text: str) -> dict:
    """The chat.postMessage body the bridge posts back to the Slack channel."""
    return {"channel": channel, "text": text}


def _post_issue(payload: dict) -> int:
    """Default issue poster — POST to the PaperClip API. Returns the status code."""
    headers = {"Content-Type": "application/json"}
    if PAPERCLIP_API_KEY:
        headers["Authorization"] = f"Bearer {PAPERCLIP_API_KEY}"
    resp = httpx.post(
        f"{PAPERCLIP_API_URL}/api/issues", json=payload, headers=headers, timeout=10.0
    )
    return resp.status_code


def _post_reply(channel: str, text: str) -> int:
    """Default Slack reply — POST chat.postMessage with the bot token. Status code."""
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    }
    resp = httpx.post(
        f"{SLACK_API_URL}/chat.postMessage",
        json=build_reply_payload(channel, text), headers=headers, timeout=10.0,
    )
    return resp.status_code


# Injectable for tests; production uses the real posters.
issue_poster: Callable[[dict], int] = _post_issue
reply_poster: Callable[[str, str], int] = _post_reply


@app.post("/slack/events")
async def slack_events(request: Request) -> Any:
    raw = await request.body()
    timestamp = request.headers.get("x-slack-request-timestamp", "")
    signature = request.headers.get("x-slack-signature", "")
    # Verify the signing-secret HMAC first (401, not 5xx — Slack retry-storms on 5xx).
    try:
        authenticate(timestamp, raw, signature)
    except AuthError:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    # Slack app-setup handshake: echo the plaintext challenge.
    if isinstance(body, dict) and body.get("type") == "url_verification":
        return JSONResponse({"challenge": body.get("challenge", "")}, status_code=200)
    parsed = parse_event(body)
    if parsed is None:
        return JSONResponse({"ignored": True}, status_code=200)
    payload = build_issue_payload(parsed, PAPERCLIP_COMPANY_ID, ORCHESTRATOR_AGENT_ID)
    try:
        code = issue_poster(payload)
    except Exception:  # noqa: BLE001 — never 5xx to Slack; it retry-storms
        return JSONResponse({"queued": False, "error": "bridge_post_failed"}, status_code=200)
    queued = 200 <= code < 300
    # Best-effort ack back into the channel; a reply failure must not 5xx either.
    try:
        reply_poster(parsed["channel"], "Got it — filed for the Orchestrator. 🛠️" if queued
                     else "Couldn't file that just now; please retry.")
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse({"queued": queued, "issueStatus": code}, status_code=200)
```

Also add the two Slack config constants near the other env reads at the top of `main.py`:

```python
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_API_URL = os.getenv("SLACK_API_URL", "https://slack.com/api")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/slack-bridge && python -m pytest -q`
Expected: PASS (full suite: 1 health + 4 parse + 2 payload + 7 auth + 1 reply + 5 endpoint = 20 tests).

- [ ] **Step 5: Commit**

```bash
git add services/slack-bridge/main.py services/slack-bridge/tests/test_bridge.py
git commit -m "feat(slack-bridge): /slack/events — challenge, HMAC, file issue, reply"
```

---

### Task 6: Service README + integration docs

**Files:**
- Create: `services/slack-bridge/README.md`
- Create: `integrations/slack/README.md`

- [ ] **Step 1: Write `services/slack-bridge/README.md`**

````markdown
# Slack bridge

A small FastAPI service that bridges **Slack** to the agent platform, at parity
with the [Discord plugin](../../integrations/discord/), the
[Telegram gateway](../../integrations/telegram/), and the
[Teams bridge](../teams-bridge/): an inbound Slack message becomes a PaperClip
issue routed to the Orchestrator, and the agent's reply returns to the channel
via `chat.postMessage`.

Disabled by default. Enable with the `slack_enabled` Terraform variable. See
[`integrations/slack/`](../../integrations/slack/) for the end-to-end setup.

## Endpoints

| Method | Path             | Purpose                                                                 |
|--------|------------------|-------------------------------------------------------------------------|
| `GET`  | `/health`        | Liveness.                                                               |
| `POST` | `/slack/events`  | Slack Events API endpoint. Answers the `url_verification` challenge; a `message` event becomes a PaperClip issue; non-`message`/bot/subtyped events are acked and ignored. |

The endpoint **never returns 5xx** to Slack (that triggers an aggressive retry
storm) — a downstream failure is acked with `{"queued": false}`.

## Configuration (env)

| Variable | Purpose |
|---|---|
| `PAPERCLIP_API_URL` | PaperClip base URL (default `http://paperclip:3000`). |
| `PAPERCLIP_COMPANY_ID` | Company the inbound issue is filed under. |
| `PAPERCLIP_API_KEY` | Bearer token for the PaperClip API (mounted from Key Vault). |
| `ORCHESTRATOR_AGENT_ID` | Optional — route Slack messages straight to one agent. |
| `SLACK_SIGNING_SECRET` | Slack app signing secret — HMAC-verifies inbound requests (mounted from Key Vault). |
| `SLACK_BOT_TOKEN` | Bot token (`xoxb-…`) for `chat.postMessage` replies (mounted from Key Vault). |
| `SLACK_REPLAY_WINDOW_SECONDS` | Replay window for the request timestamp (default `300`). |

## Security — read before enabling

The container's ingress is **internal** by design, so flipping `slack_enabled`
never publishes an unauthenticated event-ingest endpoint on its own. To take it
live you must:

1. **Expose `/slack/events`** to Slack through the platform's Cloudflare tunnel
   (the same path PaperClip uses for public ingress).
2. **Set `SLACK_SIGNING_SECRET`** so the bridge HMAC-verifies the
   `X-Slack-Signature` on every request. With it unset the endpoint logs a
   warning and trusts the body — local/dev only.

## Tests

```bash
pip install -r requirements-dev.txt
pytest            # 20 offline tests — pure helpers, HMAC verification, the endpoint contract, no network
```
````

- [ ] **Step 2: Write `integrations/slack/README.md`** (mirrors `integrations/teams/README.md` structure: Overview, Prerequisites, Setup steps, How it routes, Verify)

```markdown
# Slack Integration

## Overview

The Slack integration lets users talk to the agent platform from a Slack
channel. Messages flow from Slack → the **slack-bridge** service
([`services/slack-bridge`](../../services/slack-bridge/)) → a PaperClip issue →
the Orchestrator; the agent's reply returns to the channel via
`chat.postMessage`. It is at parity with the Discord, Telegram, and Teams
surfaces. **Disabled by default** — opt in via the `slack_enabled` Terraform
variable.

## Prerequisites

- A Slack workspace where you can create and install a Slack app.
- An Azure Key Vault provisioned by the platform.
- Azure CLI authenticated to the target subscription.

## Setup

### 1. Create a Slack app

1. At <https://api.slack.com/apps> create an app (from scratch).
2. **OAuth & Permissions** → add bot scopes `chat:write` and `channels:history`
   (and `groups:history` for private channels), then **Install to Workspace**
   and copy the **Bot User OAuth Token** (`xoxb-…`).
3. **Basic Information** → copy the **Signing Secret**.
4. **Event Subscriptions** → enable, set the **Request URL** to your public
   bridge URL `https://<your-public-host>/slack/events` (Slack sends the
   `url_verification` challenge here), and subscribe to the bot event
   `message.channels` (and `message.groups` for private channels).

### 2. Store the credentials in Key Vault

```bash
az keyvault secret set --vault-name <your-key-vault-name> \
  --name slack-bot-token --value "<xoxb-...>"
az keyvault secret set --vault-name <your-key-vault-name> \
  --name slack-signing-secret --value "<signing-secret>"
```

### 3. Enable the surface

```hcl
# dev.auto.tfvars (or your environment's tfvars)
slack_enabled               = true
slack_orchestrator_agent_id = ""   # optional — route Slack messages to one agent
```

```bash
terraform plan   # adds ca-slack-bridge-<env> (internal ingress)
terraform apply
```

### 4. Expose the events endpoint (required, not automatic)

The bridge ingress is **internal**. Route `/slack/events` to Slack through the
platform's Cloudflare tunnel (the same pattern PaperClip uses), and make sure
`SLACK_SIGNING_SECRET` is set so the bridge HMAC-verifies every request — see the
[service README security note](../../services/slack-bridge/README.md#security--read-before-enabling).
Enabling the variable alone never exposes an unauthenticated ingest endpoint.

## How it routes

| Slack event | Bridge behavior |
|---|---|
| `url_verification` | Echoes the `challenge` (app-setup handshake). |
| `message` (non-empty text, not bot/self/subtyped) | Files a PaperClip issue (`surface: slack`, the channel + ts in metadata) for the Orchestrator and acks into the channel. |
| reactions, joins, bot/self, subtyped, empty text | Acked with `200` and ignored. |
| Downstream PaperClip failure | Acked with `200 {"queued": false}` — never 5xx, which would make Slack retry-storm. |

## Verify

```bash
cd services/slack-bridge && pip install -r requirements-dev.txt && pytest
```
```

- [ ] **Step 3: Commit**

```bash
git add services/slack-bridge/README.md integrations/slack/README.md
git commit -m "docs(slack-bridge): service + integration README"
```

---

### Task 7: Terraform — `slack_bridge.tf` + the `slack_enabled` variable (default-off)

**Files:**
- Create: `infrastructure/modules/container-apps/slack_bridge.tf`
- Modify: `infrastructure/modules/container-apps/variables.tf`

Mirrors `teams_bridge.tf` exactly: a user-assigned identity, AcrPull +
KV-Secrets-User role assignments, an internal-ingress Container App, all
`count`-gated on `var.slack_enabled`. Adds the two Slack KV secrets
(`slack-signing-secret`, `slack-bot-token`) alongside the shared
`paperclip-automation-jwt-secret`.

- [ ] **Step 1: Add the variables** to `infrastructure/modules/container-apps/variables.tf` (place after the `teams_orchestrator_agent_id` block to keep the chat-surface vars together):

```hcl
variable "slack_enabled" {
  type        = bool
  description = "Enable the Slack chat surface (services/slack-bridge Slack Events API endpoint). Internal ingress — expose via the Cloudflare tunnel + set SLACK_SIGNING_SECRET before go-live."
  default     = false
}

variable "slack_bridge_image_tag" {
  type        = string
  description = "Image tag for the slack-bridge container."
  default     = "latest"
}

variable "slack_orchestrator_agent_id" {
  type        = string
  description = "Optional agent id to route inbound Slack messages to (the Orchestrator). Empty → PaperClip default routing."
  default     = ""
}
```

- [ ] **Step 2: Write `infrastructure/modules/container-apps/slack_bridge.tf`**

```hcl
# Slack bridge — Slack chat surface (services/slack-bridge).
#
# A Slack Events API messaging endpoint that turns inbound Slack messages into
# PaperClip issues for the Orchestrator and replies via chat.postMessage — at
# parity with the Discord plugin, the Telegram gateway, and the Teams bridge.
# Gated OFF by default (var.slack_enabled = false); when enabled it's a small
# stateless FastAPI app.
#
# SECURITY: ingress is INTERNAL by design. Expose /slack/events to Slack through
# the platform's Cloudflare tunnel (the same pattern PaperClip uses for public
# ingress), and set SLACK_SIGNING_SECRET so the bridge HMAC-verifies every
# request before going live (called out in services/slack-bridge/README.md).
# Keeping it internal means enabling the variable never exposes an
# unauthenticated event-ingest endpoint on its own.

resource "azurerm_user_assigned_identity" "slack_bridge" {
  count               = var.slack_enabled ? 1 : 0
  name                = "id-slack-bridge-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_role_assignment" "slack_bridge_acr_pull" {
  count                = var.slack_enabled ? 1 : 0
  scope                = var.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.slack_bridge[0].principal_id
}

resource "azurerm_role_assignment" "slack_bridge_kv_reader" {
  count                = var.slack_enabled ? 1 : 0
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.slack_bridge[0].principal_id
}

resource "azurerm_container_app" "slack_bridge" {
  count                        = var.slack_enabled ? 1 : 0
  name                         = "ca-slack-bridge-${var.environment}"
  container_app_environment_id = local.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.slack_bridge[0].id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.slack_bridge[0].id
  }

  # The bridge attaches this as the bearer token when creating PaperClip issues.
  secret {
    name                = "paperclip-automation-jwt-secret"
    key_vault_secret_id = "${var.key_vault_uri}secrets/paperclip-automation-jwt-secret"
    identity            = azurerm_user_assigned_identity.slack_bridge[0].id
  }

  # Slack app signing secret — HMAC-verifies inbound /slack/events requests.
  secret {
    name                = "slack-signing-secret"
    key_vault_secret_id = "${var.key_vault_uri}secrets/slack-signing-secret"
    identity            = azurerm_user_assigned_identity.slack_bridge[0].id
  }

  # Bot token (xoxb-…) for chat.postMessage replies.
  secret {
    name                = "slack-bot-token"
    key_vault_secret_id = "${var.key_vault_uri}secrets/slack-bot-token"
    identity            = azurerm_user_assigned_identity.slack_bridge[0].id
  }

  template {
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "slack-bridge"
      image  = "${var.container_registry_login_server}/slack-bridge:${var.slack_bridge_image_tag}"
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "PAPERCLIP_API_URL"
        value = "http://ca-paperclip-${var.environment}"
      }
      env {
        name  = "PAPERCLIP_COMPANY_ID"
        value = var.paperclip_company_id
      }
      env {
        name        = "PAPERCLIP_API_KEY"
        secret_name = "paperclip-automation-jwt-secret"
      }
      env {
        name        = "SLACK_SIGNING_SECRET"
        secret_name = "slack-signing-secret"
      }
      env {
        name        = "SLACK_BOT_TOKEN"
        secret_name = "slack-bot-token"
      }
      env {
        # Optional: route Slack messages straight to a specific agent (the
        # Orchestrator). Empty → PaperClip's default routing applies.
        name  = "ORCHESTRATOR_AGENT_ID"
        value = var.slack_orchestrator_agent_id
      }
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = var.app_insights_connection_string
      }
    }
  }

  ingress {
    external_enabled = false
    target_port      = 3978
    transport        = "http"

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  tags = var.tags

  depends_on = [
    azurerm_role_assignment.slack_bridge_acr_pull,
    azurerm_role_assignment.slack_bridge_kv_reader,
  ]
}
```

- [ ] **Step 3: Validate Terraform with the flag off (default)**

Run:
```bash
cd infrastructure/environments/dev && terraform init -backend=false && terraform validate
```
Expected: `Success! The configuration is valid.` — with `slack_enabled` defaulting to `false`, the four `slack_bridge.*` resources have `count = 0` and contribute nothing to the plan, so validation stays clean.

- [ ] **Step 4: Sanity-check the flag-on shape (optional, no apply)**

Run:
```bash
cd infrastructure/environments/dev && terraform validate
# then confirm the gate is wired (count expression present):
grep -n "var.slack_enabled" ../../modules/container-apps/slack_bridge.tf
```
Expected: `validate` clean; the `grep` shows `count = var.slack_enabled ? 1 : 0` on each of the four resources. Do NOT run `terraform plan -var slack_enabled=true` against the live backend.

- [ ] **Step 5: Commit**

```bash
git add infrastructure/modules/container-apps/slack_bridge.tf infrastructure/modules/container-apps/variables.tf
git commit -m "feat(slack-bridge): slack_enabled var + container app (internal ingress, default off)"
```

---

### Task 8: Full suite + final wiring check

**Files:** none (verification only)

- [ ] **Step 1: Run the full service test suite**

Run: `cd services/slack-bridge && pip install -r requirements-dev.txt && python -m pytest -q`
Expected: `20 passed`.

- [ ] **Step 2: Confirm the Dockerfile builds the dependency layer** (no network calls in tests; this just proves the image is buildable)

Run: `cd services/slack-bridge && docker build -t slack-bridge:plancheck .`
Expected: image builds; `main.py` copied as `appuser`; CMD is `uvicorn main:app … --port 3978`.

- [ ] **Step 3: Re-validate Terraform with the flag off**

Run: `cd infrastructure/environments/dev && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 4: Commit (if any doc tweaks were needed)** — otherwise nothing to commit.

---

## Self-Review

- **Spec coverage:**
  - New flag-gated `slack-bridge` service mirroring teams-bridge file-for-file — ✓ (`main.py`, `Dockerfile`, `requirements*.txt`, `README.md`, `tests/test_bridge.py`; Tasks 1–6).
  - Slack Events API inbound: `url_verification` challenge handshake — ✓ (Task 5, returns `{"challenge": …}`); `event_callback` / `event.type == "message"` → issue — ✓ (Tasks 2, 3, 5).
  - Replies via `chat.postMessage` — ✓ (`build_reply_payload` + `reply_poster`, Task 5).
  - Signature verification: `X-Slack-Signature` + `X-Slack-Request-Timestamp` HMAC-SHA256 over `v0:<ts>:<body>`, constant-time compare, replay window — ✓ (Task 4), wired as the real auth step in the endpoint — ✓ (Task 5). This is the deliberate Slack analog of teams-bridge's Bot Framework JWT (NO pyjwt; HMAC is stdlib).
  - Terraform `slack_bridge.tf` mirroring `teams_bridge.tf` + `slack_enabled` (default `false`), internal ingress, KV-stored bot token + signing secret — ✓ (Task 7).
  - Default-off + `terraform validate` clean with the flag off — ✓ (Task 7 Step 3, Task 8 Step 3): the four resources are `count = var.slack_enabled ? 1 : 0`.
- **Placeholder scan:** No `TODO`/`…`/"similar to teams-bridge"/"add validation" left in shipped artifacts — every handler, test body, Dockerfile, and HCL block is written out in full. The one operator-owned hardening step (exposing the endpoint through Cloudflare + ensuring `SLACK_SIGNING_SECRET` is set) is documented, not stubbed, and mirrors how teams-bridge documents its go-live step.
- **Type consistency:**
  - `parse_event(body) -> Optional[dict]` returns the fixed shape `{text, user, channel, ts, team_id}` consumed by `build_issue_payload` everywhere.
  - `issue_poster: Callable[[dict], int]` and `reply_poster: Callable[[str, str], int]` signatures match their default `_post_issue` / `_post_reply` and every `monkeypatch.setattr` in the tests.
  - `verify_signature(secret, timestamp, raw_body, signature, *, window, now) -> bool` and `authenticate(timestamp, raw_body, signature) -> None (raises AuthError)` are consistent across Task 4 and the endpoint in Task 5.
  - Test runner is `pytest` with `fastapi.testclient.TestClient` + `monkeypatch` — identical to `services/teams-bridge/tests/test_bridge.py`. Container port `3978`, `python:3.12-slim`, non-root `appuser` — identical to teams-bridge.
- **Deliberate divergences from teams-bridge (all intentional, all Slack-correct):**
  - `requirements.txt` drops `pyjwt[crypto]` — Slack auth is HMAC (stdlib `hmac`/`hashlib`), not JWT.
  - Endpoint path is `/slack/events` (not `/api/messages`) and adds the `url_verification` branch.
  - Reply is `chat.postMessage` JSON (not an Adaptive Card).
  - `parse_event` additionally drops `bot_id`/`subtype` messages to prevent the bridge looping on its own replies — no teams-bridge equivalent is needed because Bot Framework doesn't echo bot activities the same way.

**Risk:** The only externally-coupled behaviors (`_post_issue` PaperClip POST and `_post_reply` chat.postMessage) are dependency-injected and never exercised by the offline suite, exactly as in teams-bridge; their live correctness is verified by the operator during go-live (Task 6 integration README), not in CI. Everything testable — parsing, payload shaping, HMAC verification, challenge handshake, the never-5xx contract — is covered offline.
