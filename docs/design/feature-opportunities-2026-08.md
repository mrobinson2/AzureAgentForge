# AzureAgentForge feature opportunities — August 2026

Status: **active discovery; two reference-path implementation slices built**

AzureAgentForge already has the hard-to-fake foundations: model routing,
memory, approval seams, budget controls, replayable traces, and Azure
deployment automation. The next features should turn those foundations into
operator-facing product capabilities rather than add another agent demo.

## Shortlist

| Rank | Feature | User job | Why it is valuable now | Existing leverage |
| --- | --- | --- | --- | --- |
| 1 | **Live multi-tenant control plane** | Provision an isolated customer workspace with identity, budget, memory, and a repeatable teardown path. | Converts the reference architecture into a deployable product and unlocks agencies, internal platform teams, and multiple business units. | Offline identity/RBAC/budget/onboarding cores and a Terraform module already exist. |
| 2 | **Governed scheduled agent routines** | Ask an agent to monitor, summarize, reconcile, or report on a schedule with the same budget, approval, and audit guarantees as interactive work. | Recurring work is where agents become operationally useful; it also makes spend and failure controls matter continuously. | Hermes already has cron; Forge can add tenant-scoped policy, approval, cost, and run-history control around it. |
| 3 | **Verified ACA dynamic-session sandboxes** | Run untrusted or dependency-heavy tools in a fresh, disposable Azure environment. | Makes autonomous execution safer and removes the “works on the worker image” bottleneck. | The sandbox provider seam and `aca-job` provider are scaffolded and unit-tested offline. |
| 4 | **Human approval center** | Review, approve, deny, or expire risky agent actions from one queue with a clear SLA and audit trail. | Trust is a product feature; the existing outbound-comment gate is useful but too narrow and too invisible for broader adoption. | Approval wiring, escalation IDs, `escalation_*` events, and the SLA auditor already exist. |
| 5 | **Voice agent surfaces** | Talk to an agent in a browser, phone call, or Discord voice channel with consent and interruption controls. | Voice is a differentiated interaction surface, while the offline core already proves the safety and latency model. | `services/voice-core/` has VAD, barge-in, consent, Twilio parsing, Discord RTP parsing, and cost attribution. |
| 6 | **Discord control plane** | Delegate a task in a channel and receive plan → execution → result with role checks and an audit feed. | Puts governed agent work where technical teams already collaborate, without making chat the source of truth. | The orchestrator, audit trail, role model, and optional chat-bridge pattern are already present. |
| 7 | **Prompt governance loop** | See which prompt changes caused regressions, review governed-memory feedback, and fold accepted improvements back safely. | Turns prompt quality from one-off CI protection into a measurable operating loop. | Prompt replay fixtures, memory inspector/digest, and SLA auditing are already shipped. |

## Recommendation

Start with **live multi-tenant control plane**. It has the clearest path from
existing reference code to customer-visible value and forces the platform's
identity, budget, memory, and provisioning contracts to become real together.
The first slice is deliberately smaller than “multi-tenancy GA”: make tenant
spend durable and atomic before wiring every downstream service.

## First slice: persistent tenant budget ledger

### User outcome

When two requests for the same tenant arrive concurrently, the platform must
never lose a charge or allow both requests to overspend a blocking daily cap.
The ledger must survive a process restart and expose enough history for an
operator to explain why a request was allowed, warned, or blocked.

### Scope

- Add a tenant daily-cap field and a `(tenant_id, day)` spend table to the
  reference control-plane schema.
- Add a DB-backed budget store with an atomic charge operation.
- Record every decision, including blocked attempts, in an immutable audit
  table and make retries idempotent with a caller-supplied request ID.
- Expose bounded, operator-authenticated budget-decision history by tenant and
  UTC day.
- Reuse the existing `off` / `warn` / `block` decision vocabulary and make
  `block` the fail-closed default.
- Reject negative, non-finite, or otherwise invalid charges before touching the
  database.
- Keep the pure `TenantBudget` core as the deterministic unit-test seam.
- Do not enable the multi-tenant Terraform flag or claim live deployment in
  this slice; the directory remains an explicitly labeled reference path until
  the end-to-end second-tenant validation exists.

### Acceptance criteria

1. A charge that fits the cap commits exactly once and is visible after a new
   store instance is created.
2. Concurrent blocking charges serialize on the tenant and at most one charge
   can consume the remaining cap.
3. A blocked charge is not recorded.
4. `warn` records the charge and returns a warning decision; `off` records it
   without blocking.
5. Unknown tenants and missing caps fail closed rather than receiving an
   implicit unlimited budget.
6. Database errors roll back the charge and return a stable internal error;
   raw database details never cross an API boundary.
7. Offline tests cover the decision core and SQL-facing store contract; the
   opt-in PostgreSQL integration harness must also pass before enabling the
   live module.

The offline slice now includes the decision-audit record and the control-plane
`GET /tenants/{slug}/budget/events` operator surface. The audit table records
blocked attempts without increasing spend, while `(tenant_id, request_id)`
prevents a retry from charging twice. This is still a reference-path feature;
the production migration, runtime role, and deployment path must be validated
before the module is enabled.

An opt-in integration harness now exercises two real concurrent PostgreSQL
connections against a disposable database. It is intentionally skipped unless
`AAF_TEST_DATABASE_URL` is set, and it expects `init_db.sql` to have already
been applied. The store also accepts a verified `Principal`, binds its UUID
with transaction-local `app.tenant_id`, and rejects cross-tenant requests;
operator-only calls can continue using the documented BYPASSRLS connection.
On 2026-08-23, `init_db.sql` applied cleanly to a disposable PostgreSQL 17
instance and the harness passed (`1 passed`), proving concurrent charge
serialization plus reserve/list/settle accounting. Database inspection also
confirmed RLS was enabled with the expected three tenant-isolation policies,
and the harness left zero tenant and reservation rows after cleanup.

The next onboarding seam is now executable offline: `OnboardingExecutor`
persists a checkpoint before and after every planned step, supplies a stable
step idempotency key, resumes only unacknowledged work, and compensates
completed steps in reverse order. `JsonOnboardingStateStore` is deliberately a
single-process reference store; a live deployment must replace it with a
transactional durable-job implementation before enabling Azure provisioning.
The schema and `PostgresOnboardingStateStore` now provide that transactional
state boundary for the operator control plane, but no provider driver or live
Azure onboarding route is enabled yet.

The reference `provision_tenant.py` client now sends and validates the same
positive daily budget cap required by the control-plane API, preventing the
client and server contracts from drifting back to implicit unlimited spend.

## Second slice: pre-dispatch tenant budget reservations

The model router now has an opt-in durable authority path for every
non-streaming paid route: `/v1/chat/completions`, native `/v1/messages`, and
`/v1/embeddings`. It reserves a conservative maximum in PostgreSQL before
provider dispatch, settles the hold to actual model cost on success, and
releases it when all providers fail. Atomic tenant-row locking prevents two
concurrent requests from both consuming the same remaining cap. Blocked
reservations return 429 before model spend occurs; Messages failures retain
Anthropic's error envelope.

Identity is fail-closed. The router verifies the control plane's HS256 user
token from `X-Tenant-Token` and derives tenant and caller from signed claims;
caller-supplied `X-Tenant-ID` and `X-Agent-ID` values cannot select the budget
ledger. Reservation IDs are minted server-side so untrusted correlation IDs
cannot be replayed to reuse another hold. Authority responses are checked for
matching tenant/caller/request/money values and valid lifecycle states.

The control plane exposes operator-authenticated reserve, settle, release, and
list operations. Pending holds can be filtered by tenant/day/status and include
creation/update timestamps for crash or settlement-outage reconciliation. A
settlement outage deliberately leaves the conservative hold charged and marks
the successful router response with `X-Tenant-Budget-Settlement: pending`.

This remains a reference path. The standard Terraform stack does not deploy
the experimental control plane; streaming does not yet have usage-aware final
settlement. The PostgreSQL 17 disposable-database proof has passed, but
production migration/rollback behavior and a least-privilege runtime role must
still be validated before any production enablement.

### Follow-on sequence

1. Add migration/rollback automation and validate the schema through a
   production-like, least-privilege PostgreSQL runtime role.
2. Extend usage-aware reservations to streaming chat and native Messages
   without estimating final spend at request-open time.
3. Live onboarding executor: control plane → Azure Search → Postgres → Key
   Vault, with resumable steps and compensating teardown.
4. Tenant-scoped router, Honcho, Hermes, and PaperClip propagation.
5. Provision and operate a second tenant end to end before changing the
   default from single-tenant.

## Explicitly not selected yet

Voice and Discord are compelling expansion bets, but both require live provider
and transport validation. The ACA sandbox is the strongest security-focused
follow-on, but it should be validated against a real dynamic-sessions pool
before it becomes the first customer-facing feature. Scheduled routines should
be built as a policy/control-plane feature over Hermes cron, not as a second
scheduler.
