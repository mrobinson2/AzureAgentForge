"""Three-state circuit breakers for the model-router's upstream dispatch.

WHY THIS EXISTS
----------------
A router is the last place a bad upstream is cheap. Once a request leaves it,
every retry, every fallback hop, and every re-dispatch is either latency the
caller pays for or tokens the operator pays for. The two failure shapes that
cost the most are boringly ordinary:

  * a credential goes bad (rotated, revoked, expired) and every call to that
    endpoint 401s — while the router keeps trying, and keeps falling through
    to a *metered* fallback that works fine and bills fine;
  * an endpoint hits its quota (429) or stops answering at all, and the
    router's retry loop turns one caller's request into three upstream
    round-trips before it gives up.

Neither needs a human to diagnose. Both are countable, and both have the same
correct response: stop calling that upstream for a while. That is a circuit
breaker — and this module is the decision logic for one.

DESIGN CHOICES
---------------
* **Pure decision logic, stdlib only** (same shape as budget_enforcement.py
  and waste_breakers.py in this package). No sockets, no files, no HTTP. The
  one unavoidable impurity — a breaker is a *stateful* control and must know
  that a cooldown elapsed — is an injected `clock` callable, so every
  transition in this file is testable with plain data and a fake clock.
* **Fails CLOSED.** While a breaker is OPEN, the host must return a typed 503
  and must NOT invoke the upstream. The opposite default (fail open: serve
  traffic through a degraded credential) optimizes for availability; this
  router optimizes for *cost posture*, where a credential outage that
  silently falls through to metered inference turns an ops problem into an
  invoice. See docs/design/router-resilience-pack.md.
* **Narrow trip classification.** Only auth failures (401/403), quota
  exhaustion (429), and connection-level failures count. Model-content
  errors, empty or malformed responses, 400/404/413/422 client errors, and
  5xx application errors explicitly do NOT. Getting this wrong is the single
  way this feature causes an outage instead of preventing one: a breaker that
  counts "the model returned an empty string" will trip on perfectly healthy
  traffic that has nothing to do with the credential. Classification is an
  explicit allowlist, never a "looks bad" heuristic.
* **Keyed by credential identity, not tier name.** See `credential_key()`.
* **The host decides what to do.** This module returns verdicts and raises
  nothing. Refusing the request, shaping the 503, and recording the event are
  the host's job — the same "module returns a verdict, host enforces"
  convention the rest of this package uses.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

# ── States ────────────────────────────────────────────────────────────────────
STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"
ALL_STATES = (STATE_CLOSED, STATE_OPEN, STATE_HALF_OPEN)

# ── Trip reasons — stable strings, logged and surfaced in the flight trace ────
TRIP_AUTH_FAILURE = "auth_failure"
TRIP_QUOTA_EXHAUSTED = "quota_exhausted"
TRIP_CONNECTION_FAILURE = "connection_failure"
# A half-open probe that failed for a NON-tripping reason still has to resolve
# the probe (see the note on record_failure), so it gets its own reason string
# rather than being silently dropped.
TRIP_PROBE_FAILED = "half_open_probe_failed"

ALL_TRIP_REASONS = (
    TRIP_AUTH_FAILURE,
    TRIP_QUOTA_EXHAUSTED,
    TRIP_CONNECTION_FAILURE,
    TRIP_PROBE_FAILED,
)

# Machine-readable rejection code, same convention as budget_enforcement's
# budget_exceeded and waste_breakers' waste_breaker_tripped.
BREAKER_OPEN_ERROR_CODE = "UPSTREAM_BREAKER_OPEN"


# ── Trip taxonomy ─────────────────────────────────────────────────────────────
# An HTTP status the upstream actually returned is authoritative: if it is not
# in this table, the call does NOT count toward the breaker, full stop. That is
# what keeps a 400 (caller sent a bad body), a 404 (deployment name typo), a
# 413 (context overflow) and a 500 (provider application bug) from tripping a
# breaker that exists to detect credential and reachability problems.
TRIPPING_STATUS_CODES: dict[int, str] = {
    401: TRIP_AUTH_FAILURE,   # unauthenticated — key missing, wrong, or revoked
    403: TRIP_AUTH_FAILURE,   # authenticated but denied — key lacks the deployment
    429: TRIP_QUOTA_EXHAUSTED,  # rate/quota ceiling on this credential+endpoint
}

# Fallback signal when no status code is available (SDKs raise typed exceptions
# without one for transport-level problems). Matched on the exception's class
# NAME so this stays stdlib-only — the router must not import litellm/anthropic
# exception hierarchies into a pure decision module.
AUTH_EXCEPTION_NAMES = frozenset({
    "AuthenticationError",
    "PermissionDeniedError",
    "UnauthorizedError",
    "ForbiddenError",
})
QUOTA_EXCEPTION_NAMES = frozenset({
    "RateLimitError",
    "TooManyRequestsError",
    "QuotaExceededError",
})
CONNECTION_EXCEPTION_NAMES = frozenset({
    "APIConnectionError",
    "APITimeoutError",
    "ConnectError",
    "ConnectionError",
    "ConnectionRefusedError",
    "ConnectionResetError",
    "ConnectTimeout",
    "ReadTimeout",
    "RemoteProtocolError",
    "ServerDisconnectedError",
    "TimeoutError",
    "gaierror",
})

# Documented for the reader and asserted in tests: these are the shapes that
# must NEVER trip a breaker, however bad they look. Nothing consults this set
# at runtime — classification is a strict allowlist, so anything absent from
# the tables above already returns None. Keeping the exclusion list explicit is
# a statement of intent that a future edit has to argue with.
NON_TRIPPING_EXCEPTION_NAMES = frozenset({
    "BadRequestError",           # 400 — the caller's body was wrong
    "NotFoundError",             # 404 — deployment name doesn't exist
    "ContextWindowExceededError",  # 413 — prompt too big for the model
    "UnprocessableEntityError",  # 422 — schema/tool-definition problem
    "ContentPolicyViolationError",  # content filter — a model verdict
    "InternalServerError",       # 500 — provider application bug
    "JSONDecodeError",           # malformed response body
    "EmptyResponseError",        # healthy-but-empty completion
    "ValueError",
    "KeyError",
})

# Narrow text signal, used only when there is neither a status code nor a
# recognized exception class. Deliberately not a general "looks like auth"
# pattern: these are literal OAuth/provider error identifiers, not English.
_CREDENTIAL_TEXT = re.compile(r"invalid_grant|invalid_api_key|invalid_client", re.IGNORECASE)


def _status_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status from an SDK exception, without importing SDKs."""
    for attr in ("status_code", "http_status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, bool):
            continue
        if isinstance(val, int):
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)
    response = getattr(exc, "response", None)
    val = getattr(response, "status_code", None)
    if isinstance(val, int):
        return val
    return None


def classify_failure(
    exc: BaseException | None = None, *, status_code: int | None = None
) -> str | None:
    """Return a trip reason for this failure, or None if it must not count.

    Order matters. A status code the upstream actually returned wins outright,
    including when it says "don't count this" — an `AuthenticationError`
    subclass carrying a 404 is a routing mistake, not a credential problem.
    """
    code = status_code if status_code is not None else (
        _status_of(exc) if exc is not None else None
    )
    if code is not None:
        return TRIPPING_STATUS_CODES.get(code)

    if exc is None:
        return None

    name = type(exc).__name__
    if name in AUTH_EXCEPTION_NAMES:
        return TRIP_AUTH_FAILURE
    if name in QUOTA_EXCEPTION_NAMES:
        return TRIP_QUOTA_EXHAUSTED
    if name in CONNECTION_EXCEPTION_NAMES:
        return TRIP_CONNECTION_FAILURE
    if _CREDENTIAL_TEXT.search(str(exc) or ""):
        return TRIP_AUTH_FAILURE
    return None


# ── Config ────────────────────────────────────────────────────────────────────

def positive_int(raw: object, fallback: int) -> int:
    """Parse a positive int, falling back on anything else.

    Zero and negatives are rejected rather than accepted: a 0 failure
    threshold trips on the first request and a 0-probe half-open state can
    never admit a probe. Both are misconfigurations that would wedge the
    breaker permanently, not policies anyone means to express.
    """
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return val if val > 0 else fallback


def positive_float(raw: object, fallback: float) -> float:
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return val if val > 0 else fallback


@dataclass(frozen=True)
class BreakerConfig:
    """Tunable trip/reset policy. All env-configurable (see .env.example).

    `enabled` defaults True — unlike the waste breakers' observe-first
    posture, a circuit breaker is fail-safe by construction: it can only ever
    *stop* calls to an upstream that is already failing a narrow, explicit set
    of credential/reachability checks.
    """

    enabled: bool = True
    failure_threshold: int = 5
    cooldown_seconds: float = 60.0
    half_open_probes: int = 1


@dataclass(frozen=True)
class AdmitVerdict:
    """Result of asking a breaker whether one dispatch may proceed.

    `allowed=False` means the host MUST return a typed 503 without invoking
    the upstream. That is the entire point of the breaker.
    """

    allowed: bool
    state: str
    key: str
    failures: int = 0
    retry_after_seconds: float = 0.0


@dataclass
class _Entry:
    state: str = STATE_CLOSED
    failures: int = 0
    opened_at: float = 0.0
    half_open_since: float = 0.0
    probes_left: int = 0
    trips: int = 0
    last_trip_reason: str | None = None


# ── Breaker key ───────────────────────────────────────────────────────────────
# Per-process salt. Breaker state is in-process only, so the key never needs to
# be stable across restarts — which means it can be salted, which means the
# fingerprint is safe to print on the unauthenticated /health probe without
# disclosing an endpoint hostname or letting anyone confirm a guessed API key.
_KEY_SALT = secrets.token_bytes(16)


def credential_key(*, api_base: str | None, api_key: str | None) -> str:
    """Breaker key for one upstream credential identity.

    WHY CREDENTIAL AND NOT TIER NAME. In this router a "tier" is an entry in
    MODELS, and every entry carries its own `api_base` + `api_key` pair — the
    Foundry deployments are registered one per env-var prefix, each with its
    own project endpoint and key. Keying on that pair rather than on the tier
    string is the choice that matches how failures actually propagate here:

      * Every signal in the trip taxonomy belongs to the credential or its
        endpoint, not to a model's behavior. A revoked key 401s for every
        deployment it fronts; an unreachable host is unreachable for all of
        them.
      * Passthrough tiers are registered *ephemerally* — select_tier() writes
        `MODELS[<whatever model string the caller sent>]` on the fly. Keying
        by tier name would mint a brand-new breaker per distinct model string,
        so a dead project credential could 401 forever without any single
        breaker ever accumulating enough failures to trip.
      * Ollama edge tiers all share one base URL, so they correctly share one
        breaker: the edge host is up or down as a unit.

    The known cost of this choice is documented rather than hidden: Azure
    quota is per-deployment, so a 429 on one deployment can open the breaker
    for its project siblings too. In a fail-closed cost posture a 60-second
    pause on a sibling is the cheaper error than unbounded metered spend
    during a throttling incident.
    """
    material = f"{api_base or ''}\x00{api_key or ''}".encode("utf-8", "replace")
    digest = hmac.new(_KEY_SALT, material, hashlib.sha256).hexdigest()[:12]
    return f"cred:{digest}"


# ── Registry ──────────────────────────────────────────────────────────────────

class CircuitBreakerRegistry:
    """In-process, thread-safe collection of breakers keyed by credential.

    Transitions are pushed to an optional `on_transition` callback (the host
    uses it to log and to record the trip to the flight recorder) rather than
    written anywhere by this module.
    """

    def __init__(
        self,
        config: BreakerConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_transition: Callable[[dict], None] | None = None,
    ):
        self.config = config or BreakerConfig()
        self.clock = clock
        self.on_transition = on_transition
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    # -- internals -----------------------------------------------------------

    def _entry(self, key: str) -> _Entry:
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry()
            self._entries[key] = entry
        return entry

    def _apply_time(self, entry: _Entry) -> str | None:
        """Time-driven transitions. Returns the previous state if one fired.

        Two of them:
          1. OPEN -> HALF_OPEN once the cooldown elapses. This is the normal
             recovery path.
          2. HALF_OPEN with no probes left -> re-armed after another cooldown.
             This is the stall guard. A probe is admitted by decrementing the
             probe budget, and the outcome is reported afterwards; if a host
             ever admits a probe and then never reports (a bug, a crash, a
             cancelled request), without this the breaker sits half-open with
             zero probes forever and nothing can reopen or close it. Re-arming
             makes the worst case "one extra cooldown", not "wedged".
        """
        now = self.clock()
        if entry.state == STATE_OPEN:
            if now - entry.opened_at >= self.config.cooldown_seconds:
                entry.state = STATE_HALF_OPEN
                entry.probes_left = self.config.half_open_probes
                entry.half_open_since = now
                return STATE_OPEN
            return None
        if (
            entry.state == STATE_HALF_OPEN
            and entry.probes_left <= 0
            and now - entry.half_open_since >= self.config.cooldown_seconds
        ):
            entry.probes_left = self.config.half_open_probes
            entry.half_open_since = now
        return None

    def _emit(self, key: str, previous: str, entry: _Entry, reason: str | None) -> None:
        if self.on_transition is None or previous == entry.state:
            return
        try:
            self.on_transition({
                "key": key,
                "from_state": previous,
                "to_state": entry.state,
                "reason": reason,
                "failures": entry.failures,
                "trips": entry.trips,
                "failure_threshold": self.config.failure_threshold,
                "cooldown_seconds": self.config.cooldown_seconds,
            })
        except Exception:  # noqa: BLE001 — a telemetry sink must never break the breaker
            pass

    # -- public API ----------------------------------------------------------

    def admit(self, key: str) -> AdmitVerdict:
        """Ask permission for ONE dispatch. Call immediately before the call.

        A HALF_OPEN admission consumes a probe, so every admitted call must be
        followed by exactly one record_success/record_failure.
        """
        if not self.config.enabled:
            return AdmitVerdict(allowed=True, state=STATE_CLOSED, key=key)

        with self._lock:
            entry = self._entry(key)
            previous = entry.state
            self._apply_time(entry)
            emitted_from = previous if previous != entry.state else None

            if entry.state == STATE_OPEN:
                verdict = AdmitVerdict(
                    allowed=False,
                    state=STATE_OPEN,
                    key=key,
                    failures=entry.failures,
                    retry_after_seconds=max(
                        0.0,
                        self.config.cooldown_seconds - (self.clock() - entry.opened_at),
                    ),
                )
            elif entry.state == STATE_HALF_OPEN:
                if entry.probes_left > 0:
                    entry.probes_left -= 1
                    verdict = AdmitVerdict(
                        allowed=True, state=STATE_HALF_OPEN, key=key, failures=entry.failures,
                    )
                else:
                    # Another probe is already in flight. Refuse rather than
                    # queue: the point of half-open is exactly one test call.
                    verdict = AdmitVerdict(
                        allowed=False,
                        state=STATE_HALF_OPEN,
                        key=key,
                        failures=entry.failures,
                        retry_after_seconds=max(
                            0.0,
                            self.config.cooldown_seconds
                            - (self.clock() - entry.half_open_since),
                        ),
                    )
            else:
                verdict = AdmitVerdict(
                    allowed=True, state=STATE_CLOSED, key=key, failures=entry.failures,
                )
            snapshot_entry = _Entry(**vars(entry))

        if emitted_from is not None:
            self._emit(key, emitted_from, snapshot_entry, "cooldown_elapsed")
        return verdict

    def record_success(self, key: str) -> None:
        """A dispatch through this credential worked. Reset to CLOSED."""
        if not self.config.enabled:
            return
        with self._lock:
            entry = self._entry(key)
            previous = entry.state
            entry.state = STATE_CLOSED
            entry.failures = 0
            entry.opened_at = 0.0
            entry.half_open_since = 0.0
            entry.probes_left = 0
            snapshot_entry = _Entry(**vars(entry))
        self._emit(key, previous, snapshot_entry, "probe_succeeded")

    def record_failure(self, key: str, *, reason: str = TRIP_AUTH_FAILURE) -> None:
        """Count one breaker-eligible failure.

        Trips unconditionally on every call it receives — deciding WHETHER a
        given failure is eligible is the host's job (see classify_failure).
        The one deliberate exception is a HALF_OPEN probe: the host must
        report a failed probe here even when the failure reason is not
        trip-eligible (pass reason=TRIP_PROBE_FAILED), or a probe outcome the
        classifier ignores would leave the breaker half-open with no verdict.
        """
        if not self.config.enabled:
            return
        with self._lock:
            entry = self._entry(key)
            previous = entry.state
            self._apply_time(entry)
            now = self.clock()

            if entry.state == STATE_HALF_OPEN:
                # A failed probe re-opens immediately — no second chance, and
                # no accumulating toward the threshold again.
                entry.state = STATE_OPEN
                entry.opened_at = now
                entry.probes_left = 0
                entry.trips += 1
                entry.last_trip_reason = reason
            elif entry.state == STATE_OPEN:
                # Late arrival from a call admitted before the trip. Count it
                # for observability; do not extend the cooldown.
                entry.failures += 1
            else:
                entry.failures += 1
                if entry.failures >= self.config.failure_threshold:
                    entry.state = STATE_OPEN
                    entry.opened_at = now
                    entry.probes_left = 0
                    entry.trips += 1
                    entry.last_trip_reason = reason
            snapshot_entry = _Entry(**vars(entry))
        self._emit(key, previous, snapshot_entry, reason)

    def state(self, key: str) -> str:
        """Current state, with time-driven transitions applied. Read-mostly:
        it can move OPEN -> HALF_OPEN, but never consumes a probe."""
        if not self.config.enabled:
            return STATE_CLOSED
        with self._lock:
            entry = self._entry(key)
            self._apply_time(entry)
            return entry.state

    def is_open(self, key: str) -> bool:
        """True when this credential is currently refusing traffic outright.

        HALF_OPEN is deliberately NOT "open" here: it is the probing state, and
        a host asking "is this upstream shut off?" should get False so the
        probe can happen.
        """
        return self.state(key) == STATE_OPEN

    def snapshot(self, key: str) -> dict:
        if not self.config.enabled:
            return {"state": STATE_CLOSED, "enabled": False}
        with self._lock:
            entry = self._entry(key)
            self._apply_time(entry)
            return {
                "state": entry.state,
                "failures": entry.failures,
                "trips": entry.trips,
                "last_trip_reason": entry.last_trip_reason,
                "probes_left": entry.probes_left,
                "failure_threshold": self.config.failure_threshold,
                "cooldown_seconds": self.config.cooldown_seconds,
                "half_open_probes": self.config.half_open_probes,
                "enabled": True,
            }

    def snapshot_all(self) -> dict[str, dict]:
        with self._lock:
            keys = list(self._entries)
        return {key: self.snapshot(key) for key in keys}

    def open_keys(self) -> list[str]:
        return [key for key, snap in self.snapshot_all().items() if snap["state"] == STATE_OPEN]

    def reset(self, key: str | None = None) -> int:
        """Force breakers back to CLOSED. Operator action (a credential was
        fixed and waiting out the cooldown is pointless) and test hygiene.
        Returns how many entries were cleared."""
        with self._lock:
            if key is None:
                count = len(self._entries)
                self._entries.clear()
                return count
            return 1 if self._entries.pop(key, None) is not None else 0


# ── Host-facing error body ────────────────────────────────────────────────────

def open_detail(
    verdict: AdmitVerdict,
    *,
    tier: str,
    fail_closed_fallback: bool = False,
    primary_tier: str | None = None,
) -> dict:
    """Machine-readable 503 body — same shape family as
    budget_enforcement.block_detail() and waste_breakers.trip_detail(), so a
    caller that parses one can parse all three.

    `fail_closed_fallback` marks the cost-governance variant: the request's
    primary upstream is OPEN and the candidate here is a metered fallback, so
    the router refuses instead of letting a credential outage become spend.
    """
    if fail_closed_fallback:
        message = (
            f"upstream circuit breaker is open for tier {primary_tier!r}; refusing to "
            f"fall through to metered tier {tier!r}"
        )
    else:
        message = f"upstream circuit breaker is {verdict.state} for tier {tier!r}"
    return {
        "error": "upstream_breaker_open",
        "code": BREAKER_OPEN_ERROR_CODE,
        "message": message,
        "tier": tier,
        "primary_tier": primary_tier,
        "breaker_key": verdict.key,
        "breaker_state": verdict.state,
        "fail_closed": True,
        "retry_after_seconds": round(verdict.retry_after_seconds, 1),
        # Retryable, but only after the cooldown — the accompanying Retry-After
        # header carries the same number so a well-behaved client backs off
        # instead of turning a breaker into a retry storm.
        "retryable": True,
    }
