"""Per-tenant daily budget ledger — reference test (not wired into CI).

Tenant provisioning attaches metadata.tenant_id + metadata.tenant_daily_budget_usd
to every request from a tenant's agents; the router enforces a per-tenant DAILY
budget with a SOFT downgrade to the cheapest tier when the tenant exhausts it
(NOT a hard reject — same posture as the per-run cost envelope).

Deliberately rides the same COST_ENVELOPE_ENABLED=1 gate as the per-run
envelope (per-tenant budgets are part of the cost-envelope program), so these
tests monkeypatch main._COST_ENVELOPE_ENABLED on explicitly. The `router`
fixture clears the per-tenant ledgers (_tenant_spend / _tenant_budget) before
each test.

Reference scaffolding: it targets a per-tenant ledger the public model router
does not ship, so it will not import/run as-is. Kept to document the contract.
"""

import pytest

# Reference scaffolding: this suite targets a per-tenant budget ledger the public
# model router does not vendor, so it cannot run standalone. Skip at collection
# time (rather than error) so a broad `pytest` invocation never trips on it.
pytest.skip(
    "reference scaffolding — per-tenant ledger not vendored in the public router",
    allow_module_level=True,
)


@pytest.fixture()
def tenant_router(router, monkeypatch):
    """The router with COST_ENVELOPE_ENABLED forced on for this test.

    Pins _budget_date to today so the lazy day-rollover in _reset_if_new_day
    (which clears the ledgers) doesn't fire mid-test and wipe a budget we set
    up directly. In production select_tier registers the budget after the date
    is already current, so this matches real ordering. The dedicated reset
    test re-forces a stale date itself.
    """
    import main as _m
    from datetime import date

    monkeypatch.setattr(router, "_COST_ENVELOPE_ENABLED", True)
    _m._budget_date = str(date.today())
    return router


def _body(tenant_id="acme-hvac", budget=0.50, model="standard-model", **extra):
    meta = {"tenant_id": tenant_id, "tenant_daily_budget_usd": budget}
    meta.update(extra.pop("metadata_extra", {}))
    return {"model": model, "messages": [], "metadata": meta, **extra}


# ── Gate: pure no-op when COST_ENVELOPE_ENABLED is unset ─────────────────────

class TestDisabledIsNoOp:
    def test_tenant_extraction_returns_none_when_disabled(self, router):
        # router fixture leaves the flag at its import-time value (off).
        assert router._COST_ENVELOPE_ENABLED is False
        assert router._tenant_from_body(_body()) is None

    def test_tenant_over_budget_always_false_when_disabled(self, router):
        # Even with spend recorded, the disabled feature must not report over.
        router._tenant_budget["acme-hvac"] = 0.01
        router._tenant_spend["acme-hvac"] = 99.0
        assert router.tenant_over_budget("acme-hvac") is False

    def test_tenant_id_of_returns_none_when_disabled(self, router):
        # record_cost still increments the per-tenant ledger if a tenant_id is
        # passed directly, but _tenant_id_of (the actual call-site source)
        # returns None when the feature is off, so the ledger stays empty.
        assert router._tenant_id_of(_body()) is None

    def test_select_tier_unaffected_when_disabled(self, router):
        # Over-budget metadata present but flag off → normal tier, no downgrade.
        router._tenant_budget["acme-hvac"] = 0.01
        router._tenant_spend["acme-hvac"] = 99.0
        assert router.select_tier(_body(budget=0.01)) == "standard-model"


# ── Tenant extraction ────────────────────────────────────────────────────────

class TestTenantExtraction:
    def test_extracts_tenant_id_and_budget(self, tenant_router):
        assert tenant_router._tenant_from_body(_body("acme", 0.25)) == ("acme", 0.25)

    def test_none_without_metadata(self, tenant_router):
        assert tenant_router._tenant_from_body({"model": "x", "messages": []}) is None

    def test_none_without_budget_field(self, tenant_router):
        body = {"model": "x", "messages": [], "metadata": {"tenant_id": "acme"}}
        assert tenant_router._tenant_from_body(body) is None

    def test_none_without_tenant_id(self, tenant_router):
        body = {"model": "x", "messages": [],
                "metadata": {"tenant_daily_budget_usd": 0.1}}
        assert tenant_router._tenant_from_body(body) is None

    def test_string_budget_is_parsed(self, tenant_router):
        body = {"model": "x", "messages": [],
                "metadata": {"tenant_id": "acme", "tenant_daily_budget_usd": "0.30"}}
        assert tenant_router._tenant_from_body(body) == ("acme", 0.30)

    def test_unparseable_budget_returns_none(self, tenant_router):
        body = {"model": "x", "messages": [],
                "metadata": {"tenant_id": "acme", "tenant_daily_budget_usd": "lots"}}
        assert tenant_router._tenant_from_body(body) is None

    def test_non_positive_budget_returns_none(self, tenant_router):
        body = {"model": "x", "messages": [],
                "metadata": {"tenant_id": "acme", "tenant_daily_budget_usd": 0}}
        assert tenant_router._tenant_from_body(body) is None

    def test_run_and_tenant_metadata_coexist(self, tenant_router):
        # The per-run envelope + the tenant budget ride the same metadata dict.
        body = _body("acme", 0.25,
                     metadata_extra={"run_id": "run-1", "budget_envelope_usd": 0.10})
        assert tenant_router._tenant_from_body(body) == ("acme", 0.25)
        assert tenant_router._envelope_from_body(body) == ("run-1", 0.10)


# ── Per-tenant ledger (mirrors TestRunLedger for the run ledger) ─────────────

class TestTenantLedger:
    def test_record_and_check(self, tenant_router):
        tenant_router._tenant_budget["acme"] = 0.10
        tenant_router.record_cost("gpt4o-mini", 0.04, tenant_id="acme")
        assert not tenant_router.tenant_over_budget("acme")
        tenant_router.record_cost("gpt4o-mini", 0.07, tenant_id="acme")
        assert tenant_router.tenant_over_budget("acme")

    def test_unknown_tenant_is_not_over(self, tenant_router):
        assert tenant_router.tenant_over_budget("never-seen") is False

    def test_none_tenant_is_not_over(self, tenant_router):
        assert tenant_router.tenant_over_budget(None) is False

    def test_record_cost_without_tenant_does_not_touch_tenant_ledger(self, tenant_router):
        tenant_router.record_cost("gpt4o-mini", 1.0, "run-1")
        assert dict(tenant_router._tenant_spend) == {}

    def test_record_cost_increments_tier_run_and_tenant_ledgers(self, tenant_router):
        # Per-tenant accounting is additive — tier + run ledgers keep working.
        tenant_router._run_envelope["R"] = 1.0
        tenant_router._tenant_budget["acme"] = 1.0
        tenant_router.record_cost("gpt4o-mini", 0.5, "R", tenant_id="acme")
        assert tenant_router._spend["gpt4o-mini"] == pytest.approx(0.5)
        assert tenant_router._run_spend["R"] == pytest.approx(0.5)
        assert tenant_router._tenant_spend["acme"] == pytest.approx(0.5)

    def test_spend_accumulates_across_runs_for_same_tenant(self, tenant_router):
        # The tenant ledger is per-tenant/day, not per-run: two different runs
        # from the same tenant burn the same daily budget.
        tenant_router._tenant_budget["acme"] = 1.0
        tenant_router.record_cost("gpt4o-mini", 0.4, "run-1", tenant_id="acme")
        tenant_router.record_cost("gpt4o-mini", 0.4, "run-2", tenant_id="acme")
        assert tenant_router._tenant_spend["acme"] == pytest.approx(0.8)

    def test_daily_reset_clears_tenant_ledgers(self, tenant_router):
        tenant_router._tenant_budget["acme"] = 0.01
        tenant_router.record_cost("gpt4o-mini", 99.0, tenant_id="acme")
        assert tenant_router.tenant_over_budget("acme")
        tenant_router._budget_date = "2000-01-01"  # force a "new day"
        # Reset is lazy — triggered by the next ledger touch.
        tenant_router.record_cost("gpt4o-mini", 0.0)
        assert tenant_router._tenant_spend.get("acme", 0.0) == 0.0
        assert tenant_router._tenant_budget.get("acme") is None


# ── select_tier downgrade (the enforcement point) ────────────────────────────

class TestSelectTierDowngrade:
    def test_registers_budget_on_first_call(self, tenant_router):
        tenant_router.select_tier(_body("acme", 0.20))
        assert tenant_router._tenant_budget["acme"] == 0.20

    def test_under_budget_keeps_requested_tier(self, tenant_router):
        tenant_router._tenant_budget["acme"] = 0.20
        tenant_router._tenant_spend["acme"] = 0.05
        assert tenant_router.select_tier(_body("acme", 0.20)) == "standard-model"

    def test_over_budget_soft_downgrades_to_floor(self, tenant_router):
        tenant_router._tenant_budget["acme"] = 0.10
        tenant_router._tenant_spend["acme"] = 0.50  # blew the budget
        assert tenant_router.select_tier(_body("acme", 0.10)) == "gpt4o-mini"

    def test_over_budget_does_not_hard_reject(self, tenant_router):
        # Soft downgrade returns a usable tier — never raises / never None.
        tenant_router._tenant_budget["acme"] = 0.10
        tenant_router._tenant_spend["acme"] = 0.50
        tier = tenant_router.select_tier(_body("acme", 0.10))
        assert tier in tenant_router.MODELS

    def test_floor_tier_request_not_downgraded_again(self, tenant_router):
        # Already on the floor tier → returned unchanged (no pointless redirect).
        tenant_router._tenant_budget["acme"] = 0.10
        tenant_router._tenant_spend["acme"] = 0.50
        assert tenant_router.select_tier(_body("acme", 0.10, model="gpt-4o-mini")) == "gpt4o-mini"

    def test_explicit_tier_request_over_budget_still_downgrades(self, tenant_router):
        tenant_router._tenant_budget["acme"] = 0.10
        tenant_router._tenant_spend["acme"] = 0.50
        body = _body("acme", 0.10)
        body["tier"] = "standard-model"
        assert tenant_router.select_tier(body) == "gpt4o-mini"

    def test_unregistered_floor_tier_leaves_tier_unchanged(self, tenant_router, monkeypatch):
        # A floor tier that isn't a registered model must never strand the
        # request — the downgrade is skipped and the requested tier survives.
        monkeypatch.setattr(tenant_router, "_ENVELOPE_FLOOR_TIER", "no-such-tier")
        tenant_router._tenant_budget["acme"] = 0.10
        tenant_router._tenant_spend["acme"] = 0.50
        assert tenant_router.select_tier(_body("acme", 0.10)) == "standard-model"

    def test_applies_after_run_envelope_both_land_on_floor(self, tenant_router):
        # Order: route directive → cost envelope → tenant budget. Both
        # downgrades target the same floor, so the strictest always sticks:
        # run under envelope but tenant over budget still lands on the floor.
        body = _body("acme", 0.10,
                     metadata_extra={"run_id": "run-9", "budget_envelope_usd": 5.0})
        tenant_router._tenant_budget["acme"] = 0.10
        tenant_router._tenant_spend["acme"] = 0.50
        assert tenant_router.select_tier(body) == "gpt4o-mini"


# ── /health tenants section (cost-attribution groundwork) ────────────────────

class TestHealthTenants:
    def test_health_exposes_tenant_spend_and_budget(self, tenant_router, client):
        tenant_router._tenant_budget["acme"] = 0.50
        tenant_router._tenant_spend["acme"] = 0.75
        resp = client.get("/health")
        assert resp.status_code == 200
        tenants = resp.json()["tenants"]
        assert tenants["acme"]["spend"] == pytest.approx(0.75)
        assert tenants["acme"]["budget"] == pytest.approx(0.50)
        assert tenants["acme"]["over_budget"] is True

    def test_health_tenants_empty_by_default(self, router, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["tenants"] == {}

    def test_health_tenant_under_budget_not_over(self, tenant_router, client):
        tenant_router._tenant_budget["acme"] = 1.00
        tenant_router._tenant_spend["acme"] = 0.10
        tenants = client.get("/health").json()["tenants"]
        assert tenants["acme"]["over_budget"] is False
