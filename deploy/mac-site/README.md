# Self-hosted PRIMARY site (Linux/macOS host)

A docker-compose deployment of the platform that runs on an always-on machine you
own as the **primary site**, with Azure Container Apps as a warm standby. The
**database never moves**: both sites share the same managed PostgreSQL Flexible
Server (public endpoint, `sslmode=require`), so a site switch is a stateless
compute switch — no dumps, no restores. See the self-hosted-primary ADR in
[`docs/design/`](../../docs/design/) for the full model.

> **Cross-platform note:** despite the `mac-site` directory name, the
> `docker-compose.yml` and the POSIX-bash automation here are host-agnostic —
> they run unchanged on Linux and inside WSL2. The Windows host overlay (Task
> Scheduler instead of launchd) lives in [`../windows-site/`](../windows-site/)
> and **reuses** these same files.

## Layout
| Path | Role |
|---|---|
| `docker-compose.yml` | the stack — lease-guarded, `live`/`jobs` profiles, all DB URLs point at the shared managed PostgreSQL |
| `lease-guard.sh` | init container; refuses to start unless the lease names this site `local` (anti-split-brain) |
| `boot-guard.sh` + `com.azureagentforge.boot-guard.plist` | launchd backstop (login + 300s): stops the stack if the Key Vault lease definitively names another site while `aaf-*` containers run (closes the `unless-stopped`-after-reboot hole) |
| `.env.example` | env template; `[you]` operator values + `[pull]` secrets + `[fixed]` overrides |
| `secrets-pull.sh` | regenerate `.env` secret values from Key Vault (incl. the shared PG password and the tunnel token) |
| `pull-images.sh` + `com.azureagentforge.image-pull.plist` | every 600s: registry login + compose pull; rolls the live profile only on digest change; no-op when the lease ≠ local |
| `pg-firewall-update.sh` + `com.azureagentforge.pg-firewall.plist` | every 900s: upsert the `home-ip` firewall rule on the shared PG when the home WAN IP changes |
| `com.azureagentforge.skill-curator.plist` | daily 03:30: one-shot skill-curator job (jobs profile); skips when the lease ≠ local |
| `sync/sync.sh` | hourly local→cloud file-share sync (azcopy; emits `standby_sync_completed`) — keeps the standby warm; the shared DB is never synced |
| `sync/com.azureagentforge.sync.plist` | launchd hourly trigger |
| `build-multiarch.sh` | publish amd64+arm64 image manifests so an arm64 host runs native |
| [`../../scripts/aaf-site`](../../scripts/aaf-site) | the switch: `status` / `local` (failback) / `cloud [--force]` (failover away) |

## The one rule
Bring the stack up **only** via `aaf-site local`, never a bare `docker compose up`
— the lease-guard reads the mirror that `aaf-site` refreshes from the
`platform-active-site` Key Vault secret, which must name exactly one live site.
`boot-guard.sh` backstops the one path lease-guard can't cover: Docker's
`unless-stopped` restart after a reboot.

## Failover / failback
```sh
aaf-site status          # lease + mirror + local containers + cloud apps awake?
aaf-site local           # FAILBACK → local (home state: shares cloud→local, lease, cloud sleeps)
aaf-site cloud           # FAILOVER → cloud (planned: shares local→cloud, lease, cloud wakes)
aaf-site cloud --force   # EVACUATION failover (skips the share sync — warn loudly)
```

Share-sync freshness is watched by the platform watchdog: with the standby
monitor enabled, the stale-sync detector files an issue if the last
`standby_sync_completed` event is older than its threshold.
