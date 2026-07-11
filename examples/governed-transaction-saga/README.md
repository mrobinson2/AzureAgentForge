# Governed Transaction Saga

A compact, dependency-free **event-sourced governance core**. It shows the
minimum machinery that makes an autonomous action *governable*: you can always
say what happened, in what order, why, and whether every action that needed
proof actually carries it.

The worked scenario is a fictional **appliance-repair job** (all data invented,
no real customers, phones, or keys). Everything is pure Python standard library
and runs locally — no Azure subscription, no network, no secrets.

```
saga/
  events.py     append-only, idempotent event log (tenant/correlation/causation ids)
  machine.py    Stage enum + pure fold/apply state machine (IllegalTransition)
  receipts.py   complete-at-write receipts (ReceiptStore refuses incomplete writes)
  audit.py      chronological narrative + receipt-gap report (findings, not exceptions)
tests/          pytest suite (15 tests)
```

## The four ideas

1. **Append-only log (`events.py`).** State is never mutated in place. Every
   change is an immutable `Event` carrying a `tenant_id` (isolation boundary), a
   `correlation_id` (which job), and a `causation_id` (which event caused this
   one). The log is **idempotent by event id**, so a redelivered message is a
   safe no-op — the foundation for at-least-once delivery and clean replay.

2. **Fold/apply machine (`machine.py`).** The current state is *derived* by
   folding the event stream through a pure `apply(state, event)` function. The
   legal life-cycle is `REQUESTED → SCHEDULED → DISPATCHED → IN_PROGRESS →
   COMPLETED`, with `CANCELLED` reachable from any live stage; both terminals are
   sinks. Any move off those edges raises `IllegalTransition`. Same events in,
   same state out — always.

3. **Complete-at-write receipts (`receipts.py`).** A receipt is durable proof
   that a governed action happened *with* its required evidence. `ReceiptStore`
   refuses — at write time, before anything is persisted — any receipt missing a
   field it claims to require. There is no half-written receipt in the store.

4. **Audit walk (`audit.py`).** `narrate()` produces a chronological,
   human-readable story of the saga. `audit()` adds a **receipt-gap report** in
   which a missing or incomplete receipt is a **named `Finding`**, never an
   exception — because a report that throws on the first problem can't enumerate
   the rest, and enumeration is the point of an audit.

## How this maps to AzureAgentForge

This example is the sanitized skeleton of the governance features the platform
ships:

- **HITL approval → receipts.** In AAF, a human-in-the-loop approval gate is
  what lets a risky agent action proceed. The receipt here is exactly that
  gate's durable artifact: the action does not count as governed until the
  proof of approval (who, what, evidence) is written *completely*. The
  complete-at-write rule is the code form of "no approval, no action."

- **Memory governor → audit walk.** The governor's job is to scan the record of
  what agents did and flag what falls short of policy. `audit()` is that scan in
  miniature: it walks the append-only log and returns findings for every action
  that should carry a receipt but doesn't — a gap becomes a reviewable line
  item, not a crash.

- **Tenant isolation → the id triplet.** Multi-tenant governance depends on
  never crossing streams. `for_correlation(corr, tenant_id=...)` enforces the
  boundary the same way the platform scopes every memory and event read.

Feature-flag posture matches the release rules: there are no flags to flip here
and nothing that reaches a live service — it is a read-and-test-locally
reference for the pattern.

## Run the tests

```bash
cd examples/governed-transaction-saga
python3 -m pytest -q
```

## Try it

```python
from saga import Event, fold, audit, ReceiptStore, Receipt

TENANT, JOB = "tenant_demo", "job_42"
e = lambda t, **p: Event(t, TENANT, JOB, payload=p)

events = [e("job.requested"), e("job.scheduled"), e("job.dispatched"),
          e("job.started"), e("job.completed")]

print(fold(events).stage)              # Stage.COMPLETED

report = audit(events, ReceiptStore())  # no receipts filed yet
for f in report.findings:
    print(f.kind, f.event_type)         # MISSING_RECEIPT job.dispatched / job.completed
```
