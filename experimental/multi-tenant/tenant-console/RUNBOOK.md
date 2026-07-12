<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../../docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../../../docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Tenant Console — Runbook

> **Technical reference for contributors.** For the operational overview, start at [README](../../../README.md) or [Architecture](../../../docs/architecture.md).

> 🚧 **Reference scaffolding — not deployed.** Part of the multi-tenant roadmap (see ../README.md). Not wired into the compose stack or Terraform.

Provision (or decommission) a customer tenant on a shared stack. A tenant = a
PaperClip company + an intake (Band-1) and coordinator (Band-2) agent + a
workspace-partitioned memory namespace (`tenant-<slug>`) + per-tenant budgets +
channel bindings. Shared-stack-first: no new infra per tenant.

Two entry points over the **same** executor:
- **CLI** — `scripts/provision_tenant.py` (headless, CI-friendly).
- **Console** — `scripts/tenant-console` (operator GUI, loopback-only).

## Prerequisites

1. **The control-plane seams.** The tenant flow depends on governor per-tenant
   memory profiles + a `/workspace-expire` endpoint, a router per-tenant daily
   budget ledger (riding the per-run cost-envelope mechanism), an auth-proxy
   admit/expire passthrough + native instructions-bundle scopes, and
   planner-allowlist prefix rules (`intake-*` / `coordinator-*`). In the
   single-tenant reference stack these live in the control-plane services; wire
   them before running the tenant flow.
2. **A token.** The executor needs an admin JWT. Either export one
   (`TENANT_CONSOLE_TOKEN`), a signing secret (`TENANT_CONSOLE_JWT_SECRET`), or
   let it mint from Key Vault (`platform-paperclip-automation-jwt-secret` in
   your Key Vault) via `az` — the same secret your API-token generator uses.
   (Your `az login` must have data-plane read on that Key Vault secret.)

> **Cloudflare note.** If the stack sits behind Cloudflare, it may 403 (error
> 1010) on non-browser User-Agents. The client already sends a browser UA, so
> the CLI/console reach the tunnel — but if you script raw `curl`/`urllib`
> against the stack, add a browser `User-Agent` header or you'll get a spurious
> 403.

## Author a contract

Copy `examples/example-fieldservice.yaml` and edit the tenant block (slug,
display_name, budgets, the wizard variables). The four structural variables
(`tenant_slug`, `tenant_display_name`, `vertical`, `workspace`) are derived — do
**not** set them. `vertical` selects the pack under `playbooks/`.

## Dry run (always do this first — zero writes, no auth)

```
scripts/provision_tenant.py --contract path/to/contract.yaml
```

Renders the full plan (company, both agents + their env binding, seed count,
channels). Makes no API calls.

## Provision

CLI:
```
scripts/provision_tenant.py --contract path/to/contract.yaml --apply
# add --skip-smoke to skip the intake smoke conversation
# report written to ./tenant-report-<slug>.json
```

Console:
```
scripts/tenant-console       # prints http://127.0.0.1:8722/?token=…
```
Contract tab → Preview (preview-first) → Provision (live SSE steps).

Steps (idempotent — a failed run re-runs cleanly): preflight → ensure_company →
ensure_agents → upload_instructions (byte-exact verify) → seed_memories (dedupe
on snippet) → bind_channels (external channels = pending in P1) → smoke (opens
an issue to the intake agent; retries around non-deterministic wake).

## Decommission

```
scripts/provision_tenant.py --contract path/to/contract.yaml \
  --decommission --confirm "decommission <slug>"
```
Comments-then-cancels open issues, terminates + deletes agents, archives the
company (never hard-deleted — audit), expires the workspace memory partition,
then **probes that the partition reads empty** (fails loudly if any memory is
still reachable). The console's Decommission tab does the same behind the typed
`decommission <slug>` gate.

## Verifying the gate

A fresh tenant → passing smoke conversation should complete in well under the
< 30 min target (P1 target: < 10 min headless). Time it on the first real tenant.

## Rollback / safety

- Provisioning writes nothing until `--apply` / the Provision button.
- Every step is idempotent; re-run to converge.
- Decommission is reversible in effect (company archived, not deleted) except the
  expired memory, which is the point.
- The per-tenant budget soft-downgrades to the floor tier when exhausted (never
  hard-rejects mid-conversation) — same posture as the per-run envelope.

## P3 (deferred, per-need)

Schema/RLS isolation, per-tenant agent-runtime containers, subdomain ingress,
and per-tenant cost attribution are the hardening ladder — adopt per customer
when data-isolation requirements demand it. See `../ARCHITECTURE.md` for that
reference design.
