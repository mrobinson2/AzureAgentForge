# Tenant Console — static DEMO

A sanitized, self-contained **demo** of a multi-tenant operator console for the
AzureAgentForge (AAF) platform pattern. It is a single static HTML file with
inline CSS, vanilla JS, and inline fixtures — **no server, no build step, no
network, no CDN**. It renders straight from disk.

> **Everything on this page is fictional.** The tenants, playbook packs,
> budgets, and feature flags are invented sample data. All feature flags are
> shown **OFF**. Every button is visual-only and touches nothing real — there
> are no live tenants, no endpoints, and no secrets anywhere in this demo.

## How to open

Open the file directly in any browser — no server needed:

```sh
open demos/tenant-console/index.html          # macOS
# or drag the file into a browser tab, or: file:///…/demos/tenant-console/index.html
```

## What it demonstrates

- **Tenant list** — fictional tenants (Rivertown Plumbing, Cascade HVAC,
  Northwind Septic, Bluebird Dental, Summit Dog Grooming, Meadowlark Electric),
  each with vertical, status, assigned pack, monthly budget cap, spend-to-date,
  and a % of budget used bar.
- **Tenant detail drawer** (click any row):
  - assigned playbook pack,
  - a **read-only feature-flags panel** — every flag rendered **OFF**,
  - a **budget/cost view** — monthly cap, month-to-date spend, % used bar,
    daily cap, per-run cap,
  - the agent roster (Band-1 intake + Band-2 coordinator),
  - the tenant contract (workspace partition, service area, channels),
  - a representative green/yellow/red **autonomy policy** (fail-closed).
- **Playbook Packs view** — the available packs (Septic, Plumbing, HVAC, Dental
  Front-Desk, Dog-Grooming, Electrical) and which fictional tenants each is
  assigned to.

## Design notes

- **Look & feel** matches `installer/static/index.html` (same color variables,
  font stack, pill/badge/button styling) so it reads as the same product
  surface. Dark theme, like the installer.
- **Layout is responsive:** the page body never scrolls horizontally; the wide
  tenant table scrolls inside its own container, and the detail drawer collapses
  to a near-full-width overlay on narrow screens.
- The concepts modeled here (a tenant = one contract row; a pack = a per-vertical
  bundle of agent roster + intake skill + seed memories + autonomy policy;
  per-tenant budget caps and feature flags) are drawn from the AAF/MRTek
  tenant-console design and **fully sanitized** — no real client, no real
  workspace, no platform-coupled code.

## Sanitization checklist (public repo)

- [x] Fictional tenant names only — no real customers.
- [x] All feature flags OFF (read-only demo invariant).
- [x] No secrets, no real endpoints, no deploy config.
- [x] No external resources (no CDN, fonts, or remote images) — fully offline.
- [x] Buttons are no-ops with an explicit "demo, does nothing" toast.

If you render a screenshot (`console-shot.png`), eyeball the pixels for any
personal/home-path/real-identifier before committing — a text scanner can't read
text baked into an image.
