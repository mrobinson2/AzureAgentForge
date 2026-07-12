# Windows PRIMARY site (WSL2 + Task Scheduler)

> **Technical reference for contributors.** For the operational overview, start at [README](../../README.md) or [Architecture](../../docs/architecture.md).

The Windows equivalent of [`deploy/mac-site/`](../mac-site/). It runs the **same
platform stack** — a Docker Compose set of Linux containers (paperclip, honcho,
model-router, memory-governor, cloudflared) talking to the **shared** public
managed PostgreSQL, Key Vault, container registry, and the one Cloudflare tunnel
— on a Windows host via **WSL2 (Ubuntu) + Docker Desktop**. See the
self-hosted-primary ADR in [`docs/design/`](../../docs/design/). The **database
never moves**: both sites share the same managed PostgreSQL Flexible Server, so a
site switch is a stateless compute switch.

## Reuse, not duplicate — the one thing to understand first

Everything host-agnostic already lives in `deploy/mac-site/`: the
`docker-compose.yml`, and the POSIX-bash automation (`boot-guard.sh`,
`pull-images.sh`, `pg-firewall-update.sh`, `sync/sync.sh`, `secrets-pull.sh`,
`lease-guard.sh`). Those are **Linux containers + POSIX bash → they run UNCHANGED
inside WSL2.** This directory does **not** copy them (that would create drift —
the router-models anchor, honcho reasoning-level config, `PAPERCLIP_API_URL` and
all the other shared settings live only in the mac-site compose). It is **only**
the Windows-specific host-automation overlay:

| This directory | Role |
|---|---|
| `README.md` | this Windows setup runbook |
| `Install-WindowsSite.ps1` | admin bootstrap: verifies WSL/Docker, sets power policy, imports the five scheduled tasks (the launchd equivalent) |
| `tasks/*.xml` | five Task Scheduler 1.2 task definitions — one per launchd LaunchAgent — each invoking `wsl.exe … bash -lc <shared mac-site script>` |
| `.env.example` | note file: the real `.env` is the SHARED `deploy/mac-site/.env`; documents the only two Windows overrides (amd64 image tags + `AAF_HOME`) |

> **Naming note:** `deploy/mac-site/` holds the **cross-platform** compose + bash
> assets despite its name. Read "mac-site" as "the shared Linux stack + host
> scripts", which the Windows site reuses from inside WSL.

## Prerequisites

- **Windows 10 (22H2+) or Windows 11**, x86-64, **16 GB+ RAM**, always-on.
  Disable sleep/hibernate on AC — `Install-WindowsSite.ps1` does this via
  `powercfg`, but confirm "Sleep = Never (plugged in)" in Power & sleep.
- **WSL2 with Ubuntu** — `wsl --install -d Ubuntu` then reboot. Confirm it is
  version 2 with `wsl -l -v`.
- **Docker Desktop** with **WSL2 integration enabled** for the Ubuntu distro
  (Settings → Resources → WSL integration). Set Docker Desktop to "Start on
  login" so the stack's `restart: unless-stopped` containers come back.
- Inside WSL Ubuntu, install the toolchain the shared scripts need:
  ```bash
  # az CLI
  curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
  # postgres client (psql) — sync.sh stamps the freshness event via psql
  sudo apt-get update && sudo apt-get install -y postgresql-client zsh
  # azcopy (share sync)
  curl -sL https://aka.ms/downloadazcopy-v10-linux | tar -xz --strip-components=1 -C /tmp
  sudo mv /tmp/azcopy /usr/local/bin/ && sudo chmod +x /usr/local/bin/azcopy
  # docker / docker compose come from Docker Desktop's WSL integration (verify):
  docker version && docker compose version
  ```
  `zsh` is required because the failover tool `scripts/aaf-site` is a zsh script.
  The five automation scripts themselves are POSIX bash and need no zsh.

## Setup (run these IN ORDER)

All of steps 1–7 run **inside WSL Ubuntu** except step 6 (Windows PowerShell).

1. **`az login`** — the operator identity, same Azure roles as the mac-site:
   *Key Vault Secrets User* (secrets-pull), *Storage File Data Privileged
   Contributor* on the storage account (share sync writes), and PG / Automation
   Operator (failover: firewall rule + sleep/wake runbooks). Then **`azcopy
   login`** for the hourly share sync.

2. **Clone the repo into WSL** (on the Linux filesystem, not `/mnt/c`, for speed):
   ```bash
   git clone --recurse-submodules \
     https://github.com/your-org/AzureAgentForge.git ~/Code/AzureAgentForge
   cd ~/Code/AzureAgentForge
   ```

3. **Build the shared `.env`** from Key Vault (same secrets as the mac-site):
   ```bash
   cd deploy/mac-site
   cp .env.example .env
   # set the [you] values: AZURE_PG_HOST, AAF_HOME=/home/<you>/aaf
   ./secrets-pull.sh          # writes the KV secret values into .env
   ```

4. **Pin amd64 image tags in `.env`** — the ONLY difference from an arm64 host. On
   x86-64 Windows the native images are the pipeline's **amd64** tags (the plain
   build number), NOT an `-arm64` side tag:
   ```bash
   # in deploy/mac-site/.env  (example — bump the number to match the cloud)
   HONCHO_IMAGE_TAG=100
   ROUTER_IMAGE_TAG=100
   PAPERCLIP_IMAGE_TAG=100
   MEMORY_GOVERNOR_IMAGE_TAG=100
   ```
   See [`.env.example`](.env.example) for the two Windows overrides in full.

5. **Bring the stack up from `deploy/mac-site`** — the compose is shared and
   host-agnostic. Prefer the site switch (it takes the lease first); a bare
   `docker compose up` skips the lease-guard:
   ```bash
   cd ~/Code/AzureAgentForge
   ./scripts/aaf-site local          # sync shares, take lease, compose up (live profile)
   # or, once the lease already names this site:
   #   cd deploy/mac-site && docker compose --profile live up -d
   ```

6. **Install the scheduled tasks** — open **Windows PowerShell as Administrator**
   and run the bootstrap. It reads the tasks from WSL-side paths but registers
   them on the Windows side:
   ```powershell
   # From the repo checkout as seen from Windows (\\wsl$\Ubuntu\home\... or a
   # cloned copy), e.g.:
   cd \\wsl$\Ubuntu\home\<you>\Code\AzureAgentForge\deploy\windows-site
   .\Install-WindowsSite.ps1
   # override defaults if needed:
   #   .\Install-WindowsSite.ps1 -WslDistro Ubuntu-22.04 -RepoPathWsl '~/Code/AzureAgentForge'
   ```
   The script auto-detects the WSL user, resolves the repo to an absolute WSL
   path, substitutes `{{REPO}}` / `{{WSL_DISTRO}}` / `{{WSL_USER}}` into each task
   XML, and registers them under Task Scheduler `\AzureAgentForge\` (idempotent).

7. **Verify**: `./scripts/aaf-site status` (lease + containers + cloud standby),
   and in Task Scheduler check the five `\AzureAgentForge\` tasks show a "Last Run
   Result" after their first fire. Logs are in WSL at `~/aaf/logs/`.

## The WSL / Windows split (launchd ⇄ Task Scheduler)

On a Mac, five `launchd` LaunchAgents fire the host automation. On Windows the
same automation lives one layer down in WSL, and **Windows Task Scheduler is the
timer** — each task simply invokes `wsl.exe -d <distro> -u <user> -e bash -lc
"<shared mac-site script>"`. The containers and the bash scripts run in WSL; Task
Scheduler is only the trigger. That is the exact Windows analogue of launchd
firing a `/bin/zsh -lc <script>`.

| Automation | launchd (macOS) | Task Scheduler (Windows) | Cadence | Runs (in WSL) |
|---|---|---|---|---|
| Boot / split-brain guard | `com.azureagentforge.boot-guard` | `AAF-BootGuard` | AtStartup + every 5 min | `deploy/mac-site/boot-guard.sh` |
| Image freshness | `com.azureagentforge.image-pull` | `AAF-ImagePull` | every 10 min | `deploy/mac-site/pull-images.sh` |
| PG firewall keeper | `com.azureagentforge.pg-firewall` | `AAF-PgFirewall` | every 15 min | `deploy/mac-site/pg-firewall-update.sh` |
| Share sync (→ cloud) | `com.azureagentforge.sync` | `AAF-ShareSync` | hourly | `deploy/mac-site/sync/sync.sh` |
| Skill curator | `com.azureagentforge.skill-curator` | `AAF-SkillCurator` | daily 03:30 | `docker compose --profile jobs run --rm skill-curator` |

Each task sets `StartWhenAvailable=true` so a run missed while the PC was asleep
catches up on wake, and `MultipleInstancesPolicy=IgnoreNew` so overlapping fires
don't stack. They run under the interactive user token (Docker Desktop's WSL
integration lives in that session), so **keep the operator logged in**.

## Failover / failback

Identical model to the mac-site — lease + tunnel + shared DB. The switch is
[`scripts/aaf-site`](../../scripts/aaf-site) and runs **inside WSL** (it is a zsh
script, hence the `zsh` prereq):

```bash
./scripts/aaf-site status          # lease + mirror + local containers + cloud apps awake?
./scripts/aaf-site local           # FAILBACK here (sync shares cloud→local, lease, cloud sleeps, compose up)
./scripts/aaf-site cloud           # FAILOVER to the cloud (compose down, sync local→cloud, lease, cloud wakes)
./scripts/aaf-site cloud --force   # EVACUATION failover (skips the share sync — loud warning)
```

The lease KV secret `platform-active-site` still names exactly one live site;
`boot-guard.sh` backstops the reboot hole (`restart: unless-stopped` bringing
containers back while the lease moved away). The full lease / tunnel-connector /
shared-DB model in the self-hosted-primary ADR applies verbatim to the Windows
host.

## Differences from mac-site

| Aspect | mac-site (arm64 host) | windows-site |
|---|---|---|
| Image architecture | arm64 side tags (e.g. `100-arm64`) | **amd64** tags (e.g. `100`) — the only `.env` difference |
| Host scheduler | macOS `launchd` (5 `*.plist`) | Windows **Task Scheduler** (5 `tasks/*.xml`) firing `wsl.exe` |
| Where scripts run | natively on macOS | inside **WSL2 Ubuntu** (scripts unchanged) |
| `scripts/aaf-site` shell | `/bin/zsh` (built in) | `zsh` (install: `sudo apt install zsh`) |
| Power / always-on | `pmset` / "prevent sleep" | `powercfg /change standby-timeout-ac 0` + `hibernate-timeout-ac 0` |
| Split-brain notify | `osascript` notification | notification is a no-op in WSL; check `~/aaf/logs/boot-guard.log` |
| Compose / bash / secrets | **shared** — from `deploy/mac-site/` | **shared** — same files, run from WSL |
