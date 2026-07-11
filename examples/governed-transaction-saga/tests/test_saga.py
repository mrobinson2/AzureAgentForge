"""Tests for the governed-transaction-saga example. Pure stdlib + pytest."""
import pytest

from saga import (
    AuditReport,
    Event,
    EventLog,
    IllegalTransition,
    IncompleteReceipt,
    Receipt,
    ReceiptStore,
    Stage,
    apply,
    audit,
    fold,
    narrate,
    new_id,
)

TENANT = "tenant_acme"
CORR = "job_0001"


def _event(event_type, causation_id=None, **payload):
    return Event(
        event_type=event_type,
        tenant_id=TENANT,
        correlation_id=CORR,
        causation_id=causation_id,
        payload=payload,
    )


def _happy_events():
    """A full, legal life-cycle: requested -> ... -> completed, causation-linked."""
    e1 = _event("job.requested", appliance="dishwasher")
    e2 = _event("job.scheduled", causation_id=e1.event_id, window="Tue AM")
    e3 = _event("job.dispatched", causation_id=e2.event_id, technician="Dana")
    e4 = _event("job.started", causation_id=e3.event_id)
    e5 = _event("job.completed", causation_id=e4.event_id)
    return [e1, e2, e3, e4, e5]


def _complete_store(events):
    """A receipt store satisfying the audit policy for the happy path."""
    store = ReceiptStore()
    by_type = {e.event_type: e for e in events}
    store.put(
        Receipt(
            "dispatch",
            by_type["job.dispatched"].event_id,
            TENANT,
            ("technician", "eta"),
            {"technician": "Dana", "eta": "09:30"},
        )
    )
    store.put(
        Receipt(
            "completion",
            by_type["job.completed"].event_id,
            TENANT,
            ("technician", "work_summary", "customer_signature"),
            {
                "technician": "Dana",
                "work_summary": "Replaced inlet valve",
                "customer_signature": "R. Kim",
            },
        )
    )
    return store


# --- event log -------------------------------------------------------------


def test_happy_path_folds_to_completed():
    state = fold(_happy_events())
    assert state.stage is Stage.COMPLETED
    assert state.is_terminal
    assert state.history[0] is Stage.REQUESTED
    assert state.history[-1] is Stage.COMPLETED


def test_duplicate_event_is_idempotent_noop():
    log = EventLog()
    e = _event("job.requested")
    assert log.append(e) is True
    assert log.append(e) is False  # same event id -> ignored
    assert len(log) == 1


def test_for_correlation_scopes_by_tenant():
    log = EventLog()
    mine = _event("job.requested")
    theirs = Event("job.requested", "tenant_other", CORR)
    log.append(mine)
    log.append(theirs)
    scoped = log.for_correlation(CORR, tenant_id=TENANT)
    assert scoped == [mine]  # the other tenant's event is invisible


def test_causation_chain_preserved():
    events = _happy_events()
    for parent, child in zip(events, events[1:]):
        assert child.causation_id == parent.event_id


# --- state machine ---------------------------------------------------------


def test_illegal_transition_raises():
    e1 = _event("job.requested")
    e_bad = _event("job.completed")  # can't jump requested -> completed
    with pytest.raises(IllegalTransition):
        fold([e1, e_bad])


def test_unknown_event_type_is_illegal():
    with pytest.raises(IllegalTransition):
        apply(None, _event("job.teleported"))


def test_cancelled_is_terminal_no_further_moves():
    e1 = _event("job.requested")
    e2 = _event("job.cancelled")
    state = fold([e1, e2])
    assert state.stage is Stage.CANCELLED
    with pytest.raises(IllegalTransition):
        apply(state, _event("job.scheduled"))


def test_fold_empty_stream_is_none():
    assert fold([]) is None


# --- receipts (complete-at-write) ------------------------------------------


def test_incomplete_receipt_refused_at_write():
    store = ReceiptStore()
    r = Receipt(
        "completion",
        new_id("evt"),
        TENANT,
        ("technician", "work_summary", "customer_signature"),
        {"technician": "Dana"},  # missing two required fields
    )
    with pytest.raises(IncompleteReceipt):
        store.put(r)
    assert len(store) == 0  # nothing partial was written


def test_complete_receipt_is_accepted_and_stored():
    store = ReceiptStore()
    eid = new_id("evt")
    r = Receipt("dispatch", eid, TENANT, ("technician", "eta"),
                {"technician": "Dana", "eta": "09:30"})
    store.put(r)
    assert store.has(eid)
    assert store.get(eid).is_complete


def test_empty_string_counts_as_missing():
    r = Receipt("dispatch", new_id("evt"), TENANT, ("technician", "eta"),
                {"technician": "Dana", "eta": ""})
    assert r.missing_fields() == ["eta"]


# --- audit walk ------------------------------------------------------------


def test_audit_clean_when_all_receipts_present():
    events = _happy_events()
    report = audit(events, _complete_store(events))
    assert isinstance(report, AuditReport)
    assert report.clean, report.findings


def test_gap_report_on_stripped_log_is_a_finding_not_an_exception():
    events = _happy_events()
    store = _complete_store(events)
    # Strip the completion receipt to simulate a governance gap.
    completed = next(e for e in events if e.event_type == "job.completed")
    del store._by_event[completed.event_id]
    report = audit(events, store)  # must NOT raise
    kinds = [(f.kind, f.event_type) for f in report.findings]
    assert ("MISSING_RECEIPT", "job.completed") in kinds
    assert not report.clean


def test_narrative_is_chronological():
    lines = narrate(_happy_events())
    assert len(lines) == 5
    assert "-> requested" in lines[0]
    assert "-> completed" in lines[-1]
    assert lines[0].startswith(" 1.") and lines[4].startswith(" 5.")


def test_narrative_records_illegal_transition_without_raising():
    lines = narrate([_event("job.requested"), _event("job.completed")])
    assert "!!" in lines[1]  # illegal move noted, not raised
