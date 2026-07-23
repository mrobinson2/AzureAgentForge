# Full multi-tenant — implementation plan (option 2)

**Date:** 2026-07-22
**Status:** plan / not started
**Roadmap:** "Later" → Complete multi-tenant implementation (the
`experimental/multi-tenant/` design), including multiple human users per tenant
with per-user identity and RBAC.
**Sizing:** multi-week (est. 4–6 weeks / 5 phases). This is a milestone, not a
single PR. Ship each phase flag-gated; the single-tenant default deploy stays
unchanged until a tenant is explicitly provisioned.

## Where we are (what already exists)

`experimental/multi-tenant/` is a badged **reference**, not wired into the live
single-tenant stack:

- `control-plane/` — tenant CRUD / provisioning control plane.
- `memory-store/` — tenant-scoped memory API; `tenant_id` already derived from a
  verified bearer token, Postgres RLS backstop (v1.7 aaf-0007..).
- `tenant-console/` — playbook-driven onboarding (`provision_tenant.py`,
  `src/tenantconsole/`, `playbooks/example-fieldservice`), per-tenant budget cap,
  `DESIGN.md` / `RUNBOOK.md`.
- `demos/tenant-console/` — static read-only operator demo.

**The gap to "full":** (a) it is not deployable as the running topology; (b) a
tenant has exactly one implicit principal — there is **no per-user identity or
RBAC within a tenant**; (c) no per-user auth issuance/rotation; (d) the agent
runtime + governor are single-workspace at deploy time.

## Phases

### Phase 1 — Promote the reference to a deployable module (week 1)
- New `infrastructure/modules/multi-tenant/` composing control-plane +
  memory-store as real ACA container apps behind a `multi_tenant_enabled`
  variable (default **false** → zero change to single-tenant).
- Key Vault secrets for the control-plane signing key + per-service keys via
  `seed-keyvault.sh` (extend, keep idempotent).
- `terraform validate` clean on both cost profiles; module off by default.
- **Verify:** plan is a no-op with the flag false; with it true, `plan` adds the
  two apps and nothing else.

### Phase 2 — Per-user identity within a tenant (weeks 2–3)
- Data model: `tenant_users` (tenant_id, user_id, email, status) + `user_roles`
  (user_id, role) in the control-plane schema; RLS keyed on `tenant_id` AND
  `user_id`.
- Auth: control-plane issues **per-user** JWTs (sub=user_id, tenant claim, role
  claims), HS256 from Key Vault, short TTL + refresh. Extends the existing
  three-layer auth (`docs/jwt-api-authentication.md`) with a per-user layer —
  do NOT overload the automation JWT.
- Memory identity: thread `user_id` as the memory peer alongside `tenant_id` so
  the governor's canonical-user-peer model (§18) is per (tenant,user), not
  per-tenant. Reuse the v1.8.1 peer-identity + alias machinery.
- **Verify:** two users in one tenant get isolated memory + distinct audit
  actor_peer; a cross-tenant token is rejected by RLS (add an RLS negative test).

### Phase 3 — RBAC enforcement (week 3–4)
- Role set: `owner` / `operator` / `member` / `viewer` (start minimal, YAGNI).
- A scope→role map at the control-plane + memory-store boundaries (mirror the
  auth-proxy `SCOPE_MAP` pattern). Fail-closed: unknown role → no scopes.
- Wire the HITL approval gate (option 3) per-role: e.g. `member`-initiated
  `outbound_message` gated, `owner` bypass — reuses `APPROVAL_REQUIRED_KINDS`
  keyed by role.
- **Verify:** a `viewer` token cannot mutate; role changes take effect without
  re-provision; offline RBAC matrix tests.

### Phase 4 — Per-tenant agent runtime + governor workspace (week 4–5)
- Make the governor workspace-parameterized per request (it already scopes most
  queries by workspace) — confirm every `agent_events` / `documents` path
  carries the tenant workspace, close any that default to a single workspace.
- Per-tenant budget cap enforced through the model-router's existing per-caller
  cap (`BUDGET_ENFORCE_MODE`), keyed by tenant.
- **Verify:** tenant A's spend/memory never appears in tenant B's rollups
  (`/escalation-sla`, `/memory-digest`, cost) — cross-tenant isolation test.

### Phase 5 — Onboarding + operator console live (week 5–6)
- Turn `provision_tenant.py` into the real provisioning path behind the
  control-plane API; wire the (currently static) `demos/tenant-console` to live
  control-plane data as a read-mostly operator console.
- A second worked vertical pack (see the separate "second vertical example"
  roadmap item) proves it is not single-vertical.
- **Verify:** end-to-end — provision a fresh tenant, add two users with
  different roles, exercise memory isolation + RBAC + budget cap, tear down.

## Cross-cutting

- **Security:** every phase adds RLS negative tests + a cross-tenant isolation
  test; run `mrtek-vuln-scan` on the multi-tenant surface before merge (tenant
  isolation is the #1 risk class).
- **Flags:** `multi_tenant_enabled` (master, default false) gates all infra;
  per-feature flags seed off. Single-tenant deploy is byte-for-byte unchanged
  until a tenant exists.
- **Docs:** promote `experimental/multi-tenant/*/DESIGN.md` decisions into ADRs
  as they harden; update `docs/multi-tenant-architecture.md` from Draft.

## Explicitly out of scope (this milestone)
- Billing/invoicing integration (Stripe) — separate track.
- Self-serve tenant signup UI — operator-provisioned first.
- Cross-tenant admin super-console beyond the read-mostly operator view.

## Dependencies / ordering
Option 3 (HITL approval) lands first (done) — Phase 3 reuses its gate keyed by
role. Otherwise self-contained; no dependency on the voice track (option 1).
