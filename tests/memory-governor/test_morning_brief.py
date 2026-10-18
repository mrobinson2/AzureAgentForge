"""Offline tests for the operator morning brief (governor.morning_brief).

Pure assembly functions are fed fixture JSON shaped like the live sources
(PaperClip runs, router /health + /debug/flight-recorder, governor /digest +
/escalation-sla). No network, no DB.
"""

from datetime import datetime, timezone

from governor import morning_brief as mb

NOW = datetime(2026, 10, 17, 13, 0, tzinfo=timezone.utc)

RUNS = [
    {"id": "r1", "agentName": "Coder", "status": "completed", "cost_usd": 0.42,
     "startedAt": "2026-10-17T01:00:00Z", "finishedAt": "2026-10-17T01:10:00Z", "issueId": "i1"},
    {"id": "r2", "agentName": "Coder", "status": "failed", "cost_usd": 0.10,
     "startedAt": "2026-10-17T02:00:00Z", "finishedAt": "2026-10-17T02:01:00Z", "issueId": "i2"},
    {"id": "r3", "agentName": "Researcher", "status": "running", "cost_usd": 1.5,
     "startedAt": "2026-10-17T12:50:00Z", "finishedAt": None, "issueId": "i3"},
    {"id": "r4", "agentName": "Researcher", "status": "completed", "cost_usd": 2.25,
     "startedAt": "2026-10-17T03:00:00Z", "finishedAt": "2026-10-17T03:45:00Z", "issueId": "i4"},
]

HEALTH = {
    "status": "ok",
    "date": "2026-10-17",
    "tiers": {"gpt4o-mini": {"over_budget": False}, "claude": {"over_budget": True}},
    "circuit_breakers": {"enabled": True, "by_tier": {"claude": "open", "gpt4o-mini": "closed"},
                         "open_keys": 1},
    "kill_switch": {"engaged": [], "scopes": {"paid_fallback": False, "all_paid": False}},
}

RECORDER = {
    "enabled": True,
    "stats": {"events": 3},
    "recent": [
        {"caller": "coder", "served_tier": "gpt4o-mini", "outcome": "success", "cost_usd": 0.01},
        {"caller": "coder", "served_tier": "gpt4o-mini", "outcome": "error", "cost_usd": 0.0,
         "breaker_verdicts": [{"breaker": "retry_storm"}]},
        {"caller": "researcher", "served_tier": "claude", "outcome": "success", "cost_usd": 0.30},
    ],
}

SLA = {"text": "Escalations: 2 open, 1 breached", "unacked": 2, "breached": 1}
DIGEST = {"text": "📋 Memory digest: 3 durable_fact, 1 needs_review"}


def test_summarize_runs_counts_per_agent_and_totals():
    s = mb.summarize_runs(RUNS)
    assert s["totals"] == {"completed": 2, "failed": 1, "running": 1}
    assert s["per_agent"]["Coder"] == {"completed": 1, "failed": 1, "running": 0}
    assert s["per_agent"]["Researcher"] == {"completed": 1, "failed": 0, "running": 1}
    assert s["spend_usd"] == 4.27
    assert s["costliest"][0]["id"] == "r4"
    assert s["slowest"][0]["id"] == "r4"


def test_summarize_runs_handles_empty_and_malformed():
    assert mb.summarize_runs([])["totals"] == {"completed": 0, "failed": 0, "running": 0}
    s = mb.summarize_runs([{"status": None}, {"agentName": "X", "cost_usd": "bad"}])
    assert s["totals"]["running"] == 2
    assert s["spend_usd"] == 0.0


def test_summarize_router_reads_health_and_recorder():
    s = mb.summarize_router(HEALTH, RECORDER)
    assert s["over_budget_tiers"] == ["claude"]
    assert s["open_breaker_tiers"] == ["claude"]
    assert s["kill_switch_engaged"] is False
    assert s["calls"] == 3
    assert s["errors"] == 1
    assert s["waste_breaker_hits"] == 1
    assert s["spend_by_caller"] == {"researcher": 0.3, "coder": 0.01}
    assert s["spend_by_tier"] == {"claude": 0.3, "gpt4o-mini": 0.01}


def test_summarize_router_tolerates_missing_recorder():
    s = mb.summarize_router(HEALTH, None)
    assert s["calls"] is None
    assert s["over_budget_tiers"] == ["claude"]


def test_render_includes_every_section_and_marks_unavailable():
    text = mb.render(
        window_hours=24,
        now=NOW,
        runs=mb.summarize_runs(RUNS),
        router=mb.summarize_router(HEALTH, RECORDER),
        approvals=SLA,
        memory=None,
        errors={"memory": "governor unreachable"},
    )
    assert "Morning brief" in text
    assert "Coder" in text and "Researcher" in text
    assert "claude" in text  # over budget + open breaker
    assert "2 open, 1 breached" in text
    assert "Memory" in text and "unavailable" in text
    assert len(text) <= mb.MAX_CHARS


def test_render_truncates_pathological_input():
    huge_runs = mb.summarize_runs(
        [{"id": f"r{i}", "agentName": f"Agent{i}", "status": "completed", "cost_usd": 1}
         for i in range(500)]
    )
    text = mb.render(window_hours=24, now=NOW, runs=huge_runs, router=None,
                     approvals=None, memory=None, errors={})
    assert len(text) <= mb.MAX_CHARS
    assert text.endswith("…")


def test_render_quiet_night_still_says_healthy():
    text = mb.render(window_hours=24, now=NOW, runs=mb.summarize_runs([]),
                     router=mb.summarize_router({"tiers": {}}, None),
                     approvals={"text": "", "unacked": 0}, memory=DIGEST, errors={})
    assert "0 completed" in text
    assert "no guardrail trips" in text.lower() or "no trips" in text.lower()


def test_build_payload_picks_slack_or_discord_shape():
    assert mb.build_payload("hi", "https://hooks.slack.com/services/x") == {"text": "hi"}
    assert mb.build_payload("hi", "https://discord.com/api/webhooks/x") == {"content": "hi"}
    assert mb.build_payload("hi", "https://example.test/hook") == {"content": "hi", "text": "hi"}


def test_clamp_window():
    assert mb._clamp_window("48") == 48
    assert mb._clamp_window("0") == 1
    assert mb._clamp_window("9999") == 168
    assert mb._clamp_window("nonsense") == 24


def test_main_prints_brief_and_exits_zero_without_webhook(monkeypatch, capsys):
    monkeypatch.delenv("BRIEF_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(mb, "collect", lambda: ({"runs": mb.summarize_runs(RUNS),
                                                "router": None, "approvals": None,
                                                "memory": None}, {"router": "down"}))
    assert mb.main() == 0
    out = capsys.readouterr().out
    assert "Morning brief" in out
    assert "no webhook" in out


def test_main_posts_when_webhook_configured(monkeypatch):
    monkeypatch.setenv("BRIEF_WEBHOOK_URL", "https://hooks.slack.com/services/x")
    monkeypatch.setattr(mb, "collect", lambda: ({"runs": mb.summarize_runs(RUNS),
                                                "router": None, "approvals": None,
                                                "memory": None}, {}))
    posted = {}

    def fake_post(url, payload):
        posted["url"] = url
        posted["payload"] = payload
        return 200

    monkeypatch.setattr(mb, "post_webhook", fake_post)
    assert mb.main() == 0
    assert posted["url"].startswith("https://hooks.slack.com")
    assert "Morning brief" in posted["payload"]["text"]


def test_main_returns_1_when_post_fails_but_still_prints(monkeypatch, capsys):
    monkeypatch.setenv("BRIEF_WEBHOOK_URL", "https://discord.com/api/webhooks/x")
    monkeypatch.setattr(mb, "collect", lambda: ({"runs": None, "router": None,
                                                "approvals": None, "memory": None}, {}))

    def boom(url, payload):
        raise RuntimeError("webhook 500")

    monkeypatch.setattr(mb, "post_webhook", boom)
    assert mb.main() == 1
    assert "Morning brief" in capsys.readouterr().out


def test_collect_sections_survive_source_failure(monkeypatch):
    monkeypatch.setenv("GOVERNOR_BASE_URL", "http://gov.test")
    monkeypatch.setenv("ROUTER_BASE_URL", "http://router.test")
    monkeypatch.setenv("PAPERCLIP_BASE_URL", "http://pc.test")
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", "c1")
    monkeypatch.setenv("PAPERCLIP_AUTOMATION_JWT_SECRET", "s")

    def fake_fetch(url, headers=None, timeout=20):
        if "router.test/health" in url:
            return HEALTH
        if "/digest" in url:
            return DIGEST
        raise RuntimeError(f"down: {url}")

    monkeypatch.setattr(mb, "fetch_json", fake_fetch)
    sections, errors = mb.collect()
    assert sections["router"]["over_budget_tiers"] == ["claude"]
    assert sections["memory"] == DIGEST
    assert sections["runs"] is None and "runs" in errors
    assert sections["approvals"] is None and "approvals" in errors


def test_mint_jwt_is_hs256_with_automation_role():
    import base64
    import json

    tok = mb.mint_jwt("secret")
    h, p, s = tok.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
    assert json.loads(base64.urlsafe_b64decode(pad(h)))["alg"] == "HS256"
    payload = json.loads(base64.urlsafe_b64decode(pad(p)))
    assert payload["role"] == "automation"
    assert payload["exp"] > payload["iat"]
