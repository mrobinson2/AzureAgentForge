# AAF — Entra Identity Migration & Vulnerability Remediation Plan

**Date:** 2026-06-26 · **Window:** 2 days · **Status:** Plan — not executed.
**Scope decisions (confirmed):** Hermes Python fixes via **build-time override** in AAF's Dockerfile (no submodule edit); Node criticals fixed **defense-in-depth in a Hermes fork** even though AAF doesn't ship them; Entra identity covers **both layers** (services → managed identity / workload identity, agent personas → Entra Agent ID); target **dev + prod**.

> **Command safety legend:** 🟢 safe/idempotent (read-only or re-runnable, no destructive effect) · 🟠 **requires explicit approval** before running (mutates live Azure/GitHub/prod or is hard to reverse). Every 🟠 step names its blast radius.

---

## 1. Objective & success criteria

**Goal 1 — Entra identity migration. Done when:**
- Every AAF Container App authenticates to Azure resources (ACR, Key Vault, PostgreSQL, AI Foundry) via **managed identity / workload identity** — no password/key in the connection path. PostgreSQL `password_auth_enabled` can be set `false` in dev with all services still healthy.
- Each of the **14 agent personas** has a governed **Entra Agent ID** with a named human sponsor and an entry in a central inventory; lifecycle (create/disable) is documented and reproducible.
- Static third-party keys exist **only in Key Vault** (referenced by MI) and are **removed from GitHub Actions secrets**; the `seed` job no longer carries them.
- The **two-app OIDC least-privilege split is preserved and actually wired into `deploy.yml`**; the `deploy-destroy` gate still fires only on destructive plans.

**Goal 2 — Vulnerability remediation. Done when:**
- All **11 Python** packages in the findings doc are at fix versions in the **built AAF image** (verified in-image), via build-time override; base image is Python ≥3.13 where required.
- All **Node** advisories (`undici`, `ws`, `protobufjs` high; `baileys` critical) are resolved in the Hermes fork; AAF documents + CI-asserts that it never builds the Node surface.
- **Container OS layer** is scanned (Trivy) for every AAF base image and findings are triaged/remediated.
- A **recurring scan is gated in CI** (Dependabot + OSV/Trivy source scan + Trivy image scan) so this can't silently regress.
- **Re-scan is clean** (or every residual is an accepted, documented risk).

**Definition of done for the window:** both goals complete on **dev**; **prod** cutover staged with its gate (see §6 realism note — prod password-auth-disable and the shared-Foundry-key rotation are scheduled but soak-gated).

---

## 2. Current-state inventory (as found in the repo)

### 2.1 Identities
- **CI/CD OIDC (designed two-app split, partially landed):**
  - `github-aaf-deploy` — Contributor @ sub + Storage Blob Data Contributor @ state RG. Intended for **apply only**. Repo var `AZURE_CLIENT_ID`.
  - `github-aaf-deploy-plan` — Reader @ sub + Storage Blob Data Contributor @ state RG. Intended for **build/seed/plan/smoke**. Repo var `AZURE_CLIENT_ID_PLAN`.
  - **Drift:** `provision.sh` creates both apps, but the repo's `.github/workflows/deploy.yml` still sets `ARM_CLIENT_ID` / every `azure/login` to `vars.AZURE_CLIENT_ID` for **all** jobs. The per-job split is **not yet wired** (Task A1).
  - OIDC IDs are repo **Variables**, not Secrets (setting them as Secrets breaks `azure/login`).
- **Runtime (already MI-based — strong starting point):** all 7 Container Apps have a UAMI (`hermes, honcho, paperclip, memory_governor, teams_bridge, cloudflared, apps`). Each has **AcrPull** (image pull via MI) and **Key Vault Secrets User** (secret refs via MI). **Hermes already has `Cognitive Services OpenAI User`** (Foundry via MI). Secrets are KV-referenced with `identity = <uai>.id`.
- **PostgreSQL Flexible Server:** `active_directory_auth_enabled = true` **and** `password_auth_enabled = true` — **Entra auth is already on**; services still connect via password connection strings.
- **Agent personas (Entra Agent ID inventory, 14):** orchestrator, planner, researcher, coder, qa, business, psychology, cost-guardian, security, strategy, coach, infrastructure, generalist, curator (`agents/profiles/*.yaml`).

### 2.2 Static secrets in CI (the `seed` job env → KV)
`AI_FOUNDRY_API_KEY, OPENAI_API_KEY, CLAUDE_API_KEY, BRAVE_SEARCH_API_KEY, TELEGRAM_BOT_TOKEN, DISCORD_BOT_TOKEN, CF_TUNNEL_TOKEN, POSTGRES_CONNECTION_STRING, PAPERCLIP_DB_URL`. All already land in Key Vault and are consumed by MI; the gap is the **CI-held copy** and the **password-based DB strings**.

### 2.3 Vulnerable dependencies (per findings doc, target `NousResearch/hermes-agent@a4091e4`)
- **`apps/hermes/src` is a git submodule** → `github.com/NousResearch/hermes-agent.git`, pinned `a91a57f` (`v2026.5.16`). Dep changes **cannot be committed in the AAF parent repo**.
- **AAF builds Hermes Python-only:** `services/agent-runtime/Dockerfile` = `python:3.12-slim`, `pip install ".[messaging,honcho,cron]"`. **No `npm`, no `website/`, no `scripts/whatsapp-bridge/`** in the image → the Node criticals are **not in AAF's shipped artifact**.
- **Python (11, all fixes available):** aiohttp 3.13.4→**3.14.1**, starlette 1.0.1→**1.3.1**, tornado 6.5.5→**6.5.7**, python-multipart 0.0.27→**0.0.31**, cryptography 46.0.7→**48.0.1**, pynacl 1.5.0→**1.6.2**, pydantic-settings 2.13.1→**2.14.2**, msgpack 1.1.2→**1.2.1**, cbor2 5.8.0→**5.9.0**, pygments 2.19.2→**2.20.0**, pytest 9.0.2→**9.0.3** (dev). Priority cluster: **aiohttp/starlette/tornado/python-multipart** (request path / DoS).
- **Node (upstream Hermes only):** `website/` — undici (high), ws (high) + 31 mod/low (`npm audit fix` compatible). `scripts/whatsapp-bridge/` — **baileys (CRITICAL, no non-breaking fix)**, protobufjs (high), ws (high).
- **Base images (OS layer, unscanned):** `python:3.12-slim` (agent-runtime, memory-governor, model-router, teams-bridge, watchdog), `python:3.13-slim-bookworm` + `uv:0.9.24` (honcho), `node:${NODE_VERSION}` + `python:3.13-slim` (paperclip). Upstream Hermes image (unused by AAF): `uv:0.11.6-python3.13-trixie` + `debian:13.4`.
- **Interpreter note:** AAF's agent-runtime is **3.12**; several Python fixes target ≥3.13 → bump that base to `python:3.13-slim` (Task B1).

---

## 3. Scope & decisions

### 3.1 The `baileys` decision (surfaced explicitly)
`baileys` (CRITICAL) lives in `scripts/whatsapp-bridge/`, which **AAF never builds or ships** (Python-only image). **AAF runtime risk from baileys today: none.** Per the confirmed decision we still remediate **defense-in-depth in the Hermes fork** (upgrade to the patched `baileys` major + retest the bridge) **and** assert in AAF CI that the Node surface is never built. We do **not** add the WhatsApp bridge to AAF. If a future AAF feature needs WhatsApp, the breaking `baileys` major must be validated first.

### 3.2 Per-secret classification

| Secret | Nature | Target | Action |
|---|---|---|---|
| `AI_FOUNDRY_API_KEY` | Azure AI Foundry (Azure OpenAI) | **Managed identity** | Extend `Cognitive Services OpenAI User` to router/governor/honcho (Hermes done); drop key from KV+CI after MI verified |
| `OPENAI_API_KEY` (honcho) | Verify endpoint | **MI if Azure OpenAI; else Key Vault** | If Azure → MI as above; if api.openai.com → KV only + remove CI copy |
| `CLAUDE_API_KEY` | Third-party (Anthropic) | **Key Vault** | Already KV; **remove CI copy** (seed out-of-band) |
| `BRAVE_SEARCH_API_KEY` | Third-party | **Key Vault** | Already KV; **remove CI copy** |
| `TELEGRAM_BOT_TOKEN` | Third-party | **Key Vault** | Already KV; **remove CI copy** |
| `DISCORD_BOT_TOKEN` | Third-party | **Key Vault** | Already KV; **remove CI copy** |
| `CF_TUNNEL_TOKEN` | Third-party (Cloudflare) | **Key Vault** | Already KV (cloudflared MI); **remove CI copy** |
| `POSTGRES_CONNECTION_STRING` | Azure PostgreSQL | **Managed identity** | Migrate honcho/governor to Entra token auth (AAD already enabled); remove KV secret + CI copy after cutover |
| `PAPERCLIP_DB_URL` | Azure PostgreSQL | **Managed identity** | Migrate paperclip to Entra token auth; remove after cutover |
| *(Storage account key — Azure Files SMB mount)* | Azure Storage | **Stays static** | Azure Files SMB mount can't use MI; mitigate via rotation + scope; document residual |

**Principle:** a third-party API key cannot "become" a managed identity — its migration is **Key Vault + MI reference + remove from CI**. Only Azure-resource auth (PostgreSQL, Foundry, ACR, KV) converts to MI.

---

## 4. Workstream A — Identity migration

> Each task: **Steps · Validation · Rollback · Risk.** Default target **dev**; prod repeats gated (§5).

### A1 — Land the two-app OIDC split into `deploy.yml`
- **Steps:** 🟢 Confirm both apps + `vars.AZURE_CLIENT_ID_PLAN` exist (`provision.sh` already creates them; re-run `./provision.sh` is idempotent). 🟢 Edit `deploy.yml`: remove the top-level `ARM_CLIENT_ID`; set it per-job — `build/seed/plan/smoke` → `vars.AZURE_CLIENT_ID_PLAN`, `apply` → `vars.AZURE_CLIENT_ID`; point each `azure/login` `client-id` at the matching var. 🟠 Merge to `main` (the workflow is `workflow_dispatch`-only, so it won't fire on the PR).
- **Validation:** 🟢 `terraform validate`/yaml lint; 🟠 dispatch a **non-destructive** dev run — plan/seed/smoke succeed under the plan identity, apply under the privileged one; confirm no job uses Contributor pre-gate.
- **Rollback:** revert the `deploy.yml` commit (single file); apps unchanged.
- **Risk:** Low. Main failure mode = plan identity missing a narrow data role (grant the specific scoped role, never Contributor).

### A2 — Remove third-party keys from CI; seed Key Vault out-of-band
- **Steps:** 🟠 One-time seed of KV from a trusted admin shell (not CI): `scripts/seed-keyvault.sh -v <vault>` with the keys in the local env / `secrets.env` (idempotent). 🟢 Edit `deploy.yml` `seed` job to stop referencing the third-party `secrets.*` (keep the job for internal/generated secrets only, or gate it off once KV is populated). 🟠 `gh secret delete` the migrated third-party secrets from the repo.
- **Validation:** 🟢 `az keyvault secret show` each name resolves; 🟠 dev redeploy → apps start, KV refs resolve via MI; 🟢 `gh secret list` shows the keys gone.
- **Rollback:** re-add the secret via `gh secret set` and re-run seed.
- **Risk:** Medium — deleting a CI secret before KV holds the value breaks seed. **Order matters:** seed KV → verify → then delete CI secret.

### A3 — PostgreSQL: password connection strings → Entra (MI) auth
- **Steps:** 🟢 AAD auth already enabled. 🟠 Set an **Entra admin** on the server (`azurerm_postgresql_flexible_server_active_directory_administrator` or `az postgres flexible-server ad-admin create`). 🟠 For each app UAMI (honcho, paperclip, memory_governor) create a DB role mapped to the MI and `GRANT` least-privilege on its DB. **Build-time/app override** so each service connects with an Entra **access token** (AAD `https://ossrdbms-aad.database.windows.net` token via `DefaultAzureCredential`) instead of the password DSN — honcho & paperclip are submodules → apply via the same override mechanism as B1 (no submodule edit). 🟠 Flip those apps to MI auth (drop the `postgres-connection-string` / `paperclip-db-url` env, add `PGHOST/PGUSER=<uami-name>` + token sidecar/refresh).
- **Validation:** 🟠 each app reads/writes its DB under MI; 🟢 `SELECT` as the MI role works, password DSN no longer referenced; once soaked, 🟠 set `password_auth_enabled = false` (dev) and confirm health.
- **Rollback:** re-enable password auth + restore the KV DSN env (kept until soak passes).
- **Risk:** **High** — token lifetime/refresh, role grants, and submodule app-code changes. Keep password auth as the fallback until a clean soak. This is the most failure-prone task; do it on dev with the DSN still seeded.

### A4 — Extend Foundry managed-identity auth (drop the Foundry key path)
- **Steps:** 🟢 Hermes already uses `Cognitive Services OpenAI User`. 🟠 Add the same role assignment for model-router / memory-governor / honcho UAMIs; 🟠 switch those services' Azure-OpenAI calls to `DefaultAzureCredential` token auth (override). Verify whether `OPENAI_API_KEY` is Azure or api.openai.com (per §3.2) — MI only applies to Azure.
- **Validation:** 🟠 a chat/embedding call succeeds with the key removed; 🟢 confirm role assignment via `az role assignment list`.
- **Rollback:** restore the KV key env (kept until verified).
- **Risk:** Medium — only Azure OpenAI endpoints qualify; api.openai.com stays a KV key.

### A5 — Entra Agent ID for the 14 agent personas
- **Steps:** 🟠 Register an **Entra Agent ID** per persona (orchestrator … curator) using the GA (Apr 2026) flow; assign a **named human sponsor** per agent; record in a central inventory (new `agents/ENTRA-AGENT-INVENTORY.md` + the agent-id in each `agents/profiles/<name>.yaml`). 🟠 Wire the agent's Entra identity into its runtime auth where it makes outbound governed calls (least-privilege; no standing secrets). 🟢 Document lifecycle (create/disable/rotate sponsor).
- **Validation:** 🟢 inventory lists 14 agents, each with id + sponsor; 🟠 a test agent authenticates via its Entra Agent ID; disable flow revokes access.
- **Rollback:** disable the Agent IDs; personas fall back to the service UAMI path (no runtime dependency added until verified).
- **Risk:** **High/uncertain** — newest capability, tooling maturity unknown. **Most likely deferral candidate** if the GA flow is fiddly (see §5).

---

## 5. Workstream B — Vulnerability remediation

### B1 — Python deps: build-time override + base bump (the in-scope fix)
- **Steps:** 🟢 Add `services/agent-runtime/constraints.txt` pinning the 11 fix versions; 🟢 change the Dockerfile base `python:3.12-slim` → `python:3.13-slim`; 🟢 change `pip install ".[messaging,honcho,cron]"` → `pip install -c constraints.txt ".[messaging,honcho,cron]"` (constraints win over transitive resolution; no submodule edit). 🟢 Also bump `apps/hermes/src` `pyproject.toml`/`uv.lock` **in the Hermes fork** (B4) for lockfile parity + dev.
- **Validation:** 🟢 `docker build` the image; 🟢 `pip list` / `pip freeze` in-image shows all 11 at fix versions; 🟢 run Hermes' Python tests in the builder stage; 🟠 dev deploy + smoke.
- **Rollback:** revert constraints + base (one Dockerfile); image rebuilds to prior state.
- **Risk:** Medium — 3.12→3.13 base + cryptography/pynacl native wheels; the constraints approach is idempotent and reversible.

### B2 — Container OS-layer scan (Trivy) + remediate
- **Steps:** 🟢 `trivy image` each built AAF image (agent-runtime, honcho, paperclip, model-router, memory-governor, watchdog, teams-bridge); 🟢 triage OS-package CVEs; 🟢 remediate via base-image bump / `apt-get upgrade` pin in the relevant Dockerfile.
- **Validation:** 🟢 re-`trivy image` → no unaccepted HIGH/CRITICAL; 🟢 rebuild + tests pass.
- **Rollback:** revert the Dockerfile base/pin.
- **Risk:** Low–Medium — base bumps can shift transitive system libs; rebuild + smoke covers it.

### B3 — Node defense-in-depth (Hermes fork) + AAF not-built assertion
- **Steps:** 🟠 In the **Hermes fork** (B4): `cd website && npm audit fix` (undici, ws + the 31); `cd scripts/whatsapp-bridge && npm audit fix` (protobufjs, ws) + **`baileys` major upgrade** (breaking — retest the bridge). 🟢 In **AAF**: add a CI assertion that `services/agent-runtime/Dockerfile` contains no `npm`/`website`/`whatsapp-bridge` build step (fail the build if it ever does).
- **Validation:** 🟢 `npm audit` clean in both Node trees (fork); 🟢 AAF CI guard passes on current Dockerfile and fails on an injected `npm install`.
- **Rollback:** fork branch is independent; AAF guard is one CI step to revert.
- **Risk:** Medium — `baileys` major is breaking; isolated to the fork, zero AAF runtime impact.

### B4 — Hermes fork (carrier for B1 lock parity + B3)
- **Steps:** 🟠 Fork `NousResearch/hermes-agent` → your org; branch `aaf-sec-bumps`; apply Python (`pyproject.toml`/`uv.lock`) + Node fixes; keep AAF's submodule **URL/pin unchanged for runtime** (AAF runtime fix is the build-time override, B1) — the fork carries parity + the Node defense-in-depth + a clean upstream-PR basis.
- **Validation:** 🟢 fork CI / `uv sync` + tests green.
- **Rollback:** n/a (additive fork).
- **Risk:** Low — does not touch AAF runtime.

### B5 — Recurring scanning gated in CI
- **Steps:** 🟢 Enable **Dependabot** alerts + security updates on the AAF repo; 🟢 add an **OSV-Scanner** (or Trivy fs) job on source; 🟢 add a **Trivy image scan** on the `build` job's output, gated like other checks; 🟢 (optional) Black Duck Detect step for license/BDSA.
- **Validation:** 🟢 a deliberately-pinned-old dep fails the gate.
- **Rollback:** remove the workflow steps.
- **Risk:** Low.

---

## 6. Day-by-day schedule

> Front-loads CRITICAL/HIGH. Identity work that touches running apps (A3/A4) follows the vuln fixes so the image is already patched when redeployed. Prod repeats dev, gated.

**Day 1 — Vulnerability remediation + identity groundwork (low-blast-radius first)**
- **Block 1 (AM): Python + base + image scan** — B1 (constraints + 3.13 base + rebuild + in-image verify), B2 (Trivy scan of all images), B4 (fork created). 🟠 only at dev deploy. *Highest-severity in-scope items first.*
- **Block 2 (AM/Mid): Node defense-in-depth** — B3 (fork `npm audit fix` + `baileys` major + retest; AAF not-built CI guard). B5 (Dependabot + OSV/Trivy CI gates).
- **Block 3 (PM): OIDC split + CI-secret removal** — A1 (wire two-app split into `deploy.yml`), A2 (seed KV out-of-band → verify → delete CI third-party secrets). **Order:** seed+verify *before* any `gh secret delete`.
- **End-of-Day-1 gate:** 🟠 one dev `workflow_dispatch` (non-destructive) — patched images deploy under the split identities; smoke green; re-scan source/images clean.

**Day 2 — Resource-auth MI migration + agent identity + prod staging**
- **Block 4 (AM): Postgres → Entra MI (dev)** — A3 (Entra admin, MI DB roles, token-auth override for honcho/paperclip/governor) with password auth still on as fallback. *Most failure-prone; do it with the DSN still seeded.*
- **Block 5 (Mid): Foundry MI extension** — A4 (role assignments + token auth for router/governor/honcho; drop Foundry key where Azure).
- **Block 6 (PM): Entra Agent ID** — A5 (register 14 personas, sponsors, inventory). *Timeboxed; deferral candidate (§ realism).*
- **Block 7 (PM): Prod cutover (gated)** — 🟠 repeat A1–A4 + B1/B2 deploy on prod via the `deploy-destroy`-gated pipeline. **Shared AI-Foundry-key rotation needs an operator-prod maintenance window** — coordinate; do not rotate mid-day unscheduled.
- **End-of-Day-2 gate:** dev fully on MI (password auth off after soak); prod patched + identity-split live; full re-scan clean; exit checklist signed.

**Approval gates in the schedule:** every dev/prod `apply` (the existing `deploy-destroy` gate, destructive-only); `gh secret delete` (A2); `password_auth_enabled=false` (A3); Entra admin + MI DB roles (A3); prod cutover block (A7); Foundry key rotation (prod window).

### Realism — what fits, what defers
Two full goals across **dev + prod**, with **both Entra layers**, **Postgres MI**, and **defense-in-depth Node**, is **more than 2 clean days** if anything resists. Honest prioritization:
- **Will fit (commit to these):** all vuln remediation (B1–B5) on dev + dev image rebuild; the OIDC split (A1) + CI-secret removal (A2); Foundry MI extension (A4); the Hermes fork.
- **At risk / likely partial:** **A3 Postgres→MI** (token-auth app changes across two submodule services + soak before disabling password auth) and **A5 Entra Agent ID** (newest GA tooling). Recommend: land A3 on dev with password auth **kept on** (don't disable in-window); pilot **A5 on 2–3 personas**, roll the remaining 11 as a fast-follow.
- **Defer out of the window (recommended):** **prod password-auth-disable** and **prod Foundry-key rotation** — both need a scheduled maintenance window + a dev soak first. Stage prod's identity-split + patched images in-window; finish prod's MI cutover in the next window. **Rationale:** prod data-plane auth changes without a soak are the highest-regret action here.

---

## 7. Risk register & rollback summary

| # | Risk | Likelihood | Impact | Mitigation / Rollback |
|---|---|---|---|---|
| R1 | Postgres MI token/refresh or role grant wrong → apps can't reach DB | Med | High | Keep `password_auth_enabled=true` + KV DSN until soak; A3 rollback = restore DSN env |
| R2 | Deleting a CI secret before KV holds it → seed/deploy breaks | Med | High | Strict order: seed→verify→delete; rollback = `gh secret set` |
| R3 | 3.12→3.13 base or constraint pin breaks a native dep | Med | Med | In-image tests in builder stage; revert one Dockerfile |
| R4 | `baileys` major breaks the WhatsApp bridge | Med | Low (AAF) | Isolated to fork; AAF doesn't ship it |
| R5 | Plan identity missing a narrow data role after split | Med | Low | Grant the specific scoped role (AcrPush/KV Officer), never Contributor; rollback = revert deploy.yml |
| R6 | Entra Agent ID GA flow immature → time sink | Med | Med | Timebox; pilot 2–3 personas; defer remainder; personas keep service-UAMI path |
| R7 | Prod cutover without soak → outage | Low | High | Defer prod data-plane disable to a window; stage only |
| R8 | Shared Foundry key rotation hits operator prod | Med | Med | Coordinate maintenance window; two-key rotation (add new, swap, retire old) |
| R9 | Storage-account key can't go MI (Azure Files SMB) | — | Low | Accept residual; rotate + scope; document |

**Global rollback:** all changes land via PR + the saved-plan `apply`; every step keeps its prior credential path (KV DSN, CI secret, password auth, Foundry key) **until the MI path is verified**, so any single task reverts independently without a coordinated rollback.

---

## 8. Exit checklist — every finding mapped to a task + re-scan

| Finding (from `Hermes_SCA_Scan_Findings.md`) | Remediation task | Verify |
|---|---|---|
| aiohttp 3.13.4→3.14.1 | B1 (override) + B4 (fork lock) | `pip freeze` in-image |
| starlette 1.0.1→1.3.1 | B1 + B4 | in-image |
| tornado 6.5.5→6.5.7 | B1 + B4 | in-image |
| python-multipart 0.0.27→0.0.31 | B1 + B4 | in-image |
| cryptography 46.0.7→48.0.1 | B1 + B4 | in-image |
| pynacl 1.5.0→1.6.2 | B1 + B4 | in-image |
| pydantic-settings 2.13.1→2.14.2 | B1 + B4 | in-image |
| msgpack 1.1.2→1.2.1 | B1 + B4 | in-image |
| cbor2 5.8.0→5.9.0 | B1 + B4 | in-image |
| pygments 2.19.2→2.20.0 | B1 + B4 | in-image |
| pytest 9.0.2→9.0.3 (dev) | B4 (fork dev dep) | fork CI |
| website: undici (high) | B3 (fork audit fix) | `npm audit` |
| website: ws (high) + 31 mod/low | B3 | `npm audit` |
| whatsapp-bridge: **baileys (CRITICAL)** | B3 (fork major upgrade) + §3.1 not-built | `npm audit` + AAF CI guard |
| whatsapp-bridge: protobufjs (high) | B3 | `npm audit` |
| whatsapp-bridge: ws (high) | B3 | `npm audit` |
| Container OS layer (debian/node/uv/slim bases) | B2 (Trivy all images) | `trivy image` clean |
| Point-in-time / recurring | B5 (Dependabot + OSV + Trivy gates) | gate fails on stale dep |

**Final verification:** 🟢 re-run the SCA (OSV/Trivy fs) on source + `trivy image` on every built image + `npm audit` on the fork → clean or accepted-and-documented; confirm dev apps healthy on MI; confirm `gh secret list` no longer holds migrated third-party keys; confirm `deploy.yml` uses the split identities and the `deploy-destroy` gate still guards destructive plans.

---

## 9. Open items for the operator
- Confirm `OPENAI_API_KEY` endpoint (Azure OpenAI → MI, or api.openai.com → KV-only).
- Confirm the operator-prod maintenance window for the shared AI-Foundry-key rotation.
- Confirm Entra Agent ID tenant prerequisites (the GA flow may need a specific Entra role/license) before A5.
