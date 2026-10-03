# AzureAgentForge — provisioning & first deploy

This folder contains a hardened, idempotent version of the operator's handoff for wiring up
OIDC + the Terraform state backend + repo config so `deploy.yml` can run end to end.

**Why a script instead of me running it:** these commands act on your live Azure subscription
and GitHub repo. I can't reach your authenticated `az` / `gh` sessions from here, so the
deliverable is something you run yourself in a shell where you've already done `az login` and
`gh auth login`. The script is safe to re-run — it reuses what already exists instead of
creating duplicates.

## Files
- `provision.sh` — does steps 3a–3g (state backend, **two** OIDC identities, federated creds,
  least-privilege role assignments, repo **Variables**, repo **Secrets**, the `deploy-destroy`
  approval environment, and an optional workflow trigger).
- `secrets.env.example` — copy to `secrets.env`, fill in only the API keys you use.
- `.gitignore` — keeps `secrets.env` and `.provision.state` out of git.

## Prerequisites
- `az login` as a user who is **Owner** or **User Access Administrator** on subscription
  `307383d9-…` (step 3c creates role assignments, which needs that).
- `gh auth login` with admin rights on `mrobinson2/AzureAgentForge` (to set Variables/Secrets
  and the environment).
- Azure CLI and GitHub CLI installed.

## Run
```bash
cd /path/to/AzureAgentForge
cp secrets.env.example secrets.env      # optional — fill in the keys you have
chmod +x provision.sh

./provision.sh                 # provisions everything; does NOT trigger a deploy
./provision.sh --deploy        # also triggers deploy.yml and watches the run (3g)
./provision.sh --no-secrets    # skip the secrets step
```

## The one correction that matters
The Azure identifiers (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
`TFSTATE_*`) are set as repo **Variables** (`vars.*`), **not Secrets**. The deploy workflow
reads `vars.*`; setting them as Secrets makes OIDC silently fail at `azure/login`. The script
uses `gh variable set` for these and reserves `gh secret set` for the API keys in 3e.

## Least-privilege identity split

Instead of one service principal with subscription **Contributor** that every job uses, there
are now two OIDC identities:

- **`github-aaf-deploy` (privileged)** — roles: Contributor @ subscription + Storage Blob Data
  Contributor @ state RG. Used by **only the `apply` job**, which is the only job that mutates
  infrastructure. Repo variable: `AZURE_CLIENT_ID`.
- **`github-aaf-deploy-plan` (low-privilege)** — roles: Reader @ subscription + Storage Blob
  Data Contributor @ state RG. Used by **build / seed / plan / smoke** — the jobs that run
  un-gated on every dispatch and pull in third-party actions and dependencies. It can read
  resources and read/write Terraform state, but cannot create, change, or delete infrastructure.
  Repo variable: `AZURE_CLIENT_ID_PLAN`.

Your auto-apply behavior is unchanged: non-destructive plans still apply automatically, and the
`gate` job still requires approval for destructive plans. The win is that Contributor is no
longer exposed to the dependency-heavy pre-approval jobs — a poisoned dependency in plan/build
can't escalate to subscription Contributor; only the minimal `apply` job (which just applies the
already-reviewed saved plan) holds that power.

`deploy.yml` was updated to match: `ARM_CLIENT_ID` is now set per-job (plan/smoke → low-priv,
apply → privileged), and each `azure/login` uses the matching client id.

### Rollout order (important)
The updated `deploy.yml` references `vars.AZURE_CLIENT_ID_PLAN`, which won't exist until you
run `provision.sh`. So: **run `provision.sh` first** (it creates the plan identity and sets the
variable), *then* commit/merge the `deploy.yml` change. The deploy workflow is
`workflow_dispatch`-only, so it won't fire on the PR — just make sure the variable exists before
your next manual dispatch from `main`.

### When you later enable the registry / Key Vault
`build` (image push) and `seed` (secret write) skip today because `CONTAINER_REGISTRY_NAME` /
`KEY_VAULT_NAME` are unset. When you enable them, the low-priv identity needs two narrow,
resource-scoped data roles (commands are in `provision.sh`, step 3c comments):
`AcrPush` on the registry, and `Key Vault Secrets Officer` on the vault. Keep these scoped to
the specific resources — don't fall back to Contributor. Also note: if a `terraform plan` ever
fails because it reads a data-plane resource (e.g. a Key Vault secret data source, or storage
account keys), grant the plan identity the one narrow role that read needs rather than widening
it broadly.

## Hardening applied on top of the raw handoff
- **Storage account name is captured and reused.** `staaftfstate$RANDOM` only lived in the
  shell that ran 3a, so 3d referenced a `$SA` that was gone in a new session. The script writes
  the chosen name to `.provision.state` and reuses it on re-run (also reuses any existing
  `staaftfstate*` account in the RG).
- **Container creation uses shared-key, not `--auth-mode login`.** Control-plane Owner/
  Contributor does **not** grant blob *data-plane* access, so `--auth-mode login` frequently
  fails right after creation with `AuthorizationPermissionMismatch`. The script uses
  `--auth-mode key` and only falls back to AAD (granting yourself the data role + RBAC-
  propagation retries) if shared key is disabled.
- **Role assignments use the SP object id** (`--assignee-object-id … --assignee-principal-type
  ServicePrincipal`) with a retry loop, avoiding the Graph-lookup and AAD-replication races that
  make `--assignee <appId>` fail intermittently right after `az ad sp create`.
- **No duplicate app registrations.** Re-running `az ad app create` would mint a new app each
  time; the script looks up `github-aaf-deploy` by display name and reuses it. Federated creds
  are checked before creation.
- **Secrets don't hang the script.** 3e reads values from your environment or `secrets.env` and
  only sets the non-empty ones, printing what it skipped — no interactive prompt mid-run.
- **3f via API.** The `deploy-destroy` environment + your required-reviewer is created with
  `gh api` (idempotent). UI alternative: repo **Settings → Environments → New environment →**
  name it exactly `deploy-destroy` → add yourself under **Required reviewers**.

## Caveats the pipeline won't warn you about (from the operator note)
- **Only 3 of 6 images build today** — `model-router`, `memory-governor`, `watchdog` are
  self-contained and deploy; `paperclip`, `honcho`, `agent-runtime` are skipped (logged reason)
  until upstream sources are vendored into `apps/`. Infra + the 3 services come up; the full
  stack won't until vendoring.
- **Key Vault bootstrap is two-pass** — the keyvault module reads its own
  `postgres-admin-password`. If the first `apply` errors on that secret, just re-run once.
  (Documented in `docs/deploy-pipeline.md`.)

## Manual / decision items (not provisioning — they're genuinely yours)
1. **Discord MESSAGE CONTENT toggle** — portal only, no API. Discord Developer Portal → your
   bot's application → **Bot → Privileged Gateway Intents** → enable **MESSAGE CONTENT INTENT**
   (and **SERVER MEMBERS INTENT**) → Save → reinstall the plugin. The code now fails loud with a
   4014 error if the intent is still off.
2. **PRs #13 and #14** — review gate on a public repo. Review, then squash-merge when satisfied:
   ```bash
   gh pr view 13 -R mrobinson2/AzureAgentForge --web   # router tests 25 → 146
   gh pr view 14 -R mrobinson2/AzureAgentForge --web   # observability alerts + workbook
   gh pr merge 13 -R mrobinson2/AzureAgentForge --squash
   gh pr merge 14 -R mrobinson2/AzureAgentForge --squash
   ```
