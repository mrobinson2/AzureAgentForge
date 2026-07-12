# Architecture diagrams

> **Technical reference for contributors.** For the operational overview, start at [README](../../../README.md) or [Architecture](../../architecture.md).

Editable, architect-grade diagrams of the AzureAgentForge platform. The
`.drawio` files are the source of truth; the `.svg` files are rendered
companions for embedding in Markdown.

## Files

Each diagram is a pair: a `.drawio` editable source (open at
[app.diagrams.net](https://app.diagrams.net) or with the VS Code Draw.io
extension) and a rendered `.svg` companion for `<img>` embeds in Markdown. All
five diagrams below are complete and embedded in the docs.

> **SVG re-export.** The committed `.svg` is hand-authored to match the
> `.drawio` for review. For pixel-perfect fidelity (and after any edit to the
> `.drawio`), re-export the canonical SVG from diagrams.net:
> **File → Export as → SVG…** (uncheck "Include a copy of my diagram" if you
> don't want the source embedded), then overwrite `system-architecture.svg`.

## Diagram set

All five diagrams share the same conventions and naming so the set reads as one
family. Each is embedded in the doc named in its row.

| # | File | Scope |
|---|---|---|
| 1 | `system-architecture.{drawio,svg}` | **This one.** Logical system: chat surfaces → app services → memory/data → LLM backends, with region / VNet / Container Apps Environment boundaries and a numbered request flow. |
| 2 | `network-topology.{drawio,svg}` | VNet + subnet layout: `app-subnet`, `db-subnet`, `pe-subnet`, `admin-subnet`, delegations, private DNS zones, NSGs, private endpoints, ingress modes (ACA managed vs. Cloudflared). |
| 3 | `request-dataflow.{drawio,svg}` | Sequence-style agent/request flow: user → PaperClip → Hermes → Model Router → Foundry, with Honcho memory reads/writes, governor admission/retrieval, budget + fallback decisions. |
| 4 | `deploy-pipeline.{drawio,svg}` | Build/deploy: `az acr build` → Key Vault seeding → `terraform plan` → destroy-aware approval gate → `apply` → post-deploy smoke (Forge Console + reference GitHub Actions pipeline). |
| 5 | `multi-tenant.{drawio,svg}` | Multi-tenant target architecture: schema-per-tenant, RLS, per-tenant routing (marked *design target*, ~20–30% implemented). |

## Shared conventions

Apply these to every diagram in the set so they stay visually consistent.

### Icons
- Official **Azure service icons** via the draw.io **`mxgraph.azure2019`**
  stencil set (`shape=mxgraph.azure2019.<service>`), e.g.
  `container_instances`, `azure_database_for_postgresql_servers`,
  `key_vaults`, `container_registries`, `log_analytics_workspaces`,
  `virtual_networks`, `cognitive_services` (Azure AI Foundry).
- The companion `.svg` uses simplified glyphs that read as the same services
  (the canonical render comes from re-exporting the `.drawio`).

### Boundaries
- **Dashed** outlines mark trust boundaries, nested outermost → innermost:
  **Azure region** (blue, `8 4` dash) ⊃ **VNet** (`6 4` dash) ⊃
  **Container Apps Environment** (solid, `app-subnet`).
- Private data services (PostgreSQL, Key Vault) sit inside the VNet and carry a
  lock marker; PostgreSQL is VNet-injected with public access disabled.

### Layout
- Layered top-to-bottom: **chat surfaces / ingress → app services → memory &
  data → LLM backends**. Generous whitespace, aligned columns, no spider-web
  routing.
- **Optional / flag-gated** components (Teams bridge, Cloudflared, Memory
  Governor + its jobs, OpenAI-compat fallback) use a **dashed component
  outline** and muted grey fills.

### Edges
- **Numbered solid** arrows (`1, 2, 3…`) trace the happy-path request through
  the system. Numbers carry a colored pill.
- **Dotted** grey arrows mark **control / secret access** — Key Vault secrets
  read at startup, telemetry to Log Analytics — kept visually separate from the
  request path.
- Memory read/write edges are bidirectional; the LLM/fallback edge is dotted
  gold to signal the fallback chain.

### Palette & type
- Restrained Azure blues/greys with a single green accent for data/secret
  services and a muted gold for external LLM backends. No rainbow boxes.
- Type: Segoe UI (Helvetica / Arial fallback). Component titles bold 12–13px,
  descriptions 10px in muted slate.

### Title & legend
- Every diagram carries a **navy title bar** (`AzureAgentForge — <subject>`)
  and a **legend** explaining: dashed = boundary, numbered solid = data flow,
  dotted = control/secret, dashed outline = optional service.

## File naming

```
docs/assets/diagrams/
  <subject>.drawio   # editable source (mxGraphModel, azure2019 stencils)
  <subject>.svg      # rendered companion (re-export from .drawio)
  README.md          # this file
```

Use lowercase, hyphenated subjects (`system-architecture`, `network-topology`,
`request-dataflow`, `deploy-pipeline`, `multi-tenant`).
