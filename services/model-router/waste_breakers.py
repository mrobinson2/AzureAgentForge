"""Waste-breaker decision logic for the model-router (Flight Recorder pack).

WHY THIS EXISTS
----------------
An LLM router sits in the one place that can see every call an agent makes
before it is billed. Left alone, three cheap mistakes turn into real money:

  * a bug that resends the exact same prompt in a loop,
  * a caller that retries far faster than the failure it is retrying,
  * a caller that keeps stuffing more and more context into one request
    until it is paying full price for tokens the model barely uses.

None of these need a human to notice the pattern — they are countable. This
module turns "countable" into a verdict: given a small set of observed
counters for one caller, it says which waste patterns look tripped, and by
how much, without ever touching the request itself.

DESIGN CHOICES
---------------
* **Pure decision logic, stdlib only** (mirrors budget_enforcement.py in this
  same package). This module never reads a clock, a socket, or a file — the
  host (main.py) supplies the observed counts (typically read from the
  Flight Recorder's ring buffer) and this module returns a verdict. That
  makes every threshold testable with plain data, no mocking required.
* **Observe by default.** WASTE_BREAKER_ENFORCE_MODE defaults to "observe":
  every request is evaluated and tripped breakers are logged and recorded to
  the flight trace, but nothing is refused. Flipping to "block" is a
  one-line env change once the operator trusts the thresholds for their
  traffic — the same graduated posture budget_enforcement.py uses for
  spend controls (warn -> downgrade -> block).
* **Refuse, never reroute.** The only enforcement action this module can
  produce is "refuse this request" (HTTP 429). It never reassigns, retries
  on the caller's behalf, or silently swaps the model — a stalled caller is
  loud and safe; a silently rerouted one is not.
* **No prompt content.** Verdicts are built from counts and token estimates
  only. The identical-call breaker works off a prompt *fingerprint* (a
  truncated hash — see flight_recorder.prompt_fingerprint), never the
  prompt text.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Breaker identifiers — stable strings, logged and returned to callers ──────
REPEATED_IDENTICAL_CALLS = "repeated_identical_calls"
RETRY_STORM = "retry_storm"
OVERSIZED_PROMPT = "oversized_prompt"
CONSECUTIVE_FAILURES = "consecutive_failures"

ALL_BREAKERS = (
    REPEATED_IDENTICAL_CALLS,
    RETRY_STORM,
    OVERSIZED_PROMPT,
    CONSECUTIVE_FAILURES,
)

# ── Enforcement modes ──────────────────────────────────────────────────────────
MODE_OBSERVE = "observe"
MODE_BLOCK = "block"
VALID_MODES = (MODE_OBSERVE, MODE_BLOCK)


def resolve_mode(raw: str | None) -> str:
    """Normalize WASTE_BREAKER_ENFORCE_MODE; unknown values fail open to observe.

    A config typo must never turn an observability feature into an outage.
    """
    mode = (raw or MODE_OBSERVE).strip().lower()
    return mode if mode in VALID_MODES else MODE_OBSERVE


@dataclass(frozen=True)
class BreakerThresholds:
    """Tunable trip points. All are env-configurable (see .env.example)."""

    repeated_identical_calls: int = 5
    repeated_identical_window_seconds: int = 60
    retry_storm_calls: int = 20
    retry_storm_window_seconds: int = 60
    oversized_prompt_tokens: int = 100_000
    consecutive_failures: int = 4


@dataclass(frozen=True)
class CallerObservation:
    """What the host observed for the caller making the current request.

    Every field here is something the Flight Recorder's ring buffer can
    actually answer (a bounded scan over recent events) or the router
    already computes per-request (the token estimate) — this struct
    deliberately cannot express a signal the platform doesn't measure.
    """

    caller: str | None = None
    # Calls from this caller (any prompt) within the retry-storm window.
    calls_in_window: int = 0
    # Calls from this caller with the SAME prompt fingerprint within the
    # repeated-identical-calls window.
    identical_calls_in_window: int = 0
    # Trailing run of error outcomes for this caller, most-recent first.
    consecutive_failures: int = 0
    # Token estimate for the CURRENT request (not historical).
    estimated_prompt_tokens: int = 0


@dataclass(frozen=True)
class BreakerVerdict:
    breaker: str
    tripped: bool
    observed: float
    threshold: float
    detail: str

    def as_dict(self) -> dict:
        return {
            "breaker": self.breaker,
            "tripped": self.tripped,
            "observed": self.observed,
            "threshold": self.threshold,
            "detail": self.detail,
        }


def _verdict(breaker: str, observed: float, threshold: float, detail: str) -> BreakerVerdict:
    return BreakerVerdict(
        breaker=breaker,
        tripped=observed >= threshold and threshold > 0,
        observed=observed,
        threshold=threshold,
        detail=detail,
    )


def evaluate(
    obs: CallerObservation, thresholds: BreakerThresholds | None = None
) -> list[BreakerVerdict]:
    """Compute every breaker dimension for one request. Enforces nothing.

    Always returns one verdict per breaker (including non-tripped ones) so a
    trace can show what was checked, not only what fired.
    """
    t = thresholds or BreakerThresholds()
    return [
        _verdict(
            REPEATED_IDENTICAL_CALLS,
            obs.identical_calls_in_window,
            t.repeated_identical_calls,
            f"same prompt fingerprint seen {obs.identical_calls_in_window} time(s) "
            f"in the last {t.repeated_identical_window_seconds}s "
            f"(threshold {t.repeated_identical_calls})",
        ),
        _verdict(
            RETRY_STORM,
            obs.calls_in_window,
            t.retry_storm_calls,
            f"{obs.calls_in_window} call(s) from this caller in the last "
            f"{t.retry_storm_window_seconds}s (threshold {t.retry_storm_calls})",
        ),
        _verdict(
            OVERSIZED_PROMPT,
            obs.estimated_prompt_tokens,
            t.oversized_prompt_tokens,
            f"request prompt estimated at {obs.estimated_prompt_tokens} tokens "
            f"(threshold {t.oversized_prompt_tokens})",
        ),
        _verdict(
            CONSECUTIVE_FAILURES,
            obs.consecutive_failures,
            t.consecutive_failures,
            f"{obs.consecutive_failures} consecutive failed call(s) from this "
            f"caller (threshold {t.consecutive_failures})",
        ),
    ]


def tripped_only(verdicts: list[BreakerVerdict]) -> list[BreakerVerdict]:
    return [v for v in verdicts if v.tripped]


def trip_detail(verdict: BreakerVerdict) -> dict:
    """Machine-readable 429 body for a blocked request — same shape family as
    budget_enforcement.block_detail() so callers parsing one can parse the
    other."""
    return {
        "error": "waste_breaker_tripped",
        "message": verdict.detail,
        "breaker": verdict.breaker,
        "observed": verdict.observed,
        "threshold": verdict.threshold,
        "mode": MODE_BLOCK,
    }
