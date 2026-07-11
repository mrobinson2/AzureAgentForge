"""The audit walk: a chronological narrative plus a receipt-gap report.

The auditor *describes* history; it never re-enforces the rules. So an illegal
transition is written into the narrative as a line, and a missing receipt is
returned as a named :class:`Finding` — never raised. A report that raises on
the first problem cannot enumerate all the problems, and enumeration is the
whole point of an audit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .events import Event
from .machine import IllegalTransition, SagaState, apply
from .receipts import ReceiptStore

# Policy: which event types MUST carry a receipt, and the evidence it must hold.
# (The receipt independently records its own required fields; this is the
# auditor's copy of the policy, so a gap is caught even if no receipt exists.)
RECEIPTED_EVENTS: dict[str, tuple[str, ...]] = {
    "job.dispatched": ("technician", "eta"),
    "job.completed": ("technician", "work_summary", "customer_signature"),
}


@dataclass(frozen=True)
class Finding:
    """A named audit problem. Data, not an exception."""

    kind: str  # e.g. "MISSING_RECEIPT", "INCOMPLETE_RECEIPT", "ILLEGAL_TRANSITION"
    event_id: str
    event_type: str
    detail: str


@dataclass
class AuditReport:
    correlation_id: str
    narrative: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings


def narrate(events: Iterable[Event]) -> list[str]:
    """Return a human-readable chronological narrative of the saga.

    Walks events in order, re-deriving the stage at each step. An illegal
    transition is recorded as a ``!!`` line rather than raised — the audit
    reports what happened, warts and all.
    """
    lines: list[str] = []
    state: Optional[SagaState] = None
    for i, event in enumerate(events, 1):
        try:
            state = apply(state, event)
            lines.append(
                f"{i:>2}. [{event.occurred_at}] {event.event_type} -> {state.stage.value}"
            )
        except IllegalTransition as exc:
            lines.append(f"{i:>2}. [{event.occurred_at}] {event.event_type} !! {exc}")
    return lines


def audit(events: Iterable[Event], receipts: ReceiptStore) -> AuditReport:
    """Produce the full audit: narrative + receipt-gap findings.

    A required receipt that is absent becomes a ``MISSING_RECEIPT`` finding; a
    present-but-incomplete one becomes ``INCOMPLETE_RECEIPT``. Neither raises.
    """
    events = list(events)
    correlation_id = events[0].correlation_id if events else ""
    report = AuditReport(correlation_id=correlation_id, narrative=narrate(events))

    # Detect illegal transitions as findings too (narrative shows them inline).
    state: Optional[SagaState] = None
    for event in events:
        try:
            state = apply(state, event)
        except IllegalTransition as exc:
            report.findings.append(
                Finding("ILLEGAL_TRANSITION", event.event_id, event.event_type, str(exc))
            )

    for event in events:
        required = RECEIPTED_EVENTS.get(event.event_type)
        if required is None:
            continue
        receipt = receipts.get(event.event_id)
        if receipt is None:
            report.findings.append(
                Finding(
                    "MISSING_RECEIPT",
                    event.event_id,
                    event.event_type,
                    f"{event.event_type} requires a receipt; none on file",
                )
            )
        elif not receipt.is_complete:
            report.findings.append(
                Finding(
                    "INCOMPLETE_RECEIPT",
                    event.event_id,
                    event.event_type,
                    f"receipt missing {', '.join(receipt.missing_fields())}",
                )
            )
    return report
