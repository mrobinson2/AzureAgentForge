"""Governed transaction saga — an event-sourced governance core (example).

A compact, dependency-free distillation of the pattern AzureAgentForge uses to
make agent actions governable: an append-only event log, a fold/apply state
machine, complete-at-write receipts, and an audit walk that names gaps instead
of throwing. The worked scenario is a fictional appliance-repair job.
"""
from .audit import AuditReport, Finding, RECEIPTED_EVENTS, audit, narrate
from .events import Event, EventLog, new_id
from .machine import IllegalTransition, SagaState, Stage, apply, fold
from .receipts import IncompleteReceipt, Receipt, ReceiptStore

__all__ = [
    "Event",
    "EventLog",
    "new_id",
    "Stage",
    "SagaState",
    "IllegalTransition",
    "apply",
    "fold",
    "Receipt",
    "ReceiptStore",
    "IncompleteReceipt",
    "AuditReport",
    "Finding",
    "RECEIPTED_EVENTS",
    "audit",
    "narrate",
]
