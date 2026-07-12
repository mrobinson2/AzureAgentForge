"""Acting budget enforcement for per-tier daily budgets (AAF A6).

PORTABILITY CONTRACT: this module is deliberately self-contained — stdlib
only, no FastAPI, no LiteLLM, no host-specific imports. It is vendored
verbatim from the upstream private deployment's budget-enforcement module
(their M5 hardening enhancement; this port is AAF enhancement A6). The host
router owns tier config, spend ledgers, and HTTP; this module owns the
DECISION of what happens when a request lands on a tier that has exhausted
its daily budget.

Why it exists: per-tier daily budgets historically only ever WARNED —
observed evidence upstream is a Claude tier running ~25x its daily budget
with nothing but `budget_exceeded` WARN log lines (real money). This module
upgrades that from observability to an acting control, behind an env var
so the default remains exactly the old behavior.

Modes (env var BUDGET_ENFORCE_MODE, normalized by resolve_mode()):

  warn (default) — serve the requested tier anyway and emit a warning.
                   This is the pre-existing behavior (ship-dark safe):
                   budgets observed, never acted on.
  downgrade      — serve the configured fallback tier instead and mark the
                   response (DOWNGRADE_HEADER + host log line). If the
                   fallback tier is unavailable (not registered, or the
                   same tier that's over budget), degrade to warn — a
                   misconfigured fallback must never strand a request.
  block          — refuse the request; the host returns HTTP 429 with the
                   machine-readable body from block_detail().

The fallback tier is deliberately EXEMPT from enforcement: it is the
designated floor, and blocking it would turn "over budget" into a full
outage. (In this router the floor is gpt4o-mini, matching the pre-existing
gpt4o-mini→phi4 over-budget redirect's spirit of degrade-don't-die.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Container

# ── Modes ─────────────────────────────────────────────────────────────────────
MODE_WARN = "warn"
MODE_DOWNGRADE = "downgrade"
MODE_BLOCK = "block"
VALID_MODES = (MODE_WARN, MODE_DOWNGRADE, MODE_BLOCK)

# ── Decision actions ──────────────────────────────────────────────────────────
ACTION_ALLOW = "allow"          # under budget — serve as requested
ACTION_WARN = "warn"            # over budget — serve as requested, warn loudly
ACTION_DOWNGRADE = "downgrade"  # over budget — serve the fallback tier instead
ACTION_BLOCK = "block"          # over budget — refuse with 429

# Response header the host sets when a request was budget-downgraded, value
# "<requested-tier>-><served-tier>" (see downgrade_header()).
DOWNGRADE_HEADER = "X-Router-Budget-Downgrade"


def resolve_mode(raw: str | None) -> str:
    """Normalize a BUDGET_ENFORCE_MODE env value; unknown values -> warn.

    Fail-open on config typos: an invalid mode must not brick the router,
    and warn is the pre-existing (ship-dark) behavior.
    """
    mode = (raw or MODE_WARN).strip().lower()
    return mode if mode in VALID_MODES else MODE_WARN


@dataclass(frozen=True)
class BudgetDecision:
    """What the host router should do with one request. Pure data."""

    action: str              # one of the ACTION_* constants
    requested_tier: str      # the tier the request resolved to
    serve_tier: str | None   # tier to actually serve (None only for block)
    reason: str              # human-readable, log/error-body ready
    spent_usd: float = 0.0
    limit_usd: float = 0.0


def decide(
    *,
    tier: str,
    over_budget: bool,
    mode: str,
    fallback_tier: str | None,
    registered_tiers: Container[str],
    spent_usd: float = 0.0,
    limit_usd: float = 0.0,
) -> BudgetDecision:
    """The enforcement decision for serving `tier` right now.

    Pure function: the host supplies the over-budget verdict (its ledger),
    the mode, the configured fallback tier, and the set of registered tiers;
    this returns what to do. Never raises.
    """
    if not over_budget:
        return BudgetDecision(ACTION_ALLOW, tier, tier, "under budget",
                              spent_usd, limit_usd)

    over = (
        f"tier '{tier}' daily budget exhausted "
        f"(spent ${spent_usd:.4f} of ${limit_usd:.2f})"
    )

    if mode == MODE_BLOCK:
        return BudgetDecision(
            ACTION_BLOCK, tier, None,
            over + "; BUDGET_ENFORCE_MODE=block refused the request",
            spent_usd, limit_usd,
        )

    if mode == MODE_DOWNGRADE:
        if fallback_tier and fallback_tier != tier and fallback_tier in registered_tiers:
            return BudgetDecision(
                ACTION_DOWNGRADE, tier, fallback_tier,
                over + f"; downgrading to '{fallback_tier}'",
                spent_usd, limit_usd,
            )
        # Misconfigured / self-referential fallback: never strand the request.
        return BudgetDecision(
            ACTION_WARN, tier, tier,
            over + f"; downgrade fallback {fallback_tier!r} unavailable — "
                   "serving requested tier",
            spent_usd, limit_usd,
        )

    # MODE_WARN (and anything resolve_mode would have normalized to it).
    return BudgetDecision(
        ACTION_WARN, tier, tier,
        over + "; BUDGET_ENFORCE_MODE=warn serves it anyway",
        spent_usd, limit_usd,
    )


def downgrade_header(decision: BudgetDecision) -> tuple[str, str]:
    """(header-name, header-value) marking a budget downgrade on the response."""
    return DOWNGRADE_HEADER, f"{decision.requested_tier}->{decision.serve_tier}"


def block_detail(decision: BudgetDecision) -> dict:
    """Machine-readable 429 error body for a block decision."""
    return {
        "error": "budget_exceeded",
        "message": decision.reason,
        "tier": decision.requested_tier,
        "spent_usd": round(decision.spent_usd, 6),
        "limit_usd": round(decision.limit_usd, 6),
        "mode": MODE_BLOCK,
    }
