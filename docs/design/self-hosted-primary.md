<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Self-hosted-primary, cloud warm standby — architecture reference

![status](https://img.shields.io/badge/reference-architecture-blue)

> **Status — read first.** This document describes a deployment topology and the
> assets that implement it: the compose overlay and host automation under
> [`deploy/mac-site/`](../../deploy/mac-site/) and
> [`deploy/windows-site/`](../../deploy/windows-site/), the site switch
> [`scripts/aaf-site`](../../scripts/aaf-site), and the
> [`self-hosted-primary`](../../infrastructure/profiles/self-hosted-primary.tfvars)
> cost profile. It is a **reference topology** — adopt it whole, or take the
> lease/failover pattern into your own layout. The cost anatomy is in
> [`docs/cost.md`](../cost.md).

**Audience.** A single operator (or a very small team) who already runs an
always-on machine — a mini PC, a spare desktop, an Apple-silicon mini — and would
rather that box run the platform than pay a cloud provider to run it 24/7, while
keeping a cloud deployment as a one-tap failover. If you want a hands-off,
no-hardware deployment, use the `cost-optimized` cloud profile instead; this
topology trades a little operational surface for a much smaller cloud bill.

---

## 0. The idea in one paragraph

Run the full compose stack on hardware you own as the **primary site**. Keep the
cloud deployment as a **dormant warm standby**. Point **both** sites at the
**same** managed PostgreSQL Flexible Server over its public endpoint, so the
database never moves. A single-writer **lease** names exactly one live site at a
time; a one-tap **site switch** flips the lease, syncs the file shares, and wakes
or sleeps the cloud. Because the data lives in the shared database, a failover is
a **stateless compute switch** — no dumps, no restores, near-zero RPO.

## 1. Why invert primary and standby

The default cloud deployment pays Azure Container Apps to keep the platform
running around the clock. For a single operator that is the largest line on the
bill, and most of it is idle time. If you already own an always-on machine, that
compute is a sunk cost — so let it be the primary and let the cloud be the
thing that is normally asleep.

The result (see [`docs/cost.md`](../cost.md)) is steady-state infra of roughly
**$35–45/mo** instead of the cost-optimized profile's ~$83/mo, because the two
largest cloud lines — always-on Container Apps and transaction-billed file
shares — move onto hardware you already run. What remains in the cloud is the one
shared database, the registry, and a near-idle standby.

The trade you are accepting: you now operate a machine (power, uptime, the
occasional reboot) and you accept that a home ISP and a consumer box are less
reliable than a cloud region. The failover machinery below is what makes that
trade safe rather than reckless.

## 2. The database never moves (RPO ≈ 0)

The load-bearing decision is that **there is only one database, and it is
shared**. Both the self-hosted primary and the cloud standby connect to the same
managed PostgreSQL Flexible Server over its public endpoint with
`sslmode=require`. There is no local Postgres on the self-hosted host.

This is what makes failover cheap and safe:

- **No data gravity.** A site switch moves *compute*, not *data*. Nothing has to
  be dumped, shipped, or restored, so there is no window where the two sites hold
  divergent database state.
- **RPO ≈ 0 for the database.** Whichever site is live writes to the same rows
  the other would read. The only state that is *not* in the database — the file
  shares (agent workspaces, gateway config) — is synced hourly (§5), so the
  standby's shares are at most an hour stale while the DB is always current.
- **Public endpoint, firewalled.** The shared server is reachable over its public
  endpoint, restricted to known IPs. The self-hosted host's home ISP rotates its
  WAN address, so a small keeper (§4) upserts the firewall rule when the IP
  changes. The cloud standby reaches the same endpoint the same way.

Keeping the shared Postgres always-on is deliberate: it is the failover pivot.
That single always-on managed server is the bulk of the steady-state cloud cost,
and it is the price of RPO-0 failover.

## 3. The single-writer lease (anti-split-brain)

Two sites sharing one database must never both be live — two live sites would
answer the same public URL, process the same work twice, and write duplicate
events. A **lease** enforces one live site at a time.

- **Source of truth:** a Key Vault secret `platform-active-site` ∈
  `{cloud, local}`.
- **Local mirror:** the site switch writes the lease value to a small local file
  (`~/aaf/active-site`) immediately before every bring-up. A home machine never
  holds a service principal; it only reads a mirror that an authenticated
  operator wrote seconds earlier.
- **`lease-guard`** (an init container every long-running service depends on with
  `condition: service_completed_successfully`) refuses to start the stack unless
  the mirror names *this* site. Bring the stack up **only** via the site switch,
  never a bare `docker compose up` — the switch is what refreshes the mirror.
- **`boot-guard`** closes the one hole `lease-guard` cannot see: Docker's
  `restart: unless-stopped` brings containers back after a reboot *without*
  `lease-guard` re-running. A host timer (§6) runs `boot-guard` at load and every
  few minutes; if the authoritative Key Vault lease definitively names another
  site while local containers are running, it stops the stack. On any Key Vault
  error it does **nothing** — never kill a live site on a transient network
  failure.

## 4. One tunnel, two connectors

Public ingress is a single Cloudflare tunnel with **two connectors** — one in the
cloud, one on the self-hosted host — with the tunnel origin pointing at the local
service. The lease keeps exactly one connector live at a time (`cloudflared` is in
the compose `live` profile, started only when this site holds the lease). Because
both connectors serve the same tunnel, **failover is a lease flip, not a DNS
change** — the public hostname never moves, and clients see at most a brief
reconvergence.

## 5. Failover and failback (`scripts/aaf-site`)

The site switch is the only thing that flips the lease, and it never does so
automatically — every transition is a deliberate operator action.

```sh
aaf-site status          # lease + mirror + local containers + cloud apps awake?
aaf-site local           # FAILBACK → local (sync shares cloud→local, lease, cloud sleeps, up)
aaf-site cloud           # FAILOVER → cloud (down, sync shares local→cloud, lease, cloud wakes)
aaf-site cloud --force   # EVACUATION failover (skips the share sync — loud warning)
```

- **Planned failover** (`cloud`) stops the local stack, syncs the file shares
  local→cloud so the standby is fully current, flips the lease, and wakes the
  cloud apps.
- **Failback** (`local`) is the reverse and the normal home state: sync
  cloud→local, take the lease, put the cloud back to sleep, bring the local stack
  up.
- **Evacuation** (`cloud --force`) is for when the local host is dying and cannot
  do a clean sync. It skips the share sync — the cloud serves the last hourly
  warm-standby sync (§6), so file-share writes since then are lost, but the shared
  database is safe because it never moves. The switch prints a loud warning.

Only the file shares move during a switch. The database is shared and is never
touched by the switch.

## 6. Host automation (the always-on discipline)

A handful of periodic jobs keep the primary healthy. On macOS/Linux they are
`launchd` LaunchAgents; on Windows they are Task Scheduler tasks that invoke the
**same** shared bash scripts inside WSL2. Each is a cheap no-op when this site
does not hold the lease.

| Job | Cadence | What it does |
|---|---|---|
| Boot / split-brain guard | startup + every 5 min | §3 `boot-guard` — stop the stack if the lease moved |
| Image freshness | every 10 min | registry login + `compose pull`; roll the live profile only on a digest change |
| Shared-PG firewall keeper | every 15 min | upsert the `home-ip` firewall rule when the home WAN IP changes |
| Warm-standby share sync | hourly | azcopy the file shares local→cloud; stamp a freshness event |
| Skill curator | daily | one-shot maintenance job (jobs profile) |

The hourly share sync is what keeps the standby *warm*: it copies only the file
shares (never the database) and records a `standby_sync_completed` event so the
platform watchdog can flag a standby whose shares have gone stale.

## 7. Cross-platform hosting

The Linux containers and the POSIX-bash automation are host-agnostic. They live
in [`deploy/mac-site/`](../../deploy/mac-site/) and run unchanged on Linux and
inside WSL2. The Windows overlay in
[`deploy/windows-site/`](../../deploy/windows-site/) adds **only** the
Windows-specific host layer — Task Scheduler task definitions and an admin
bootstrap that installs them — and reuses the shared compose and scripts. The
only substantive per-host difference is the image architecture tag (arm64 vs
amd64). The lease, tunnel, and shared-DB model are identical on both.

## 8. When not to use this

- You want zero hardware to own or operate → use `cost-optimized`.
- You need a strong availability SLA → a cloud-primary with zone-redundant HA
  (`hardened`) is a better fit than a consumer box on a home ISP.
- You are evaluating the project → run the local `docker compose --profile full`
  stack from the repo root first; adopt this topology once you know you want an
  always-on deployment.

## 9. What is deliberately not parameterized

The `self-hosted-primary` Terraform profile only sets the cost/network knobs the
repo exposes (Postgres tier, public Key Vault access with default-deny,
Cloudflared on, minimal logs). Two parts of this topology are **architectural**,
not Terraform toggles:

- the standby's dormant / scale-to-zero posture — bring-up is operator-driven via
  the failover runbook, not an apply-time variable;
- running the stack on owned hardware — that is `deploy/mac-site/`, outside
  Terraform entirely.

Restrict the shared endpoints to your egress with `key_vault_allowed_ip_ranges`.
