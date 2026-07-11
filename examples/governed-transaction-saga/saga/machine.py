"""Fold/apply state machine for one service transaction (an appliance-repair job).

The current state is never stored; it is *computed* by folding the event stream
through a pure ``apply`` function. Because ``apply`` is pure and total over its
declared edges, the same events always produce the same state — the property
that makes an event-sourced system auditable and replay-safe.

The legal life-cycle of a repair job::

    REQUESTED -> SCHEDULED -> DISPATCHED -> IN_PROGRESS -> COMPLETED
        \\___________\\____________\\_____________\\-------> CANCELLED

Any move off those edges raises :class:`IllegalTransition`. COMPLETED and
CANCELLED are terminal.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from typing import Iterable, Optional

from .events import Event


class Stage(enum.Enum):
    REQUESTED = "requested"
    SCHEDULED = "scheduled"
    DISPATCHED = "dispatched"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class IllegalTransition(Exception):
    """Raised when an event would traverse an edge the machine does not allow."""


# Which stage each event type drives the saga toward.
_EVENT_STAGE: dict[str, Stage] = {
    "job.requested": Stage.REQUESTED,
    "job.scheduled": Stage.SCHEDULED,
    "job.dispatched": Stage.DISPATCHED,
    "job.started": Stage.IN_PROGRESS,
    "job.completed": Stage.COMPLETED,
    "job.cancelled": Stage.CANCELLED,
}

# Legal outgoing edges. ``None`` is the pre-creation state.
_ALLOWED: dict[Optional[Stage], set[Stage]] = {
    None: {Stage.REQUESTED},
    Stage.REQUESTED: {Stage.SCHEDULED, Stage.CANCELLED},
    Stage.SCHEDULED: {Stage.DISPATCHED, Stage.CANCELLED},
    Stage.DISPATCHED: {Stage.IN_PROGRESS, Stage.CANCELLED},
    Stage.IN_PROGRESS: {Stage.COMPLETED, Stage.CANCELLED},
    Stage.COMPLETED: set(),
    Stage.CANCELLED: set(),
}

_TERMINAL = {Stage.COMPLETED, Stage.CANCELLED}


@dataclass(frozen=True)
class SagaState:
    """The derived state of one job. Immutable; ``apply`` returns a new one."""

    correlation_id: str
    tenant_id: str
    stage: Optional[Stage] = None
    history: tuple[Stage, ...] = ()

    @property
    def is_terminal(self) -> bool:
        return self.stage in _TERMINAL


def apply(state: Optional[SagaState], event: Event) -> SagaState:
    """Pure transition: ``(state, event) -> next state``.

    Raises :class:`IllegalTransition` if the event's target stage is not
    reachable from the current stage (including unknown event types).
    """
    target = _EVENT_STAGE.get(event.event_type)
    if target is None:
        raise IllegalTransition(f"unknown event type {event.event_type!r}")

    current = state.stage if state is not None else None
    if target not in _ALLOWED[current]:
        raise IllegalTransition(
            f"cannot move {current.value if current else 'START'} -> "
            f"{target.value} via {event.event_type!r}"
        )

    if state is None:
        state = SagaState(event.correlation_id, event.tenant_id)
    return replace(state, stage=target, history=state.history + (target,))


def fold(events: Iterable[Event]) -> Optional[SagaState]:
    """Reduce an event stream to its final state by folding ``apply`` over it.

    Returns ``None`` for an empty stream. Raises :class:`IllegalTransition` at
    the first event that violates the machine.
    """
    state: Optional[SagaState] = None
    for event in events:
        state = apply(state, event)
    return state
