"""Complete-at-write receipts.

A *receipt* is the durable proof that a governed action really happened with
all of its required evidence — the technician who was dispatched, the work
summary and customer signature captured at completion. The governing rule is
**complete-at-write**: the store refuses to persist a receipt that is missing
any field it claims to require. There is no such thing as a half-written
receipt sitting in the store waiting to be finished later. Either the evidence
exists at the moment of writing, or nothing is written.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Optional

from .events import new_id


class IncompleteReceipt(Exception):
    """Raised at write time when a receipt is missing one or more required fields."""


@dataclass(frozen=True)
class Receipt:
    """Proof attached to a specific event. Records the fields it requires."""

    receipt_type: str
    event_id: str  # the event this receipt attests to
    tenant_id: str
    required_fields: tuple[str, ...] = ()
    fields: Mapping[str, object] = field(default_factory=dict)
    receipt_id: str = field(default_factory=lambda: new_id("rcpt"))
    issued_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def missing_fields(self) -> list[str]:
        """Required fields that are absent or empty (``None`` / ``""``)."""
        return [
            name
            for name in self.required_fields
            if name not in self.fields or self.fields[name] in (None, "")
        ]

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields()


class ReceiptStore:
    """Keyed by the event id a receipt attests to; refuses incomplete writes."""

    def __init__(self) -> None:
        self._by_event: dict[str, Receipt] = {}

    def put(self, receipt: Receipt) -> Receipt:
        """Persist ``receipt`` — or refuse it.

        Raises :class:`IncompleteReceipt` (before any state is written) if the
        receipt is missing required fields. This is the complete-at-write gate.
        """
        missing = receipt.missing_fields()
        if missing:
            raise IncompleteReceipt(
                f"receipt {receipt.receipt_type!r} for event {receipt.event_id} "
                f"missing required field(s): {', '.join(missing)}"
            )
        self._by_event[receipt.event_id] = receipt
        return receipt

    def get(self, event_id: str) -> Optional[Receipt]:
        return self._by_event.get(event_id)

    def has(self, event_id: str) -> bool:
        return event_id in self._by_event

    def __len__(self) -> int:
        return len(self._by_event)
