<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../../docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../../../docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Tenant Console — Design

> **Technical reference for contributors.** For the operational overview, start at [README](../../../README.md) or [Architecture](../../../docs/architecture.md).

> 🚧 **Reference scaffolding — not deployed.** Part of the multi-tenant roadmap (see ../README.md). Not wired into the compose stack or Terraform.

Gate: a scripted **< 30 minutes** from "new customer" to a working
vertical deployment.

## 1. Why now

Per-workspace memory partitioning is real: governed memory keys every read and
write on `workspace_name`, and a Band-1 sandbox already proved the
retrieval-path partition. Per-run economics are real: a per-run cost envelope
rides request `metadata`, and the same mechanism extends to `tenant_id`. The
Forge Console pattern (preflight → config form → preview-first → live-streamed
provisioning behind typed confirmations) shipped and was validated end-to-end in
the installer. Tenant onboarding is architecturally isomorphic to that flow;
what's missing is the tenant-shaped contract and the glue.

## 2. What a tenant IS (provisioning contract)

One tenant = one row of config, everything else derived:

```yaml
tenant:
  slug: "example-fieldservice"          # DNS-safe, immutable
  display_name: "Acme Field Services"
  vertical: "example-fieldservice"      # selects the playbook pack
  workspace: "tenant-example-fieldservice"  # governor memory partition (workspace_name)
  paperclip_company_id: <uuid>          # companies = tenants
  agents:                               # roster template instantiated per tenant
    - role: intake            # Band 1, customer-facing
    - role: coordinator       # Band 2, scoped to tenant workspace
    - role: research          # optional per playbook
  budgets:
    daily_usd: 5.00                     # router per-tenant daily cap
    per_run_usd: 0.50                   # per-run envelope default
  memory_profile:                       # the Band-1 sandbox model, parameterized
    readClasses: [pinned, durable_fact]
    scope_partition: "workspace:tenant-example-fieldservice"   # hard partition
  channels:
    - kind: external_webhook            # or other surfaces as they exist
      binding: <per-channel config>
```

Provisioning = executing that contract idempotently:

1. **PaperClip**: create company (tenant) + agents from the roster template +
   upload playbook AGENTS.md files (the multi-phase agent-deploy flow, but via
   the REST API from the console — no per-OS dependency).
2. **Governor**: no new infra — the partition IS `workspace_name`; seed the
   tenant's pinned playbook memories; register agent memory profiles
   (readClasses + hard workspace scope, exactly the Band-1 pattern).
3. **Router**: register per-tenant budget (daily cap keyed by tenant metadata —
   extends the per-run ledger, which already proves the metadata plumbing).
4. **Channels**: bind the vertical's intake surface (per-channel routing id
   becomes per-tenant).
5. **Verification lane**: a smoke conversation against the intake agent, the
   same way the deploy pipeline smoke-tests the stack.

## 3. Architecture decision: shared stack first

**Phase 1 runs tenants on the EXISTING stack** — companies-as-tenants (PaperClip
already models this), workspace-partitioned memory, shared router/memory/agent
runtime. No new infra per tenant, so provisioning is pure API orchestration →
the < 30 min gate is beatable by a wide margin, and per-tenant fixed cost ≈ $0 +
metered tokens.

The full isolation ladder (schema-per-tenant + RLS, per-tenant agent-runtime
containers, subdomain-per-tenant tunnel ingress) stays the **Phase 3 hardening
path**, adopted per-tier when a customer's data-isolation requirements demand it
— not before. See the multi-tenant reference architecture (`../ARCHITECTURE.md`)
for that design; Phase 1 deploys none of it.

**Reuse verdict on earlier scaffolding.** An early provisioning script
(`provision_tenant.py`, kept here for provenance) is the right *shape* — a
provisioning-contract executor — but predates governed memory, the per-run
envelope, and the current PaperClip API surface. The maintained executor is
rebuilt small inside `src/tenantconsole/`, lifting the still-valid tenant-record
schema and steps. Rationale: the hard 20% (memory partition, budgets, playbooks)
didn't exist when the early script was written.

## 4. The console

Forge Console pattern, ported not reinvented (the `installer/` is the working
reference: FastAPI + single-file HTML, loopback-only + per-session token,
SSE-streamed steps, server-enforced typed-confirmation apply gate):

- **Preflight** — stack reachable, admin JWT mintable, governor/router healthy,
  vertical playbook pack present.
- **Wizard** — the tenant contract as a form; renders the YAML preview
  (preview-first, nothing executes on form submit).
- **Provision** — live-streamed step execution with per-step ✓/✗ and a final
  verification-lane transcript; failure = halt + rollback notes (every step
  idempotent, so re-run converges).
- **Decommission** — typed-confirmation teardown (cancel agents, expire
  workspace memory via TTL sweep, drop channel bindings; company row archived,
  never hard-deleted — audit).

CLI parity: `scripts/provision_tenant.py --contract contract.yaml --apply` runs
the same executor headless — the console is a view over it, and CI can exercise
it.

## 5. Vertical playbook pack (per trade)

A directory per vertical — `playbooks/<vertical>/` — containing the intake skill
spec (the V1 7-question intake pattern re-skinned per trade), agent AGENTS.md
templates with `{{variable}}` substitution, seed pinned memories (pricing tiers,
service area, escalation rules — filled by the wizard), and the
smoke-conversation fixture. The bundled `example-fieldservice` pack is the
worked example; a second pack is the proof the abstraction holds.

## 6. Phasing & acceptance

- **P1 — contract + executor (headless)**: provision/decommission a tenant
  end-to-end from a YAML contract. Accept: fresh tenant to passing smoke
  conversation < 10 min, zero manual steps, idempotent re-run.
- **P2 — console UX**: the wizard + SSE stream + gates. Accept: an operator with
  no repo knowledge provisions a tenant < 30 min including form-filling;
  decommission leaves no cross-tenant memory reachable (verified by a
  governed-retrieval probe from another tenant's agent).
- **P3 — hardening ladder** (per-need): RLS/schema isolation, per-tenant
  ingress, per-tenant cost attribution (a reporting query over the event spine +
  the router ledger).

## 7. Open decisions (operator input)

1. **First vertical** — sequencing only; the pack structure is identical, so
   pick the one with the realest prospect.
2. **Reuse vs rebuild the early script** — recommendation above (rebuild small,
   lift the schema).
3. **Where the console runs** — recommendation: operator-local, like the
   Forge Console, talking to the stack over the tunnel with an admin JWT.
   Alternative (deferred): deploy as an internal service behind access control.

## 8. Non-goals (Phase 1)

Multiple human users per tenant, tenant self-service signup, per-tenant model
fine-tuning, per-tenant infra isolation, billing integration. Each has a natural
seam in the contract when its day comes.
