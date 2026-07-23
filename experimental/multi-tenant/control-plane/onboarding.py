# Reference design — part of the multi-tenant roadmap. Phase 5 of
# docs/notes/plans/2026-07-22-full-multi-tenant.md: tenant onboarding.

"""Tenant onboarding — contract validation + provisioning-step planning.

provision_tenant.py is the imperative "do it" path (it POSTs to the live
control-plane). This module is the offline core that runs BEFORE any call:
validate a tenant contract and plan the ordered provisioning steps, so a bad
contract fails with a clear list of reasons instead of a half-provisioned
tenant. Pure — no I/O — so it is unit-testable and the executor just walks the
returned step list.
"""

from __future__ import annotations

from dataclasses import dataclass

from user_tokens import ROLES


@dataclass(frozen=True)
class OnboardingStep:
    action: str
    detail: dict


_REQUIRED_STR = ("slug", "display_name", "primary_email", "vertical", "pack")


def validate_contract(contract: dict) -> list[str]:
    """Return a list of human-readable problems (empty == valid)."""
    errors: list[str] = []
    c = contract or {}

    for field in _REQUIRED_STR:
        val = c.get(field)
        if not isinstance(val, str) or not val.strip():
            errors.append(f"{field} is required")

    email = c.get("primary_email")
    if isinstance(email, str) and email and "@" not in email:
        errors.append("primary_email must be an email address")

    cap = c.get("daily_budget_cap")
    if not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0:
        errors.append("daily_budget_cap must be a positive number")

    users = c.get("users")
    if not isinstance(users, list) or not users:
        errors.append("at least one user is required")
    else:
        for i, u in enumerate(users):
            if not isinstance(u, dict):
                errors.append(f"users[{i}] must be an object")
                continue
            if "@" not in str(u.get("email", "")):
                errors.append(f"users[{i}].email must be an email address")
            if u.get("role") not in ROLES:
                errors.append(f"users[{i}].role must be one of {ROLES}")
        if not any(isinstance(u, dict) and u.get("role") == "owner" for u in users):
            errors.append("exactly one user must have the 'owner' role")

    return errors


def plan_provisioning(contract: dict) -> list[OnboardingStep]:
    """Validate then produce the ordered provisioning steps. Raises ValueError
    (with every reason) on an invalid contract — never a partial plan.

    Order matters: the tenant record and its budget/workspace exist before any
    user is attached, and the pack is enabled last (it may reference users)."""
    errors = validate_contract(contract)
    if errors:
        raise ValueError("invalid tenant contract: " + "; ".join(errors))

    steps: list[OnboardingStep] = [
        OnboardingStep("create_tenant", {
            "slug": contract["slug"],
            "display_name": contract["display_name"],
            "vertical": contract["vertical"],
        }),
        OnboardingStep("set_budget", {"daily_budget_cap": contract["daily_budget_cap"]}),
        OnboardingStep("provision_workspace", {"slug": contract["slug"]}),
    ]
    # Owner first, so the tenant always has an administrator before members land.
    for user in sorted(contract["users"], key=lambda u: ROLES.index(u["role"]), reverse=True):
        steps.append(OnboardingStep("create_user", {
            "email": user["email"], "role": user["role"],
        }))
    steps.append(OnboardingStep("enable_pack", {"pack": contract["pack"]}))
    return steps
