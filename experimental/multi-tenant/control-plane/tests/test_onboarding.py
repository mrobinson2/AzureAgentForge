"""Tenant onboarding — contract validation + step planning, offline."""

import pytest

from onboarding import OnboardingStep, plan_provisioning, validate_contract


def _contract(**over):
    c = {
        "slug": "acme",
        "display_name": "Acme Co",
        "primary_email": "ops@acme.test",
        "vertical": "field-service",
        "pack": "example-fieldservice",
        "daily_budget_cap": 5.00,
        "users": [
            {"email": "boss@acme.test", "role": "owner"},
            {"email": "tech@acme.test", "role": "member"},
        ],
    }
    c.update(over)
    return c


def test_valid_contract_has_no_errors():
    assert validate_contract(_contract()) == []


def test_plan_orders_steps_tenant_budget_workspace_users_pack():
    steps = plan_provisioning(_contract())
    actions = [s.action for s in steps]
    assert actions == [
        "create_tenant", "set_budget", "provision_workspace",
        "create_user", "create_user", "enable_pack",
    ]
    assert isinstance(steps[0], OnboardingStep)


def test_owner_is_provisioned_before_member():
    steps = plan_provisioning(_contract())
    user_steps = [s for s in steps if s.action == "create_user"]
    assert user_steps[0].detail["role"] == "owner"
    assert user_steps[1].detail["role"] == "member"


def test_missing_required_fields_reported():
    errs = validate_contract({"users": []})
    assert any("slug is required" in e for e in errs)
    assert any("daily_budget_cap" in e for e in errs)
    assert any("at least one user" in e for e in errs)


def test_bad_email_and_budget_and_role():
    errs = validate_contract(_contract(
        primary_email="not-an-email",
        daily_budget_cap=0,
        users=[{"email": "x@y.z", "role": "superuser"}],
    ))
    assert any("primary_email must be" in e for e in errs)
    assert any("daily_budget_cap must be a positive" in e for e in errs)
    assert any("role must be one of" in e for e in errs)


def test_no_owner_is_rejected():
    errs = validate_contract(_contract(
        users=[{"email": "a@b.c", "role": "member"}],
    ))
    assert any("owner" in e for e in errs)


def test_plan_raises_on_invalid_contract():
    with pytest.raises(ValueError) as exc:
        plan_provisioning(_contract(slug="", daily_budget_cap=-1))
    msg = str(exc.value)
    assert "slug is required" in msg
    assert "daily_budget_cap" in msg


def test_budget_bool_is_not_a_valid_cap():
    # True is an int subclass — must not sneak through as a cap of 1
    errs = validate_contract(_contract(daily_budget_cap=True))
    assert any("daily_budget_cap must be a positive" in e for e in errs)
