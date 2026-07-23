"""Per-tenant daily budget cap — offline tests."""

import pytest

from tenant_budget import BudgetDecision, BudgetMode, TenantBudget

DAY = "2026-07-22"


def _budget(mode=BudgetMode.BLOCK):
    return TenantBudget({"t1": 1.00, "t2": 0.50}, default_cap=0.10, mode=mode)


def test_under_cap_allows_regardless_of_mode():
    for mode in BudgetMode:
        b = _budget(mode)
        assert b.check("t1", 0.50, DAY) is BudgetDecision.ALLOW


def test_over_cap_blocks_in_block_mode():
    b = _budget(BudgetMode.BLOCK)
    assert b.check("t2", 0.75, DAY) is BudgetDecision.BLOCK


def test_over_cap_warns_in_warn_mode():
    b = _budget(BudgetMode.WARN)
    assert b.check("t2", 0.75, DAY) is BudgetDecision.WARN


def test_over_cap_allows_in_off_mode():
    b = _budget(BudgetMode.OFF)
    assert b.check("t2", 0.75, DAY) is BudgetDecision.ALLOW


def test_unknown_tenant_uses_default_cap():
    b = _budget()
    assert b.cap_for("nope") == 0.10
    assert b.check("nope", 0.20, DAY) is BudgetDecision.BLOCK
    assert b.check("nope", 0.05, DAY) is BudgetDecision.ALLOW


def test_record_accumulates_and_blocks_at_boundary():
    b = _budget(BudgetMode.BLOCK)
    assert b.record("t2", 0.40, DAY) is BudgetDecision.ALLOW
    assert b.remaining("t2", DAY) == pytest.approx(0.10)
    # next charge would exceed 0.50 -> blocked, and NOT recorded
    assert b.record("t2", 0.20, DAY) is BudgetDecision.BLOCK
    assert b.spent("t2", DAY) == pytest.approx(0.40)
    # a charge that fits still goes through
    assert b.record("t2", 0.10, DAY) is BudgetDecision.ALLOW
    assert b.remaining("t2", DAY) == pytest.approx(0.0)


def test_per_day_isolation():
    b = _budget()
    b.record("t1", 1.00, "2026-07-22")
    assert b.remaining("t1", "2026-07-22") == pytest.approx(0.0)
    # a new day starts fresh
    assert b.remaining("t1", "2026-07-23") == pytest.approx(1.00)


def test_per_tenant_isolation():
    b = _budget()
    b.record("t1", 1.00, DAY)
    # t2's ledger is untouched by t1's spend
    assert b.spent("t2", DAY) == 0.0
    assert b.remaining("t2", DAY) == pytest.approx(0.50)


def test_warn_mode_records_overage():
    b = _budget(BudgetMode.WARN)
    assert b.record("t2", 0.75, DAY) is BudgetDecision.WARN
    # warn allows the spend, so it accumulates (unlike block)
    assert b.spent("t2", DAY) == pytest.approx(0.75)
