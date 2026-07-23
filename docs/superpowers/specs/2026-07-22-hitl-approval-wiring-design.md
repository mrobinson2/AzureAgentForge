# HITL action-approval wiring — design

**Date:** 2026-07-22
**Status:** approved (approach A, gate `outbound_message`)
**Roadmap item:** "Later" → Human-in-the-loop approval of agent actions; lights the v1.7 escalation-SLA auditor.

## Why

The action-approval seam (`apps/paperclip/approval.mjs`) shipped in v1.5 fully
tested but **unwired** — importing it changes nothing. The v1.7 escalation-SLA
auditor (`services/memory-governor/.../escalation_sla.py`) reads `agent_events`
for `escalation_opened` / `autonomy_decision` (correlated by
`payload.escalation_id`) but no emitter exists, so it reports an honest zero.
This wires the seam into a real action path and emits the taxonomy the auditor
already consumes — end to end, flag-gated, inert by default.

## Scope

Gate exactly ONE action kind as the reference wiring: `outbound_message` (an
agent posting a comment via `POST /api/issues/*/comments`). Destructive tool
calls (adapter path, vendored) are explicitly out of scope here.

## Approach A — gate at the auth-proxy

The auth-proxy owns the comment-post route (`proxyWithJwt`) and already reaches
the governor (`GOVERNOR_BASE_URL` / `GOVERNOR_API_KEY`, VNet DNS). The governor
owns the only `agent_events` writer (`db.emit_event`, gated `AGENT_EVENTS_ENABLED`,
never raises) but exposes no emit endpoint — so the design adds one.

### Components (owned files only)

1. **Governor `POST /escalation-event`** (`main.py`, `require_key`).
   Body: `{event_type, escalation_id, lane, source, workspace, actor_peer?,
   issue_id?, decision?, latency_ms?}`. Validates `event_type ∈
   {escalation_opened, escalation_acked, escalation_resolved, autonomy_decision}`;
   builds the payload through `escalation_sla.escalation_payload(escalation_id,
   lane=lane, source=source, workspace=workspace, **extra)` (raises on bad
   lane/source → HTTP 400); calls `db.emit_event(...)`. `AGENT_EVENTS_ENABLED`
   off → the write is a no-op; returns `{accepted: true}` either way.
   `lane ∈ {red, yellow}`, `source ∈ {approval, presend, handoff}`.

2. **auth-proxy wiring** (`auth-proxy.mjs`). Build one `createApprovalGate()`
   at boot from env. In `proxyWithJwt`, after the body is buffered and before
   the forward, for the comment route only: `action = {kind: "outbound_message",
   agent: claims.sub, summary}`. If `gate.requiresApproval(action)`:
   - mint `escalation_id = crypto.randomUUID()`, record `t0`;
   - emit `escalation_opened` (fire-and-forget);
   - `const {approved, reason} = await gate.requestApproval(action)`;
   - emit `autonomy_decision` with `decision: approved ? "approved" : "denied"`
     and `latency_ms: Date.now() - t0`;
   - `approved` → forward as normal; `!approved` → **403** `{error, reason,
     escalationId}`, do not forward.
   Not gated → unchanged path (inert).

3. **Terraform** — thread `APPROVAL_PROVIDER` (default `auto`),
   `APPROVAL_REQUIRED_KINDS` (default **empty**), `APPROVAL_WEBHOOK_URL`
   (default empty) onto the auth-proxy container env. Governor URL/key already
   wired.

### Data flow

comment POST → `proxyWithJwt` → gate check → [gated] `escalation_opened` →
decide (`auto`=deny / `allow`=approve / `webhook`=external) →
`autonomy_decision{decision, latency_ms}` → 403 or forward. SLA auditor reads
it via its documented retro path — **zero auditor changes**.

## Error handling — two deliberate postures

- **Gate = fail-closed.** Any gate error/timeout → denied (the seam already
  does this). A gated kind with `auto` provider and no approver → denied.
- **Emit = fail-open telemetry.** A governor `/escalation-event` POST failure
  from the auth-proxy is logged and swallowed; it never flips an approve to a
  deny. Mirrors `emit_event`'s "observability, not control flow". The decision
  is computed independently of whether the emit lands.

## Inert by default

`APPROVAL_REQUIRED_KINDS` empty → `requiresApproval` false for every action →
no escalation_id, no emit, no 403. The comment route is byte-for-byte unchanged
until an operator opts `outbound_message` in AND sets a non-`auto` provider (or
accepts fail-closed denials). No migration — `/escalation-event` reuses the
existing `agent_events` table (migration 0001) and emitter.

## Testing (all offline)

- **Node** (`tests/approval/`): gated+`auto` → 403; gated+`allow` → forwarded;
  empty-kinds → inert (no emit, forwarded); emit called with correct
  `escalation_opened` + `autonomy_decision` payloads (injected governor
  transport / fetch).
- **Python** (governor tests): `/escalation-event` valid body emits with the
  right payload; bad `lane`/`source` → 400; `AGENT_EVENTS_ENABLED` off → no-op,
  `accepted: true`.

## Out of scope / follow-ons

- Destructive-tool-call gating (adapter patch, approach B).
- A real `webhook` approver UI (Slack/Discord button) — the `webhook` provider
  already delegates; the external approver endpoint is a separate deliverable.
- `escalation_acked` / `escalation_resolved` emission for a two-step human
  workflow — this wiring emits the synchronous open+decision pair only.
