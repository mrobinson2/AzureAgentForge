"""Scoped paid-action kill switch for the model-router.

WHY THIS EXISTS
----------------
Circuit breakers are automatic and narrow — they react to a specific class of
upstream failure. Sometimes an operator knows something the router cannot
infer: a runaway agent is looping, a bill alert just fired, a prompt template
shipped with a bug that quadrupled context size. What they need in that
minute is not a diagnosis. It is a switch.

The naive version of that switch is a global "turn the router off", and it is
a bad idea. It converts a *cost* incident into an *availability* incident,
which is usually worse and always harder to explain. So this switch is
scoped: it stops the dispatches that spend money, and leaves the ones that
don't.

SCOPES
-------
* `paid_fallback` — block a FALLBACK dispatch to a metered model. The tier the
  caller originally selected still serves; only the router's own automatic
  recovery hop is stopped. This is the incident-shaped scope: fallbacks fire
  precisely when something upstream is already broken, which is exactly when
  spend runs away unattended.
* `all_paid` — block ALL dispatch to a metered model, primary included. Free
  local tiers (Ollama edge deployments — zero marginal cost per token) keep
  serving, as do /health, /v1/models, and the debug surface. The platform
  stays up; the meter stops.

DESIGN CHOICES
---------------
* **Pure decision logic, stdlib only** — same shape as budget_enforcement.py,
  waste_breakers.py and circuit_breaker.py. This module holds engagement
  state and answers "should this dispatch happen?"; the host reads env,
  raises the HTTP error, and records the event.
* **Default disengaged.** Unlike the breakers (fail-safe by construction), a
  kill switch is a deliberate operator action. Booting with a scope already
  engaged is possible (ROUTER_KILL_SWITCH_SCOPES) but it is never the default.
* **Never silently downgrade.** A blocked dispatch returns a typed error that
  names the scope. It does not quietly reroute to something cheaper — a
  stalled caller is loud and diagnosable; a silently rerouted one is not.
* **Events, not just state.** Engaging and releasing both emit an event to the
  host's sink (the flight recorder), so "who turned this on, when, and why"
  is answerable after the incident rather than during it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

# ── Scopes ────────────────────────────────────────────────────────────────────
SCOPE_PAID_FALLBACK = "paid_fallback"
SCOPE_ALL_PAID = "all_paid"
ALL_SCOPES = (SCOPE_PAID_FALLBACK, SCOPE_ALL_PAID)

# ── Actions ───────────────────────────────────────────────────────────────────
ACTION_ENGAGE = "engage"
ACTION_RELEASE = "release"
ALL_ACTIONS = (ACTION_ENGAGE, ACTION_RELEASE)

# Machine-readable rejection code, same convention as budget_enforcement's
# budget_exceeded and circuit_breaker's UPSTREAM_BREAKER_OPEN.
KILL_SWITCH_ERROR_CODE = "PAID_ACTIONS_DISABLED"

EVENT_ENGAGED = "kill_switch_engaged"
EVENT_RELEASED = "kill_switch_released"
EVENT_BLOCKED = "kill_switch_blocked"


def parse_scopes(raw: str | None) -> tuple[set[str], list[str]]:
    """Parse a comma-separated scope list into (recognized, unknown).

    Unknown names are returned rather than raising: a typo in an env var must
    not stop the router from booting, but it must not silently look like a
    working kill switch either — the host logs the unknown names loudly.
    """
    recognized: set[str] = set()
    unknown: list[str] = []
    for token in (raw or "").split(","):
        name = token.strip().lower()
        if not name:
            continue
        if name in ALL_SCOPES:
            recognized.add(name)
        else:
            unknown.append(name)
    return recognized, unknown


@dataclass(frozen=True)
class DispatchIntent:
    """One upstream dispatch the router is about to make.

    `metered` is the host's judgement about whether this dispatch spends real
    per-token money (in this router: everything except a local Ollama tier).
    `is_fallback` is False for the tier the caller's request originally
    resolved to and True for every automatic recovery hop after it.
    """

    tier: str
    metered: bool
    is_fallback: bool = False
    primary_tier: str | None = None


@dataclass(frozen=True)
class KillSwitchDecision:
    blocked: bool
    scope: str | None = None
    reason: str = ""


class KillSwitch:
    """Engagement state for the scoped paid-action switch."""

    def __init__(
        self,
        engaged: set[str] | tuple[str, ...] = (),
        *,
        on_event: Callable[[dict], None] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._engaged: set[str] = {s for s in engaged if s in ALL_SCOPES}
        self._boot_engaged: set[str] = set(self._engaged)
        self.on_event = on_event
        self.clock = clock
        self._lock = threading.Lock()
        self.blocked_counts: dict[str, int] = {scope: 0 for scope in ALL_SCOPES}

    # -- state ---------------------------------------------------------------

    def is_engaged(self, scope: str) -> bool:
        with self._lock:
            return scope in self._engaged

    def engaged_scopes(self) -> list[str]:
        with self._lock:
            return sorted(self._engaged)

    def _emit(self, event: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001 — a telemetry sink must never break the switch
            pass

    def _set(
        self, scope: str, *, engage: bool, actor: str | None, reason: str | None
    ) -> dict:
        if scope not in ALL_SCOPES:
            raise ValueError(f"unknown kill-switch scope: {scope!r}")
        with self._lock:
            was = scope in self._engaged
            if engage:
                self._engaged.add(scope)
            else:
                self._engaged.discard(scope)
            engaged_now = sorted(self._engaged)
        event = {
            "event": EVENT_ENGAGED if engage else EVENT_RELEASED,
            "scope": scope,
            "actor": actor,
            "reason": reason,
            "changed": was != engage,
            "engaged_scopes": engaged_now,
            "ts": self.clock(),
        }
        self._emit(event)
        return event

    def engage(self, scope: str, *, actor: str | None = None, reason: str | None = None) -> dict:
        return self._set(scope, engage=True, actor=actor, reason=reason)

    def release(self, scope: str, *, actor: str | None = None, reason: str | None = None) -> dict:
        return self._set(scope, engage=False, actor=actor, reason=reason)

    def reset(self) -> None:
        """Return to the boot-time posture (env default). Operator escape
        hatch and test hygiene."""
        with self._lock:
            self._engaged = set(self._boot_engaged)
            self.blocked_counts = {scope: 0 for scope in ALL_SCOPES}

    # -- decision ------------------------------------------------------------

    def evaluate(self, intent: DispatchIntent) -> KillSwitchDecision:
        """Should this dispatch be allowed? Counts blocks; changes nothing else."""
        if not intent.metered:
            # Free/local inference is never what a cost kill switch is for.
            # Blocking it would turn a spend control into an outage for the
            # one path that costs nothing.
            return KillSwitchDecision(blocked=False, reason="tier is not metered")

        with self._lock:
            all_paid = SCOPE_ALL_PAID in self._engaged
            paid_fallback = SCOPE_PAID_FALLBACK in self._engaged

        if all_paid:
            scope = SCOPE_ALL_PAID
            reason = f"all metered dispatch is halted; tier {intent.tier!r} is metered"
        elif paid_fallback and intent.is_fallback:
            scope = SCOPE_PAID_FALLBACK
            reason = (
                f"metered fallback dispatch is halted; refusing to fall back from "
                f"{intent.primary_tier!r} to metered tier {intent.tier!r}"
            )
        else:
            return KillSwitchDecision(blocked=False, reason="no engaged scope applies")

        with self._lock:
            self.blocked_counts[scope] = self.blocked_counts.get(scope, 0) + 1
        return KillSwitchDecision(blocked=True, scope=scope, reason=reason)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "scopes": {scope: scope in self._engaged for scope in ALL_SCOPES},
                "engaged": sorted(self._engaged),
                "boot_engaged": sorted(self._boot_engaged),
                # Trip counters, not spend figures: they answer "did the
                # operator's switch actually fire during the incident", not
                # "how much did it save".
                "blocked_counts": dict(self.blocked_counts),
            }


def block_detail(decision: KillSwitchDecision, intent: DispatchIntent) -> dict:
    """Machine-readable 503 body naming the engaged scope. Same shape family
    as budget_enforcement.block_detail() and circuit_breaker.open_detail()."""
    return {
        "error": "paid_actions_disabled",
        "code": KILL_SWITCH_ERROR_CODE,
        "message": decision.reason,
        "kill_switch_scope": decision.scope,
        "tier": intent.tier,
        "primary_tier": intent.primary_tier,
        "is_fallback": intent.is_fallback,
        # Not retryable: only an operator releasing the scope changes this
        # answer, so a client backing off and retrying accomplishes nothing.
        "retryable": False,
    }
