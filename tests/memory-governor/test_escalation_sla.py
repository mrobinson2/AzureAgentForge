"""Escalation SLA auditor — offline tests.

Pure pairing/rollup/SLA-math/renderer tests for governor.escalation_sla (no
DB, no HTTP; mirrors test_memory_digest.py's pure section), plus
handler-level tests for GET /escalation-sla and the ESCALATION_SLA_ENABLED
/digest fold-in — called directly (asyncio.run) against a fake pool, the
test_memory_inspector.py convention. Live E2E (real emitters + real DB) lands
with the event emitters themselves; today the pipeline is deliberately dark.
"""

import asyncio
from datetime import datetime, timezone

from governor import db as governor_db
from governor import main as governor_main
from governor.escalation_sla import (
    DEFAULT_SLA_MINUTES,
    EVENT_ACKED,
    EVENT_AUTONOMY_DECISION,
    EVENT_OPENED,
    EVENT_RESOLVED,
    build_sla_stats,
    clock_start,
    empty_stats,
    escalation_payload,
    format_attention_lines,
    format_escalation_report,
    format_summary_line,
    pair_escalations,
    resolve_sla_config,
)

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


def _ev(event_type, ts, escalation_id, ws="tenant-a", actor="system", **payload):
    return {
        "ts": ts,
        "actor_peer": actor,
        "event_type": event_type,
        "payload": {"escalation_id": escalation_id, "workspace": ws, **payload},
    }


def _open(ts, eid, ws="tenant-a", lane="yellow", source="approval"):
    return _ev(EVENT_OPENED, ts, eid, ws=ws, lane=lane, source=source)


# ---------------------------------------------------------------------------
# Taxonomy helper
# ---------------------------------------------------------------------------


def test_escalation_payload_builds_canonical_shape():
    p = escalation_payload("hold-1", lane="yellow", source="approval",
                           workspace="tenant-a", hold_id="hold-1")
    assert p["escalation_id"] == "hold-1"
    assert p["lane"] == "yellow"
    assert p["source"] == "approval"
    assert p["workspace"] == "tenant-a"
    assert p["hold_id"] == "hold-1"


def test_escalation_payload_rejects_bad_lane_source_and_missing_id():
    import pytest

    with pytest.raises(ValueError):
        escalation_payload("x", lane="green", source="approval", workspace="w")
    with pytest.raises(ValueError):
        escalation_payload("x", lane="red", source="carrier-pigeon", workspace="w")
    with pytest.raises(ValueError):
        escalation_payload("", lane="red", source="handoff", workspace="w")


# ---------------------------------------------------------------------------
# Pairing / rollup
# ---------------------------------------------------------------------------


def test_open_ack_pair_yields_latency():
    rows = [
        _open("2026-07-10T10:00:00+00:00", "e1"),
        _ev(EVENT_ACKED, "2026-07-10T10:14:00+00:00", "e1", actor="operator"),
    ]
    recs = pair_escalations(rows, now=NOW)
    assert len(recs) == 1
    assert recs[0]["ack_latency_ms"] == 14 * 60_000
    assert recs[0]["acked_by"] == "operator"


def test_open_without_ack_is_unacked_with_age():
    rows = [_open("2026-07-10T11:00:00+00:00", "e1")]
    stats = build_sla_stats(rows, workspace="tenant-a", window_days=7, now=NOW)
    assert stats["total"] == 1
    assert stats["unacked"] == 1
    assert stats["acked"] == 0
    e = stats["escalations"][0]
    assert e["acked"] is False
    assert e["age_minutes"] == 60.0


def test_autonomy_decision_backfills_ack_for_approval_open():
    # Retro-compat: decision approved/denied = ack+resolution at latency_ms.
    rows = [
        _open("2026-07-10T10:00:00+00:00", "hold-7"),
        _ev(EVENT_AUTONOMY_DECISION, "2026-07-10T10:05:00+00:00", "hold-7",
            decision="approved", latency_ms=5 * 60_000),
    ]
    stats = build_sla_stats(rows, workspace="tenant-a", window_days=7, now=NOW)
    assert stats["acked"] == 1
    assert stats["resolved"] == 1
    assert stats["all_closed"] is True
    assert stats["breaches"] == 0
    assert stats["escalations"][0]["outcome"] == "approved"


def test_autonomy_decision_with_no_open_synthesizes_historical_hold():
    # Historical holds (pre-taxonomy) are auditable retroactively; hold_id
    # serves as the escalation_id fallback.
    rows = [{
        "ts": "2026-07-10T09:00:00+00:00",
        "actor_peer": "operator",
        "event_type": EVENT_AUTONOMY_DECISION,
        "payload": {"hold_id": "hold-old", "workspace": "tenant-a",
                    "decision": "denied", "latency_ms": 120_000},
    }]
    stats = build_sla_stats(rows, workspace="tenant-a", window_days=7, now=NOW)
    assert stats["total"] == 1
    assert stats["acked"] == 1
    assert stats["resolved"] == 1
    assert stats["median_ack_ms"] == 120_000
    assert stats["escalations"][0]["source"] == "approval"


def test_expired_is_breach_and_unresolved():
    rows = [
        _open("2026-07-10T10:00:00+00:00", "hold-9"),
        _ev(EVENT_AUTONOMY_DECISION, "2026-07-10T10:30:00+00:00", "hold-9",
            decision="expired"),
    ]
    stats = build_sla_stats(rows, workspace="tenant-a", window_days=7, now=NOW)
    assert stats["expired"] == 1
    assert stats["breaches"] == 1
    assert stats["resolved"] == 0
    assert stats["all_closed"] is False


def test_median_over_fixture_set():
    rows = []
    for i, mins in enumerate([2, 14, 40]):
        rows.append(_open(f"2026-07-10T0{i + 1}:00:00+00:00", f"e{i}"))
        rows.append(_ev(EVENT_ACKED, f"2026-07-10T0{i + 1}:{mins:02d}:00+00:00", f"e{i}",
                        ack_latency_ms=mins * 60_000))
    stats = build_sla_stats(rows, workspace="tenant-a", window_days=7, now=NOW)
    assert stats["median_ack_ms"] == 14 * 60_000


def test_cross_workspace_isolation():
    # Tenant A's open must never pair with tenant B's ack for the same id.
    rows = [
        _open("2026-07-10T10:00:00+00:00", "e1", ws="tenant-a"),
        _ev(EVENT_ACKED, "2026-07-10T10:05:00+00:00", "e1", ws="tenant-b"),
    ]
    recs = pair_escalations(rows, now=NOW)
    a = [r for r in recs if r["workspace"] == "tenant-a"]
    assert len(a) == 1
    assert a[0]["acked_at"] is None


def test_duplicate_acks_first_wins():
    rows = [
        _open("2026-07-10T10:00:00+00:00", "e1"),
        _ev(EVENT_ACKED, "2026-07-10T10:05:00+00:00", "e1", acked_by="first"),
        _ev(EVENT_ACKED, "2026-07-10T11:00:00+00:00", "e1", acked_by="second"),
    ]
    recs = pair_escalations(rows, now=NOW)
    assert len(recs) == 1
    assert recs[0]["acked_by"] == "first"
    assert recs[0]["ack_latency_ms"] == 5 * 60_000


def test_zero_escalation_window_yields_empty_stats():
    stats = build_sla_stats([], workspace="tenant-a", window_days=7, now=NOW)
    assert stats["total"] == 0
    assert stats["median_ack_ms"] is None
    assert stats["escalations"] == []
    # empty_stats (the DB-hiccup fail-open shape) matches the honest zero.
    empty = empty_stats("tenant-a", 7)
    assert empty["total"] == 0 and empty["breaches"] == 0


def test_resolved_event_tracks_closure_separately_from_ack():
    rows = [
        _open("2026-07-10T10:00:00+00:00", "e1", source="handoff"),
        _ev(EVENT_ACKED, "2026-07-10T10:10:00+00:00", "e1"),
        _ev(EVENT_RESOLVED, "2026-07-10T10:40:00+00:00", "e1", outcome="handled"),
    ]
    stats = build_sla_stats(rows, workspace="tenant-a", window_days=7, now=NOW)
    assert stats["acked"] == 1
    assert stats["resolved"] == 1
    assert stats["all_closed"] is True
    assert stats["escalations"][0]["outcome"] == "handled"


# ---------------------------------------------------------------------------
# SLA math
# ---------------------------------------------------------------------------


def test_breach_boundary_sla_plus_one_second_breaches():
    rows = [
        _open("2026-07-10T10:00:00+00:00", "late"),
        _ev(EVENT_ACKED, "2026-07-10T10:30:01+00:00", "late"),
        _open("2026-07-10T10:00:00+00:00", "fine"),
        _ev(EVENT_ACKED, "2026-07-10T10:29:59+00:00", "fine"),
    ]
    stats = build_sla_stats(rows, workspace="tenant-a", window_days=7,
                            sla_config=30, now=NOW)
    by_id = {e["escalation_id"]: e for e in stats["escalations"]}
    assert by_id["late"]["breach"] is True
    assert by_id["fine"]["breach"] is False
    assert stats["breaches"] == 1


def test_business_hours_clock_starts_at_next_opening():
    # Mon-Fri 08:00-17:00; open Friday 22:00 -> clock starts Monday 08:00.
    bh = {"days": [0, 1, 2, 3, 4], "start": "08:00", "end": "17:00"}
    opened = datetime(2026, 7, 10, 22, 0, tzinfo=timezone.utc)  # Friday
    start = clock_start(opened, bh)
    assert start == datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)  # Monday
    # In-hours open: clock is the open time itself.
    in_hours = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    assert clock_start(in_hours, bh) == in_hours
    # And the latency math uses the shifted clock: acked Monday 08:10 = 10m.
    rows = [
        _open("2026-07-10T22:00:00+00:00", "e1"),
        _ev(EVENT_ACKED, "2026-07-13T08:10:00+00:00", "e1"),
    ]
    stats = build_sla_stats(
        rows, workspace="tenant-a", window_days=7,
        sla_config={"ack_sla_minutes": 30, "business_hours": bh},
        now=datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc),
    )
    e = stats["escalations"][0]
    assert e["ack_latency_ms"] == 10 * 60_000
    assert e["breach"] is False


def test_missing_tenant_sla_uses_platform_default():
    cfg = resolve_sla_config(None)
    assert cfg["ack_sla_minutes"] == DEFAULT_SLA_MINUTES
    assert cfg["warnings"] == []
    stats = build_sla_stats([], workspace="t", window_days=7, now=NOW)
    assert stats["sla_minutes"] == DEFAULT_SLA_MINUTES


def test_malformed_sla_config_defaults_with_warning_never_crashes():
    for junk in ("soon-ish", -5, 0, ["30"], True, {"ack_sla_minutes": "nope"}):
        cfg = resolve_sla_config(junk)
        assert cfg["ack_sla_minutes"] == DEFAULT_SLA_MINUTES, junk
        assert cfg["warnings"], junk
    # Malformed business_hours degrades to the calendar clock + warning.
    cfg = resolve_sla_config({"ack_sla_minutes": 15, "business_hours": {"days": "weekdays"}})
    assert cfg["ack_sla_minutes"] == 15
    assert cfg["business_hours"] is None
    assert cfg["warnings"]
    # And the whole rollup still works with junk config.
    stats = build_sla_stats([_open("2026-07-10T11:50:00+00:00", "e1")],
                            workspace="t", window_days=7, sla_config="junk", now=NOW)
    assert stats["sla_minutes"] == DEFAULT_SLA_MINUTES
    assert stats["sla_warnings"]


def test_open_past_sla_with_no_ack_counts_as_breach_now():
    rows = [_open("2026-07-10T11:00:00+00:00", "e1")]  # 60m old vs 30m SLA
    stats = build_sla_stats(rows, workspace="t", window_days=7, now=NOW)
    assert stats["breaches"] == 1
    assert stats["escalations"][0]["breach"] is True
    # A younger open is aging, not breached.
    rows = [_open("2026-07-10T11:50:00+00:00", "e2")]  # 10m old
    stats = build_sla_stats(rows, workspace="t", window_days=7, now=NOW)
    assert stats["breaches"] == 0


# ---------------------------------------------------------------------------
# Renderers + fold-in (fake pool for the handler paths, no real DB)
# ---------------------------------------------------------------------------


class _FakePool:
    """Replays canned rows per fetch()/fetchrow() call (in call order) and
    records the SQL of every call — same convention as test_memory_digest."""

    def __init__(self, results=None, fetchrow_results=None):
        self._results = list(results or [])
        self._fetchrow_results = list(fetchrow_results or [])
        self.fetch_calls = []
        self.fetchrow_calls = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self._results.pop(0) if self._results else []

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        return self._fetchrow_results.pop(0) if self._fetchrow_results else None


def _patch_pool(monkeypatch, pool):
    async def _fake_pool():
        return pool

    monkeypatch.setattr(governor_db, "pool", _fake_pool)


def _patch_flag(monkeypatch, enabled):
    async def _fake_flag(name):
        return enabled

    monkeypatch.setattr(governor_db, "flag_enabled", _fake_flag)


def _stats_one_breached_one_aging():
    rows = [
        _open("2026-07-10T10:00:00+00:00", "hold-1"),        # 120m unacked -> breach
        _open("2026-07-10T11:50:00+00:00", "e2", lane="red", source="presend"),  # 10m -> aging
    ]
    return build_sla_stats(rows, workspace="tenant-a", window_days=1, now=NOW)


def test_attention_lines_golden_breached_plus_aging(monkeypatch):
    lines = format_attention_lines(_stats_one_breached_one_aging())
    text = "\n".join(lines)
    assert "🚨 1 escalation past ack SLA (30m)" in text
    assert "hold-1" in text
    assert "unacked for 120.0m" in text
    assert "⏳ 1 escalation awaiting ack (oldest 10m, SLA 30m)" in text
    # Folded into the actual daily /digest when the flag is on (fake pool;
    # flags on enables the memory-digest listing too — order: events fetch,
    # 3 review-queue fetches, then the escalation-events fetch).
    esc_rows = [
        _open("2026-07-10T10:00:00+00:00", "hold-1"),
        _ev(EVENT_AUTONOMY_DECISION, "2026-07-10T10:30:00+00:00", "hold-1",
            decision="expired"),
    ]
    pool = _FakePool(results=[[], [], [], [], esc_rows],
                     fetchrow_results=[{"pin_candidates": 0, "needs_review": 0}])
    _patch_pool(monkeypatch, pool)
    _patch_flag(monkeypatch, True)
    out = asyncio.run(governor_main.memory_digest(window_hours=24))
    assert "escalation_sla" in out
    assert "Escalation SLA" in out["text"]
    assert "🚨 1 escalation past ack SLA (30m)" in out["text"]
    assert "hold-1" in out["text"]


def test_summary_line_golden_every_escalation_closed(monkeypatch):
    rows = []
    for i, mins in enumerate([2, 14, 40]):
        rows.append(_open(f"2026-07-10T0{i + 1}:00:00+00:00", f"e{i}"))
        rows.append(_ev(EVENT_AUTONOMY_DECISION, f"2026-07-10T0{i + 1}:{mins:02d}:00+00:00",
                        f"e{i}", decision="approved", latency_ms=mins * 60_000))
    stats = build_sla_stats(rows, workspace="tenant-a", window_days=7, now=NOW)
    line = format_summary_line(stats)
    assert "every escalation closed, median ack 14m" in line
    # The 40m ack breached the 30m SLA — the brag stays honest about it.
    assert "1 breached the 30m ack SLA" in line
    # And the /escalation-sla endpoint renders the same line (fake pool).
    pool = _FakePool(results=[rows])
    _patch_pool(monkeypatch, pool)
    out = asyncio.run(governor_main.escalation_sla_endpoint(
        workspace="tenant-a", window_days=7))
    assert "every escalation closed, median ack 14m" in out["text"]
    # workspace-scoped fetch filters on the payload's tenant dimension
    assert "payload->>'workspace' = $1" in pool.fetch_calls[0][0]


def test_zero_week_wording_never_fabricates():
    stats = build_sla_stats([], workspace="tenant-a", window_days=7, now=NOW)
    line = format_summary_line(stats)
    assert "no escalations required your attention this week" in line
    assert "median" not in line
    assert format_attention_lines(stats) == []
    # Standalone report render is honest too.
    text = format_escalation_report(stats)
    assert "no escalations required your attention" in text


def test_unpaired_opens_week_shows_them_open_never_dropped():
    rows = [
        _open("2026-07-10T10:00:00+00:00", "e1"),
        _open("2026-07-10T10:30:00+00:00", "e2"),
        _ev(EVENT_ACKED, "2026-07-10T10:35:00+00:00", "e2"),
        _ev(EVENT_RESOLVED, "2026-07-10T10:36:00+00:00", "e2"),
    ]
    stats = build_sla_stats(rows, workspace="tenant-a", window_days=7, now=NOW)
    line = format_summary_line(stats)
    assert "1 of 2 escalations closed" in line
    assert "1 still open" in line
    assert "every escalation closed" not in line


# ---------------------------------------------------------------------------
# Ship-dark contract: with the flag off, /digest is byte-for-byte unchanged
# ---------------------------------------------------------------------------


def test_digest_unchanged_without_escalation_flag(monkeypatch):
    pool = _FakePool(results=[[]],
                     fetchrow_results=[{"pin_candidates": 0, "needs_review": 0}])
    _patch_pool(monkeypatch, pool)
    _patch_flag(monkeypatch, False)
    out = asyncio.run(governor_main.memory_digest(window_hours=24))
    assert "escalation_sla" not in out
    assert "escalation" not in out["text"].lower()
    # only the events fetch ran — no rollup queries fired with the flag off
    assert len(pool.fetch_calls) == 1


def test_expired_renders_fail_closed_wording():
    rows = [
        _open("2026-07-10T10:00:00+00:00", "hold-9"),
        _ev(EVENT_AUTONOMY_DECISION, "2026-07-10T10:30:00+00:00", "hold-9",
            decision="expired"),
    ]
    stats = build_sla_stats(rows, workspace="tenant-a", window_days=1, now=NOW)
    text = "\n".join(format_attention_lines(stats))
    assert "expired unacked" in text
    assert "hold-9" in text
