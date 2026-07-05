"""Offline tests for the daily-digest webhook poster."""

from governor import digest_post


def test_build_payload_extracts_text():
    p = digest_post.build_payload({"text": "📋 Memory digest: 3 durable_fact"})
    assert p == {"content": "📋 Memory digest: 3 durable_fact"}


def test_build_payload_falls_back_when_text_missing():
    p = digest_post.build_payload({"writes_by_class": {}})
    assert "Memory digest" in p["content"]


def test_build_payload_truncates_overlong_text():
    p = digest_post.build_payload({"text": "x" * 5000})
    assert len(p["content"]) <= 1990
    assert p["content"].endswith("…")


def test_clamp_window():
    assert digest_post._clamp_window("48") == 48
    assert digest_post._clamp_window("0") == 1       # floor
    assert digest_post._clamp_window("9999") == 168  # ceiling
    assert digest_post._clamp_window("nonsense") == 24


def test_main_noops_without_webhook(monkeypatch, capsys):
    monkeypatch.delenv("DIGEST_WEBHOOK_URL", raising=False)
    assert digest_post.main() == 0
    assert "no-op" in capsys.readouterr().out


def test_main_happy_path(monkeypatch):
    monkeypatch.setenv("DIGEST_WEBHOOK_URL", "https://discord.test/webhook")
    monkeypatch.setattr(digest_post, "fetch_digest", lambda *a, **k: {"text": "hello digest"})
    posted = {}
    def fake_post(url, payload):
        posted["url"] = url
        posted["payload"] = payload
        return 204
    monkeypatch.setattr(digest_post, "post_to_discord", fake_post)
    assert digest_post.main() == 0
    assert posted["url"] == "https://discord.test/webhook"
    assert posted["payload"] == {"content": "hello digest"}


def test_main_returns_1_on_fetch_failure(monkeypatch):
    monkeypatch.setenv("DIGEST_WEBHOOK_URL", "https://discord.test/webhook")
    def boom(*a, **k):
        raise RuntimeError("governor unreachable")
    monkeypatch.setattr(digest_post, "fetch_digest", boom)
    assert digest_post.main() == 1
