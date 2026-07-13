# Design — Multi-tenant AzureAgentForge

**Date:** 2026-06-26
**Status:** Design spec. Supersedes the memory-plane assumptions in
[`experimental/multi-tenant/ARCHITECTURE.md`](../../../experimental/multi-tenant/ARCHITECTURE.md)
(kept as the authoritative reference for the SQL/HCL detail). Implementation is
**phased** — this is the umbrella design; each phase gets its own plan.
**Target:** AzureAgentForge as a flag-gated capability (`multi_tenant_enabled`),
sanitized for the public template.

---

## 1. Summary

Turn AAF from a single-tenant stack into a platform that runs many isolated
tenants on **one shared deployment** (one Postgres, one Container Apps
Environment, one Key Vault), with hard isolation enforced **at the database
layer** by PostgreSQL Row-Level Security. A tenant is identified by a `tenant_id`
that rides every request (resolved from subdomain or JWT), is set as
`app.tenant_id` on every DB connection, and is enforced by RLS policies that
default-deny. A small **control plane** (`aaf_core` registry + provisioning API)
and a per-tenant **Terraform module** make onboarding a tenant a bounded,
repeatable operation.

The reference design in `ARCHITECTURE.md` already specifies the SQL, RLS
policies, router budget code, Terraform `modules/tenant`, onboarding flow, and
security model in detail. **This spec does not restate them** — it (a) locks the
decisions, (b) **reconciles the design with the code that actually shipped**, (c)
closes the governed-memory gap the reference doc predates, and (d) defines
component boundaries, phasing, testing, and done-criteria.

## 2. Goals / non-goals

**Goals**
- Hard tenant isolation at the data layer (RLS, default-deny), un-bypassable from app code.
- One shared infrastructure footprint; per-tenant marginal cost near zero in shared mode.
- Per-tenant budget tracking and per-tenant agent memory.
- Bounded onboarding: registry row + Terraform module + secret seed + DNS.
- Ship sanitized and flag-gated; single-tenant remains the default and is unaffected.

**Non-goals (this milestone)**
- Database-per-tenant or schema-per-tenant-via-separate-schemas (RLS on shared `public` is the decision; see §4.1).
- A billing/metering product (budget tracking exists; invoicing does not).
- The experimental mem0 / Azure AI Search per-tenant memory backend (superseded; see §4.3).
- Self-service signup UI (onboarding is operator/admin-driven via the control-plane API).

## 3. What exists today (grounding)

- **Reference design** — `experimental/multi-tenant/ARCHITECTURE.md`: complete, authoritative for detail.
- **Control plane (~built, not deployed)** — `experimental/multi-tenant/control-plane/`: a FastAPI provisioning API + `aaf_core` registry schema (`tenants`, `users`, `channels`, …). **Keep** — this is the tenant control plane. It currently provisions an Azure AI Search index per tenant (`vector_index_name`); that step is dropped (see §4.3).
- **memory-store (~built, not deployed)** — `experimental/multi-tenant/memory-store/`: a standalone pgvector memory service for the mem0/AI-Search model. **Supersede** — the shipped stack uses Honcho + the memory-governor instead.
- **Shipped single-tenant services** — `services/{paperclip,hermes(agent-runtime),model-router,honcho,memory-governor,watchdog,teams-bridge}` on one VNet + Postgres Flexible Server + Key Vault. These are the real integration surface.
- **Drift to reconcile:** the reference doc names predate the template — `ca-orchestrator`→`paperclip`, the `platform-*` KV prefix vs the current `seed-keyvault.sh` names, `app.example.com` as a literal. The spec uses generic, current names; concrete hostnames/prefixes are variables.

## 4. Key decisions (locked here)

### 4.1 Isolation: RLS on shared `public` schema
Adopt the reference recommendation. One Postgres instance, shared `honcho`/`paperclip` databases, a `tenant_id` column on every tenant table, RLS policies keyed on `current_setting('app.tenant_id')`, app roles (`honcho_app`, `paperclip_app`, `governor_app`) that are subject to RLS while the `postgres` admin bypasses it. Rationale and the rejected alternatives are in `ARCHITECTURE.md` §1.

### 4.2 Tenant context propagation
`tenant_id` is resolved at the edge (Paperclip from subdomain `*.<base-domain>`; agents from the inbound channel) and carried as: a JWT claim through the app, an `X-Tenant-ID` header to the model-router, and `SET LOCAL app.tenant_id` on every DB transaction (the SQLAlchemy `checkout` / Drizzle `withTenant` patterns in `ARCHITECTURE.md` §2.3). A request with no resolvable tenant is **rejected**, never defaulted.

### 4.3 Memory tenancy on the shipped stack (the reconciliation)
Multi-tenant memory is **Honcho + memory-governor**, isolated by `tenant_id` + RLS on their Postgres tables — *not* the experimental mem0/Azure-AI-Search/`memory-store` path, which is retired. Concretely this **adds scope the reference doc omits**:
- Honcho tables get `tenant_id` + RLS (reference §2.1 covers this).
- **memory-governor** tables (`session_memory`, `documents` annotations, `durable_facts`, `agent_events`, and the `feature_flags`/registry it reads) get `tenant_id` + RLS, and `/admit` + `/plan-retrieval` set `app.tenant_id` per call. Admission authority and retrieval planes become tenant-scoped.
- **watchdog** detectors and the lessons they write (`durable_fact` per agent) are tenant-scoped, so one tenant's failure signatures never leak into another's agents.

This is the single largest delta from `ARCHITECTURE.md` and the main source of new implementation work.

### 4.4 Hermes mode
Default **Option A** (one shared Hermes; tenant resolved per-inbound-channel; tenant-scoped Honcho `app_id` and `/opt/data/{slug}/`). **Option B** (dedicated Hermes container app per tenant) is a per-tenant Terraform flag (`enable_dedicated_hermes`) for tenants needing their own Telegram bot / isolated network path. Both behind `modules/tenant` (reference §3, §8).

### 4.5 Ingress
Wildcard Cloudflare tunnel (`*.<base-domain>` → the shared Paperclip ingress); Paperclip resolves the tenant from the subdomain (reference §6). Base domain is a variable, not a literal.

## 5. Components (each independently testable)

1. **Control plane** (`aaf_core` registry + provisioning API) — owns tenant lifecycle: create/list/suspend a tenant, its users/channels/api-keys/features. Source of truth for "what tenants exist."
2. **Data-layer tenancy** — migrations adding `tenant_id` + indexes + RLS policies + app roles across honcho / paperclip / governor schemas; the per-connection `SET app.tenant_id` middleware in each service.
3. **Router budget** — per-tenant `tenant→tier→spend` tracking, DB-backed (`budget_spend`, `tenant_budget_limits`), gated by `X-Tenant-ID` (reference §4). Falls back to flat single-tenant behavior when the header is absent.
4. **Hermes scoping** — tenant→`HONCHO_APP_ID` mapping + tenant-scoped file-share paths + `X-Tenant-ID` to the router sidecar.
5. **Paperclip resolver** — subdomain→tenant middleware; `company_id`/`tenant_id` in Better-Auth JWT; `SET app.tenant_id` on all connections.
6. **Terraform `modules/tenant`** — per-tenant managed identity + RBAC (KV scoped to `…-{slug}-*`), file share, optional dedicated Hermes; driven by a `tenants` map (reference §8).
7. **Onboarding** — the bounded sequence (registry row → tfvars entry → apply → secret seed → DNS → verify) (reference §9), with the control-plane API as the entry point.

Each maps to a phase in §7 and can be built/tested without the others present (behind the flag).

## 6. Security (the core of the feature)

RLS is the isolation mechanism; everything else is defense in depth. The spec adopts `ARCHITECTURE.md` §11 wholesale and extends it:
- **Default-deny:** every tenant table has RLS enabled; a connection without `app.tenant_id` sees zero rows.
- **App-role vs admin:** services connect as `*_app` roles (RLS-subject); migrations/ops use the admin (RLS-bypass) explicitly.
- **Governor/watchdog** included in the RLS perimeter (§4.3) — the reference doc's table list is extended to the governed-memory tables.
- **Audit:** the `audit_log` table + triggers (reference §11.2) capture tenant-scoped mutations.
- **Verification is a test artifact, not a checklist:** the cross-tenant pen-test queries (reference §11.4) become automated isolation tests (§8) that must pass in CI before the flag can be enabled in any environment.

## 7. Phasing (umbrella → per-phase plans)

The reference §12 four-phase plan is sound; adopt it with the governor work folded in. Each phase is a separate implementation plan and ships behind the flag.

- **Phase 1 — Data-layer foundation.** `tenants` registry; `tenant_id` + indexes + RLS + app roles across honcho **and the governor tables**; per-connection `SET app.tenant_id` middleware in Honcho + governor. Existing single-tenant behavior preserved (seed `operator`, backfill, default-deny only after backfill). **This is the first plan to write.**
- **Phase 2 — Router + Hermes.** Per-tenant budget (DB-backed) behind `X-Tenant-ID`; Hermes tenant→`HONCHO_APP_ID` + file-share scoping; `modules/tenant` (structured, `operator`-only, no behavior change).
- **Phase 3 — Paperclip + ingress + onboarding.** Subdomain resolver + JWT `tenant_id` + `SET app.tenant_id`; wildcard Cloudflare; control-plane API wired to drive provisioning; `audit_log`.
- **Phase 4 — Second tenant + validation.** Onboard a test tenant end-to-end; run the automated isolation/budget/memory tests; load test; runbooks; production cutover.

## 8. Testing

- **RLS isolation (automated, gates the flag):** as `*_app` role, set tenant A, assert zero rows for tenant B; reset context, assert zero rows (default-deny). One test per tenant table, including the governor tables.
- **Budget isolation:** tenant A over-budget does not throttle tenant B; spend persists across a simulated router restart.
- **Memory isolation:** governor `/admit` + `/plan-retrieval` and Honcho recall return only the calling tenant's data; a watchdog lesson written for tenant A is never injected into tenant B's agents.
- **Regression:** the full single-tenant suite passes with RLS active and the flag **off** (proves zero-impact default).
- **Migration safety:** Phase-1 migration is idempotent and reversible (drop policies / revert role) on a seeded single-tenant DB.

## 9. Risks & open questions

- **Honcho upstream `tenant_id`** — Honcho is vendored; adding `tenant_id` + RLS may need a maintained patch/fork (reference risk register). Confirm against the vendored Honcho schema before Phase 1.
- **Governor tenancy complexity** — the governed-memory layer (admission authority, four-plane retrieval, contradiction sweeps, the watchdog loop) is the hardest surface to make tenant-correct; it is also flag-gated-off today, so Phase 1 can land its RLS without the governor being live.
- **Connection limits** — B1ms (~50 conns) bounds tenant count under per-connection `SET`; monitor, plan the B2ms bump (reference risk register).
- **Drift reconciliation** — the per-connection middleware must be added to each *current* service's actual DB layer; verify the SQLAlchemy/Drizzle integration points still match the reference patterns.
- **Open:** does Paperclip's shipped `company` model still map 1:1 to a tenant, or has it diverged? Confirm before Phase 3.
- **Open:** retire `memory-store/` outright, or keep it as a documented alternative? (Default: mark superseded, leave in `experimental/`.)

## 10. References

- Authoritative detail: `experimental/multi-tenant/ARCHITECTURE.md` (§1 isolation, §2 data layer + middleware, §4 router budget, §6 Cloudflare, §8 Terraform module, §9 onboarding, §11 security, §12 migration).
- Control plane: `experimental/multi-tenant/control-plane/` (`aaf_core` registry + provisioning API).
- Superseded: `experimental/multi-tenant/memory-store/` (mem0/AI-Search memory model).
- Shipped memory architecture this builds on: `docs/design/memory-system.md`, `services/memory-governor/`, `services/watchdog/`.
