"""Append-only event log — the substrate the whole saga is built on.

Every meaningful change in a service transaction is recorded as an immutable
:class:`Event`. Nothing mutates state directly; state is *derived* by folding
the event stream (see ``machine.py``). Three ids travel with every event so an
auditor can reconstruct not just *what* happened but *why*:

* ``tenant_id``       — which tenant the fact belongs to (isolation boundary).
* ``correlation_id``  — which saga instance (one repair job) it belongs to.
* ``causation_id``    — the id of the event that caused this one (cause/effect).

The log is **append-only** and **idempotent by event id**: appending the same
event twice is a no-op. That is what makes replay and at-least-once delivery
safe — a redelivered message can never double-apply.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional


def new_id(prefix: str = "evt") -> str:
    """A short, collision-resistant id. Fictional/local — no external service."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Event:
    """An immutable fact. Once appended to the log it never changes."""

    event_type: str
    tenant_id: str
    correlation_id: str
    payload: dict = field(default_factory=dict)
    causation_id: Optional[str] = None
    event_id: str = field(default_factory=lambda: new_id("evt"))
    occurred_at: str = field(default_factory=_utcnow)


class EventLog:
    """An ordered, append-only, idempotent collection of events."""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._ids: set[str] = set()

    def append(self, event: Event) -> bool:
        """Append ``event``. Idempotent by ``event_id``.

        Returns ``True`` if the event was newly stored, ``False`` if it was a
        duplicate and therefore ignored. The append order is the log's source
        of chronological truth (it never depends on wall-clock timestamps).
        """
        if event.event_id in self._ids:
            return False
        self._ids.add(event.event_id)
        self._events.append(event)
        return True

    def extend(self, events: Iterator[Event]) -> int:
        """Append many events; return how many were newly stored."""
        return sum(1 for e in events if self.append(e))

    def for_correlation(
        self, correlation_id: str, tenant_id: Optional[str] = None
    ) -> list[Event]:
        """All events for one saga instance, optionally scoped to a tenant.

        Passing ``tenant_id`` enforces the isolation boundary: even if two
        tenants somehow shared a correlation id, you only ever see your own.
        """
        out = [e for e in self._events if e.correlation_id == correlation_id]
        if tenant_id is not None:
            out = [e for e in out if e.tenant_id == tenant_id]
        return out

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)
