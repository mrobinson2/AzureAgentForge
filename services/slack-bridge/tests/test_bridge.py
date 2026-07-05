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


# ── Slack signing-secret HMAC verification ───────────────────────────────────

import hashlib  # noqa: E402
import hmac  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

_SECRET = "test-slack-signing-secret-not-a-real-key"  # noqa: S105 — fixture, not a credential


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
