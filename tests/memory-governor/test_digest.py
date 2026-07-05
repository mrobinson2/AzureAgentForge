"""Offline tests for the daily memory digest formatter."""

from governor.digest import format_digest


def test_digest_with_activity():
    out = format_digest({
        "window_hours": 24,
        "writes_by_class": {"durable_fact": 3, "decaying": 6, "user_preference": 1},
        "confirmed": 2, "disputed": 0, "expired": 1, "promoted": 0,
        "pin_candidates_pending": 1, "needs_review": 1,
    })
    assert "3 durable_fact" in out and "6 decaying" in out and "1 user_preference" in out
    assert "2 confirmed" in out and "1 expired" in out
    assert "disputed" not in out  # zero counts omitted
    assert "1 pin-candidate(s) pending" in out and "1 need(s) review" in out
    assert "pc-memory list --pin-candidates" in out


def test_digest_quiet_day():
    out = format_digest({"window_hours": 24, "writes_by_class": {}})
    assert "no new memories written" in out
    assert "pin-candidate" not in out


def test_digest_class_render_order():
    out = format_digest({"writes_by_class": {"decaying": 1, "durable_fact": 1}})
    # durable_fact precedes decaying per CLASS_ORDER regardless of dict order
    assert out.index("durable_fact") < out.index("decaying")
