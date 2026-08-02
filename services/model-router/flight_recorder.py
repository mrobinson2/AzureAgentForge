"""Bounded, in-process flight recorder for the model-router.

WHY THIS EXISTS
----------------
An agent platform's most expensive failures are rarely a single bad call —
they're a *pattern* of calls: a retry loop, a prompt that quietly doubled in
size, a caller hammering the same request. By the time that pattern shows up
on a bill, the calls themselves are long gone. There is nothing left to
inspect, only a total.

A flight recorder closes that gap the same way an aircraft's does: it keeps
a short, bounded, replayable window of exactly what happened per call —
who called, what was requested, what was served, how long it took, what it
cost, and whether a waste breaker (see waste_breakers.py) would have
flagged it — so an operator debugging a spend spike or a runaway agent has
something to look at other than a dollar figure.

DESIGN CHOICES
---------------
* **Bounded by construction, not by discipline.** The primary store is a
  fixed-size ring buffer (`collections.deque(maxlen=...)`); the oldest event
  is evicted automatically the moment a new one is recorded. There is no
  code path that lets this grow without limit. Optional on-disk persistence
  (FLIGHT_RECORDER_JSONL_PATH) is size-capped with single-generation
  rotation for the same reason.
* **Redacted by default.** With FLIGHT_RECORDER_REDACT=true (the default),
  prompt and response TEXT is never stored — only a truncated hash
  (`prompt_fingerprint`), token counts, and message counts. That is enough
  to detect "this caller sent the same prompt five times" without the
  recorder ever holding what was said. Turning redaction off is a
  local-debugging escape hatch, not a production posture; excerpts are
  still hard-capped in length even then.
* **Never blocks a request.** Recording a call must not be able to fail the
  call it is recording. Every write path catches its own exceptions, counts
  them, and logs loudly — an accounting gap must be visible, never silent.
* **Zero overhead when disabled.** When FLIGHT_RECORDER_ENABLED=false the
  host never constructs a FlightRecorder at all; every call site checks for
  `None` first. No hashing, no locking, no allocation happens on that path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("router.flight_recorder")

OUTCOME_SUCCESS = "success"
OUTCOME_ERROR = "error"
OUTCOME_DOWNGRADED = "downgraded"
VALID_OUTCOMES = frozenset({OUTCOME_SUCCESS, OUTCOME_ERROR, OUTCOME_DOWNGRADED})

# Hard cap on stored excerpts even with redaction OFF — a debugging aid must
# still be bounded, not an unredacted prompt dump.
_MAX_EXCERPT_CHARS = 300


def prompt_fingerprint(messages: list[dict] | None, tools: list[dict] | None = None) -> str:
    """Stable short hash of request SHAPE and content, for repeat detection.

    Hashing (never storing) is what lets the repeated-identical-calls breaker
    detect "this caller sent the same prompt N times" without the recorder
    holding prompt text anywhere. Truncated to 16 hex chars: enough to
    distinguish calls within one demo run, too short to be useful for
    anything else.
    """
    h = hashlib.sha256()
    for m in messages or []:
        h.update(str(m.get("role", "")).encode("utf-8", "replace"))
        content = m.get("content")
        if isinstance(content, str):
            h.update(content.encode("utf-8", "replace"))
        else:
            h.update(repr(content).encode("utf-8", "replace"))
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        h.update(str(fn.get("name", "")).encode("utf-8", "replace"))
    return h.hexdigest()[:16]


class FlightRecorder:
    """Bounded, append-only, in-memory (plus optional on-disk) call trace."""

    def __init__(
        self,
        *,
        max_events: int = 500,
        redact: bool = True,
        jsonl_path: str | None = None,
        jsonl_max_bytes: int = 10_000_000,
    ):
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        self.max_events = max_events
        self.redact = redact
        self.jsonl_path = jsonl_path or None
        self.jsonl_max_bytes = max(1, jsonl_max_bytes)

        self._buffer: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._lock = threading.Lock()

        # Observability for the recorder itself — a telemetry subsystem that
        # fails silently produces confident-looking traces with holes in them.
        self.writes = 0
        self.write_failures = 0
        self.last_error: str | None = None

        if self.jsonl_path:
            parent = os.path.dirname(os.path.abspath(self.jsonl_path))
            if parent:
                os.makedirs(parent, exist_ok=True)

    # ── write path ─────────────────────────────────────────────────────────

    def record(self, **fields: Any) -> str | None:
        """Append one call-level event. Returns its event_id, or None.

        Raises nothing: a telemetry write must never fail a request that
        already succeeded or failed upstream. Failures increment
        `write_failures` and set `last_error` so the gap is visible.
        """
        try:
            return self._record_unsafe(**fields)
        except Exception as exc:  # noqa: BLE001 — telemetry is not the request
            self.write_failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.error(
                "flight_recorder_write_failed (%s total failures): %s",
                self.write_failures, exc,
            )
            return None

    def _record_unsafe(self, **fields: Any) -> str:
        outcome = fields.get("outcome") or OUTCOME_SUCCESS
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"invalid outcome: {outcome!r}")

        event = dict(fields)
        event["event_id"] = fields.get("event_id") or uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        event["ts"] = now.isoformat()
        # Monotonic-ish epoch seconds for cheap window queries (count_recent /
        # consecutive_failures) — kept separate from the human-readable "ts"
        # so a formatting change to one never breaks the other.
        event["_epoch"] = time.time()
        event["outcome"] = outcome
        event["redacted"] = self.redact

        in_tok = int(event.get("input_tokens") or 0)
        out_tok = int(event.get("output_tokens") or 0)
        event.setdefault("total_tokens", in_tok + out_tok)
        event["cost_usd"] = round(float(event.get("cost_usd") or 0.0), 6)

        # Redaction is enforced HERE, unconditionally — even if a caller
        # upstream passes raw excerpt text, redact=True guarantees it never
        # persists. This is what makes "safe to leave on" a real guarantee
        # rather than a convention callers have to honor themselves.
        if self.redact:
            event.pop("prompt_excerpt", None)
            event.pop("response_excerpt", None)
        else:
            for key in ("prompt_excerpt", "response_excerpt"):
                val = event.get(key)
                if isinstance(val, str) and len(val) > _MAX_EXCERPT_CHARS:
                    event[key] = val[:_MAX_EXCERPT_CHARS] + "…"

        with self._lock:
            self._buffer.append(event)
            self.writes += 1
            if self.jsonl_path:
                self._append_jsonl(event)

        return event["event_id"]

    def _append_jsonl(self, event: dict[str, Any]) -> None:
        """Best-effort on-disk persistence. Never raises past this method —
        disk trouble must not affect the in-memory ring buffer or the request
        it's recording."""
        try:
            line = json.dumps(event, default=str) + "\n"
            if (
                os.path.exists(self.jsonl_path)
                and os.path.getsize(self.jsonl_path) + len(line) > self.jsonl_max_bytes
            ):
                self._rotate()
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as exc:
            log.error("flight_recorder_jsonl_write_failed path=%s error=%s", self.jsonl_path, exc)

    def _rotate(self) -> None:
        """Single-generation rotation: current file becomes `<path>.1`,
        overwriting any previous backup. Bounds on-disk size to roughly
        2x jsonl_max_bytes — the same 'never grows unbounded' guarantee as
        the ring buffer, just with a slightly looser bound."""
        backup = self.jsonl_path + ".1"
        try:
            os.replace(self.jsonl_path, backup)
        except OSError as exc:
            log.error("flight_recorder_jsonl_rotate_failed path=%s error=%s", self.jsonl_path, exc)

    # ── read path ─────────────────────────────────────────────────────────

    def recent(self, *, limit: int = 50, caller: str | None = None) -> list[dict]:
        """Most-recent-first slice of the ring buffer, optionally filtered to
        one caller. Read-only; never touches disk."""
        with self._lock:
            items = list(self._buffer)
        if caller is not None:
            items = [e for e in items if e.get("caller") == caller]
        items.reverse()
        return items[: max(1, limit)]

    def get(self, event_id: str) -> dict | None:
        with self._lock:
            for e in reversed(self._buffer):
                if e.get("event_id") == event_id:
                    return dict(e)
        return None

    def count_recent(
        self, *, caller: str | None = None, fingerprint: str | None = None, seconds: int = 60
    ) -> int:
        """Count buffered events matching caller/fingerprint within the last
        `seconds`. Used by waste_breakers to build a CallerObservation."""
        cutoff = time.time() - max(0, seconds)
        with self._lock:
            items = list(self._buffer)
        count = 0
        for e in items:
            if caller is not None and e.get("caller") != caller:
                continue
            if fingerprint is not None and e.get("prompt_fingerprint") != fingerprint:
                continue
            if (e.get("_epoch") or 0) >= cutoff:
                count += 1
        return count

    def consecutive_failures(self, *, caller: str | None = None) -> int:
        """Trailing run of error-outcome events for `caller`, most-recent
        first, stopping at the first non-error. A caller with no history
        (or an unattributed one) reports 0."""
        with self._lock:
            items = list(self._buffer)
        if caller is not None:
            items = [e for e in items if e.get("caller") == caller]
        count = 0
        for e in reversed(items):
            if e.get("outcome") == OUTCOME_ERROR:
                count += 1
            else:
                break
        return count

    def stats(self) -> dict:
        with self._lock:
            size = len(self._buffer)
        return {
            "redact": self.redact,
            "buffer_size": size,
            "max_events": self.max_events,
            "writes": self.writes,
            "write_failures": self.write_failures,
            "last_error": self.last_error,
            "jsonl_path": self.jsonl_path,
            "jsonl_max_bytes": self.jsonl_max_bytes if self.jsonl_path else None,
        }
