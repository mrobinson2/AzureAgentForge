# Reference design — part of the multi-tenant roadmap. Phase 4 of
# docs/notes/plans/2026-07-22-full-multi-tenant.md: per-tenant budget cap.

"""Per-tenant daily budget cap — the pure ledger + enforcement logic.

Phase 4 enforces a per-tenant daily spend cap, keyed by tenant the way the
model-router already keys its per-caller cap. This module is the offline core:
an in-memory ledger + a three-mode enforcement decision (off / warn / block),
matching the router's BUDGET_ENFORCE_MODE vocabulary. The production wire (read
the tenant's cap from the control-plane, persist spend) sits on top of this;
the decision logic lives here so it is unit-testable with no clock and no DB.

Day is injected (a 'YYYY-MM-DD' string), never read from a clock, so tests are
deterministic and the same ledger is reusable across process restarts when the
caller supplies the current day.
"""

from __future__ import annotations

from enum import Enum


class BudgetMode(Enum):
    OFF = "off"      # observe only — always allow
    WARN = "warn"    # allow, but signal an overage
    BLOCK = "block"  # deny once the cap would be exceeded


class BudgetDecision(Enum):
    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"


class TenantBudget:
    """Daily spend ledger with a per-tenant cap.

    caps: tenant_id -> daily cap. Unknown tenants fall back to `default_cap`.
    A non-positive cap means "no spend allowed" (every charge trips the mode);
    to allow unlimited spend, use mode=OFF or a very large cap.
    """

    def __init__(
        self,
        caps: dict[str, float] | None = None,
        *,
        default_cap: float = 0.0,
        mode: BudgetMode = BudgetMode.BLOCK,
    ) -> None:
        self._caps = dict(caps or {})
        self._default_cap = default_cap
        self._mode = mode
        self._spent: dict[tuple[str, str], float] = {}

    def cap_for(self, tenant_id: str) -> float:
        return self._caps.get(tenant_id, self._default_cap)

    def spent(self, tenant_id: str, day: str) -> float:
        return self._spent.get((tenant_id, day), 0.0)

    def remaining(self, tenant_id: str, day: str) -> float:
        return max(0.0, self.cap_for(tenant_id) - self.spent(tenant_id, day))

    def check(self, tenant_id: str, amount: float, day: str) -> BudgetDecision:
        """Decide whether charging `amount` is allowed, WITHOUT recording it.

        Under cap -> ALLOW regardless of mode. Over cap -> the mode decides:
        OFF -> ALLOW, WARN -> WARN, BLOCK -> BLOCK. Fail-closed default is BLOCK.
        """
        projected = self.spent(tenant_id, day) + amount
        if projected <= self.cap_for(tenant_id):
            return BudgetDecision.ALLOW
        if self._mode is BudgetMode.OFF:
            return BudgetDecision.ALLOW
        if self._mode is BudgetMode.WARN:
            return BudgetDecision.WARN
        return BudgetDecision.BLOCK

    def record(self, tenant_id: str, amount: float, day: str) -> BudgetDecision:
        """Charge `amount` and return the decision. A BLOCK is NOT recorded (the
        spend never happened); ALLOW and WARN accumulate."""
        decision = self.check(tenant_id, amount, day)
        if decision is not BudgetDecision.BLOCK:
            key = (tenant_id, day)
            self._spent[key] = self._spent.get(key, 0.0) + amount
        return decision
