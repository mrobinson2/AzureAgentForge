"""Offline tests for the self-improvement loop: detector failure-lesson
fields, roster slug resolution, and the governed
durable_fact write payload. No network, no DB — the poster is stubbed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from services.watchdog import detectors, memory, roster  # noqa: E402


# ---------------------------------------------------------------------------
# roster: display name OR slug -> live slug
# ---------------------------------------------------------------------------

def test_roster_maps_display_name_to_live_slug():
    assert roster.slug_for("Researcher") == "researcher"
    assert roster.slug_for("Orchestrator") == "orchestrator"


def test_roster_passes_through_known_slug():
    # agent_events.actor_peer is already a slug — must round-trip unchanged.
    assert roster.slug_for("security") == "security"


def test_roster_case_insensitive_on_name():
    assert roster.slug_for("researcher") == "researcher"


def test_roster_unknown_or_empty_returns_none():
    assert roster.slug_for("Nobody") is None
    assert roster.slug_for("") is None
    assert roster.slug_for(None) is None
    assert roster.slug_for("00000000-0000-uuid") is None


# ---------------------------------------------------------------------------
# detectors carry a failure lesson + subject for agent-named findings
# ---------------------------------------------------------------------------

def _adapter_finding():
    runs = [{"id": "r1", "agentName": "Researcher", "status": "failed",
             "stopReason": "adapter_failed", "result": "the staging migration was not applied"}]
    return detectors.detect_adapter_failures(runs)[0]


def test_adapter_failure_carries_lesson_and_subject():
    f = _adapter_finding()
    assert f.subject_agent == "Researcher"
    assert f.lesson and "Researcher" in f.lesson


def test_budget_anomaly_carries_lesson():
    runs = [{"id": "r1", "agentName": "Orchestrator", "cost_usd": 14.0}]
    f = detectors.detect_budget_anomaly(runs, agent_caps={"Orchestrator": 15.0})[0]
    assert f.subject_agent == "Orchestrator"
    assert f.lesson


def test_fabrication_carries_lesson_for_worst_actor():
    events = [{"event_type": "phantom_delegation_blocked",
               "actor_peer": "orchestrator"}]
    f = detectors.detect_fabrication_signals(events)[0]
    assert f.subject_agent == "orchestrator"
    assert f.lesson


def test_stuck_wakes_is_infra_no_lesson():
    events = [{"event_type": "wakeup_queued", "payload": {"wakeup_id": f"w{i}"}, "ts": "t"}
              for i in range(5)]
    f = detectors.detect_stuck_wakes(events)[0]
    assert f.subject_agent is None
    assert f.lesson is None


# ---------------------------------------------------------------------------
# build_admit_payload: the governed durable_fact write body
# ---------------------------------------------------------------------------

def test_admit_payload_shape_for_agent_finding():
    f = _adapter_finding()
    p = memory.build_admit_payload(f, workspace="hermes-dev")
    assert p["memory_class"] == "durable_fact"
    assert p["scope_kind"] == "peer"
    assert p["scope_id"] == "researcher"
    assert p["observer"] == "researcher"
    assert p["observed"] == "researcher"
    assert p["source_type"] == "agent_observed"
    assert p["verification_state"] == "unverified"
    assert p["created_by_peer"] == "watchdog"
    assert p["planner_hint"] == "failure_lesson"
    # >= persist threshold so it lands as durable_fact, not decaying.
    assert p["confidence_score"] >= 0.80
    assert p["workspace_name"] == "hermes-dev"
    # signature embedded so re-writes of the SAME finding are byte-identical
    # (governor trigram dedup collapses them).
    assert f.signature in p["content"]


def test_admit_payload_none_for_infra_finding():
    events = [{"event_type": "wakeup_queued", "payload": {"wakeup_id": f"w{i}"}, "ts": "t"}
              for i in range(5)]
    f = detectors.detect_stuck_wakes(events)[0]
    assert memory.build_admit_payload(f, workspace="hermes-dev") is None


def test_admit_payload_none_for_unresolvable_agent():
    f = detectors.Finding(
        signature="x", severity="high", title="t", summary="s", evidence={},
        recommended_owner="Infrastructure", subject_agent="UnknownBot", lesson="some lesson")
    assert memory.build_admit_payload(f, workspace="hermes-dev") is None


# ---------------------------------------------------------------------------
# write_lesson: posts to /admit via the injectable poster
# ---------------------------------------------------------------------------

def test_write_lesson_posts_to_admit_with_key():
    calls = []

    def poster(url, payload, key):
        calls.append((url, payload, key))
        return {"status": "admitted", "doc_id": "abc"}

    verdict = memory.write_lesson(
        _adapter_finding(), base_url="http://gov", key="k",
        workspace="hermes-dev", poster=poster)
    assert verdict["status"] == "admitted"
    assert calls[0][0] == "http://gov/admit"
    assert calls[0][2] == "k"
    assert calls[0][1]["scope_id"] == "researcher"


def test_write_lesson_skips_infra_finding_without_calling_poster():
    called = []

    def poster(url, payload, key):
        called.append(1)
        return {}

    events = [{"event_type": "wakeup_queued", "payload": {"wakeup_id": f"w{i}"}, "ts": "t"}
              for i in range(5)]
    f = detectors.detect_stuck_wakes(events)[0]
    assert memory.write_lesson(
        f, base_url="http://gov", key="k", workspace="hermes-dev", poster=poster) is None
    assert called == []


# ---------------------------------------------------------------------------
# runtime JWT mint (a scheduled job has no operator to paste a token)
# ---------------------------------------------------------------------------

def test_mint_jwt_structure_and_claims():
    import base64 as _b64
    import json as _json

    from services.watchdog import watchdog

    def _unpad(s):
        return s + "=" * (-len(s) % 4)

    tok = watchdog.mint_jwt("test-secret", ttl_s=60)
    parts = tok.split(".")
    assert len(parts) == 3
    payload = _json.loads(_b64.urlsafe_b64decode(_unpad(parts[1])))
    assert payload["sub"] == "watchdog"
    assert "issues:write" in payload["scope"]
    assert payload["aud"] == "paperclip-api"
    assert payload["exp"] > payload["iat"]


def test_normalize_dsn_strips_sqlalchemy_driver():
    from services.watchdog import watchdog
    assert watchdog._normalize_dsn("postgresql+psycopg://u:p@h/db") == "postgresql://u:p@h/db"
    assert watchdog._normalize_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"
