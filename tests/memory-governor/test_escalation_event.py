"""POST /escalation-event emit endpoint — offline tests.

The single emit endpoint for wired approval surfaces (the auth-proxy HITL gate).
Called directly (asyncio.run) with a monkeypatched db.emit_event / fake pool,
the test_escalation_sla.py convention. No DB, no HTTP.
"""

import asyncio

import pytest

from governor import db as governor_db
from governor import main as governor_main
from governor.main import EscalationEventIn
from fastapi import HTTPException


def _record_emit(monkeypatch):
    """Replace db.emit_event with a recorder; returns the calls list."""
    calls = []

    async def _fake_emit(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(governor_db, "emit_event", _fake_emit)
    return calls


def test_autonomy_decision_emits_with_decision_and_latency(monkeypatch):
    calls = _record_emit(monkeypatch)
    out = asyncio.run(
        governor_main.escalation_event_emit(
            EscalationEventIn(
                event_type="autonomy_decision",
                escalation_id="esc-1",
                lane="red",
                source="approval",
                workspace="tenant-a",
                actor_peer="forge",
                issue_id="i9",
                decision="denied",
                latency_ms=42,
            )
        )
    )
    assert out["accepted"] is True
    assert out["escalation_id"] == "esc-1"
    assert len(calls) == 1
    call = calls[0]
    assert call["event_type"] == "autonomy_decision"
    assert call["actor_peer"] == "forge"
    assert call["issue_id"] == "i9"
    assert call["channel"] == "approval"
    payload = call["payload"]
    assert payload["escalation_id"] == "esc-1"
    assert payload["lane"] == "red"
    assert payload["source"] == "approval"
    assert payload["workspace"] == "tenant-a"
    assert payload["decision"] == "denied"
    assert payload["latency_ms"] == 42


def test_escalation_opened_emits_without_decision_keys(monkeypatch):
    calls = _record_emit(monkeypatch)
    asyncio.run(
        governor_main.escalation_event_emit(
            EscalationEventIn(
                event_type="escalation_opened",
                escalation_id="esc-2",
                lane="red",
                source="approval",
                workspace="tenant-a",
            )
        )
    )
    payload = calls[0]["payload"]
    assert "decision" not in payload
    assert "latency_ms" not in payload


def test_unknown_event_type_is_400(monkeypatch):
    _record_emit(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            governor_main.escalation_event_emit(
                EscalationEventIn(
                    event_type="not_a_real_event",
                    escalation_id="esc-3",
                    lane="red",
                    source="approval",
                    workspace="tenant-a",
                )
            )
        )
    assert exc.value.status_code == 400


def test_bad_lane_is_400(monkeypatch):
    _record_emit(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            governor_main.escalation_event_emit(
                EscalationEventIn(
                    event_type="autonomy_decision",
                    escalation_id="esc-4",
                    lane="purple",  # not in LANES
                    source="approval",
                    workspace="tenant-a",
                )
            )
        )
    assert exc.value.status_code == 400


def test_bad_source_is_400(monkeypatch):
    _record_emit(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            governor_main.escalation_event_emit(
                EscalationEventIn(
                    event_type="autonomy_decision",
                    escalation_id="esc-5",
                    lane="red",
                    source="not_a_source",
                    workspace="tenant-a",
                )
            )
        )
    assert exc.value.status_code == 400


def test_flag_off_is_noop_but_accepted(monkeypatch):
    """AGENT_EVENTS_ENABLED off → the real emitter writes nothing, but the
    endpoint still reports accepted (fail-open, observability-not-control-flow)."""

    executed = []

    class _FakePool:
        async def execute(self, *args):
            executed.append(args)

    async def _fake_pool():
        return _FakePool()

    async def _fake_flag(name):
        return False  # AGENT_EVENTS_ENABLED off

    monkeypatch.setattr(governor_db, "pool", _fake_pool)
    monkeypatch.setattr(governor_db, "flag_enabled", _fake_flag)

    out = asyncio.run(
        governor_main.escalation_event_emit(
            EscalationEventIn(
                event_type="escalation_opened",
                escalation_id="esc-6",
                lane="red",
                source="approval",
                workspace="tenant-a",
            )
        )
    )
    assert out["accepted"] is True
    assert executed == []  # no row written
