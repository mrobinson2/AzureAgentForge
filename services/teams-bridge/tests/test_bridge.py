"""Offline tests for the Teams bridge — pure helpers + the endpoint contract.

No network: the PaperClip POST is swapped via the injectable `issue_poster`.
Run: pip install -r requirements-dev.txt && pytest
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(main.app)


# ── parse_activity ───────────────────────────────────────────────────────────

def test_parse_message_activity_extracts_fields():
    body = {
        "type": "message",
        "text": "  deploy the staging stack  ",
        "from": {"id": "29:abc", "name": "Ada"},
        "conversation": {"id": "19:meeting"},
        "serviceUrl": "https://smba.example/",
    }
    parsed = main.parse_activity(body)
    assert parsed == {
        "text": "deploy the staging stack",
        "user": "Ada",
        "conversation_id": "19:meeting",
        "service_url": "https://smba.example/",
    }


def test_parse_ignores_non_message_and_empty():
    assert main.parse_activity({"type": "typing"}) is None
    assert main.parse_activity({"type": "conversationUpdate"}) is None
    assert main.parse_activity({"type": "message", "text": "   "}) is None
    assert main.parse_activity("not a dict") is None
    assert main.parse_activity({}) is None


def test_parse_falls_back_to_id_then_default_for_user():
    assert main.parse_activity(
        {"type": "message", "text": "hi", "from": {"id": "29:x"}})["user"] == "29:x"
    assert main.parse_activity({"type": "message", "text": "hi"})["user"] == "teams-user"


# ── build_issue_payload ──────────────────────────────────────────────────────

def test_build_issue_payload_is_camelcase_and_tagged():
    parsed = {"text": "x" * 200, "user": "Ada", "conversation_id": "19:c"}
    p = main.build_issue_payload(parsed, "co-1", agent_id="agent-9")
    assert p["companyId"] == "co-1"           # camelCase, not company_id
    assert p["assigneeId"] == "agent-9"
    assert p["status"] == "todo"
    assert len(p["title"]) == 120             # truncated
    assert p["metadata"] == {"surface": "teams", "conversationId": "19:c"}
    assert "via Microsoft Teams — Ada" in p["description"]


def test_build_issue_payload_omits_assignee_when_unset():
    p = main.build_issue_payload({"text": "t", "user": "u", "conversation_id": ""}, "co")
    assert "assigneeId" not in p


# ── build_card ───────────────────────────────────────────────────────────────

def test_build_card_is_a_valid_adaptive_card_envelope():
    card = main.build_card("done ✅")
    att = card["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert att["content"]["type"] == "AdaptiveCard"
    assert att["content"]["body"][0]["text"] == "done ✅"


# ── endpoint contract ────────────────────────────────────────────────────────

def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["surface"] == "teams"


def test_message_creates_issue_and_acks(monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "issue_poster", lambda payload: seen.update(payload) or 201)
    r = client.post("/api/messages", json={
        "type": "message", "text": "run the audit", "from": {"name": "Ada"},
        "conversation": {"id": "19:c"}})
    assert r.status_code == 200 and r.json()["queued"] is True
    assert seen["title"] == "run the audit"


def test_non_message_is_ignored_with_200(monkeypatch):
    monkeypatch.setattr(main, "issue_poster", lambda p: (_ for _ in ()).throw(AssertionError("should not post")))
    r = client.post("/api/messages", json={"type": "typing"})
    assert r.status_code == 200 and r.json()["ignored"] is True


def test_poster_failure_never_5xxes_bot_framework(monkeypatch):
    def boom(_):
        raise RuntimeError("paperclip down")
    monkeypatch.setattr(main, "issue_poster", boom)
    r = client.post("/api/messages", json={
        "type": "message", "text": "hi", "conversation": {"id": "c"}})
    assert r.status_code == 200 and r.json()["queued"] is False


# ── Bot Framework JWT validation ──────────────────────────────────────────────

import time  # noqa: E402
import jwt as _jwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

_APP_ID = "11111111-2222-3333-4444-555555555555"


def _rsa_pem():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = k.private_bytes(serialization.Encoding.PEM,
                           serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.PEM,
                                       serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, pub


def _tok(priv, **over):
    claims = {"iss": main.BOT_FRAMEWORK_ISSUER, "aud": _APP_ID,
              "exp": int(time.time()) + 300, **over}
    return _jwt.encode(claims, priv, algorithm="RS256")


def test_verify_jwt_accepts_valid_token():
    priv, pub = _rsa_pem()
    assert main.verify_jwt(_tok(priv), _APP_ID, pub)["aud"] == _APP_ID


def test_verify_jwt_rejects_wrong_audience():
    priv, pub = _rsa_pem()
    with pytest.raises(main.AuthError):
        main.verify_jwt(_tok(priv, aud="someone-else"), _APP_ID, pub)


def test_verify_jwt_rejects_wrong_issuer():
    priv, pub = _rsa_pem()
    with pytest.raises(main.AuthError):
        main.verify_jwt(_tok(priv, iss="https://evil.example"), _APP_ID, pub)


def test_verify_jwt_rejects_expired():
    priv, pub = _rsa_pem()
    with pytest.raises(main.AuthError):  # 1000s past, well beyond the 300s leeway
        main.verify_jwt(_tok(priv, exp=int(time.time()) - 1000), _APP_ID, pub)


def test_verify_jwt_rejects_bad_signature():
    priv1, _ = _rsa_pem()
    _, pub2 = _rsa_pem()
    with pytest.raises(main.AuthError):
        main.verify_jwt(_tok(priv1), _APP_ID, pub2)


def test_bearer_token_extraction():
    assert main.bearer_token("Bearer abc.def") == "abc.def"
    assert main.bearer_token("") is None
    assert main.bearer_token("Basic xyz") is None


def test_authenticate_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(main, "TEAMS_APP_ID", "")
    monkeypatch.setattr(main, "TEAMS_AUTH_DISABLED", False)
    main.authenticate("")  # warns, does not raise


def test_messages_returns_401_when_auth_required_and_missing(monkeypatch):
    monkeypatch.setattr(main, "TEAMS_APP_ID", _APP_ID)
    monkeypatch.setattr(main, "TEAMS_AUTH_DISABLED", False)
    r = client.post("/api/messages", json={"type": "message", "text": "hi"})
    assert r.status_code == 401
