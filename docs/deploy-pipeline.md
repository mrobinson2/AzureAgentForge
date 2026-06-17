<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Reference deploy pipeline

`.github/workflows/deploy.yml` is a **reference** GitHub Actions pipeline you wire
up against your own Azure subscription. It is intentionally *not* run by this
repo's own CI: the public repo holds no cloud credentials, so CI stays
validate-only (`fmt`, `validate`, `compose config`, tests). This workflow is the
piece you add when you fork and deploy for real.

## The one idea worth copying: a destroy-aware approval gate

Most Terraform pipelines either apply everything unattended (fast, but one bad
plan nukes your data) or gate *every* apply behind a human (safe, but you
rubber-stamp dozens of harmless image bumps until the click means nothing).

This pipeline gates **only when it matters**:

```
plan ──► detect-destroy ──► apply
                │
                └─ destructive? ──► human approval (GitHub Environment) ──► apply
```

- A plan that only **adds or updates** resources (a new container, an image-tag
  bump) applies **unattended**.
- A plan that would **delete or replace** any resource **blocks** on a required
  reviewer before apply.

"Destructive" is decided by `installer/detect_destroy.py`, which calls
`installer.core.plan_has_destroy`, the *same* function the
[Forge Console](../installer/README.md) GUI uses. A resource counts as destroyed
if its `terraform show -json` actions contain `delete`: a pure `["delete"]` and
both replace orderings (`["delete","create"]`, `["create","delete"]`) all gate.
If the plan JSON can't be parsed, the detector **fails safe** and routes to
manual approval rather than auto-applying something it couldn't read.

The apply job uses a `needs` + `if` split so the gate is skipped on
non-destructive plans (and apply still runs via `if: always()` with explicit
result checks). It applies the **saved plan file** verbatim, so what a reviewer
approved is exactly what is applied, with no re-plan in between.

## One-time setup

### 1. Federated (OIDC) Azure credentials - no stored secrets

Create an Entra ID app registration + service principal, grant it `Contributor`
(and `User Access Administrator` if you deploy the RBAC role assignments) on the
target subscription, then add a **federated credential** for GitHub Actions:

```bash
az ad app create --display-name "aaf-deploy"
APP_ID=$(az ad app list --display-name "aaf-deploy" --query '[0].appId' -o tsv)
az ad sp create --id "$APP_ID"
az role assignment create --assignee "$APP_ID" --role Contributor \
  --scope "/subscriptions/<SUBSCRIPTION_ID>"

# Trust this repo's workflow runs (scope the subject as tightly as you can):
az ad app federated-credential create --id "$APP_ID" --parameters '{
  "name": "aaf-deploy-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:<OWNER>/<REPO>:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

For tighter control, federate on the environment instead
(`subject: repo:<OWNER>/<REPO>:environment:deploy-destroy`).

### 2. GitHub repository **variables** (Settings → Secrets and variables → Actions → Variables)

OIDC means these are non-secret identifiers, not credentials:

| Variable | Value |
|---|---|
| `AZURE_CLIENT_ID` | the app registration's `appId` |
| `AZURE_TENANT_ID` | your Entra tenant ID |
| `AZURE_SUBSCRIPTION_ID` | target subscription ID |
| `TFSTATE_RESOURCE_GROUP` | resource group holding the TF state storage account |
| `TFSTATE_STORAGE_ACCOUNT` | storage account name for remote state |
| `TFSTATE_CONTAINER` | blob container for state (e.g. `tfstate`) |
| `CONTAINER_REGISTRY_NAME` | optional. ACR name for the `build` job. Unset means an infra-only deploy with no image build. |
| `KEY_VAULT_NAME` | optional. Key Vault name for the `seed` job. Unset means seeding is skipped. |
| `SMOKE_URL` | optional. A URL the `smoke` job probes for a 2xx/3xx response. |

Create the state storage account once (any standard pattern works); the
pipeline passes these via `terraform init -backend-config=` so no real values
live in `backend.tf`.

External secrets the `seed` job reads come from repository **secrets** (not
variables), each named for the Key Vault secret upper-cased with underscores:
`CLAUDE_API_KEY`, `AI_FOUNDRY_API_KEY`, `OPENAI_API_KEY`, `BRAVE_SEARCH_API_KEY`,
`TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `CF_TUNNEL_TOKEN`,
`POSTGRES_CONNECTION_STRING`, `PAPERCLIP_DB_URL`. Set only the ones your
deployment uses; the rest are seeded as empty placeholders so every container's
Key Vault reference resolves (an unset model tier or surface stays inert until
you provide its value and re-run). `scripts/seed-keyvault.sh --list` prints the
full inventory.

### 3. The approval gate - a GitHub **Environment**

Create an environment named **`deploy-destroy`**
(Settings → Environments → New environment) and add yourself (or your team) under
**Required reviewers**. That environment is referenced only by the `gate` job,
so the approval prompt appears **only** on destructive plans. Optionally add a
wait timer or restrict it to protected branches.

## Running it

Actions → **Deploy (reference)** → **Run workflow**, choosing the environment,
profile (`cost-optimized` / `hardened`), and region. Then:

- **Non-destructive plan** → `plan` ✅ → `apply` runs unattended → `smoke`.
- **Destructive plan** → `plan` ✅ → `gate` waits for your approval (the run
  pauses; you'll see the resources to be deleted in the job summary) → on
  approval, `apply` → `smoke`. Reject, and `apply` is skipped.

## Build, seed, and smoke

Three jobs wrap the plan/apply core so a run goes from source to a checked
deployment.

**`build`** runs `scripts/build-and-push.sh`, which uses `az acr build` (the
image is built server-side inside ACR, so the runner needs no Docker daemon).
The resolved tag (short git SHA by default, or the `image_tag` input) feeds the
plan as `-var=<service>_image_tag`. Two classes of image exist:

- self-contained (`model-router`, `memory-governor`, `watchdog`) build from this
  repo alone.
- upstream-dependent (`paperclip`, `honcho`, `agent-runtime`) need their
  `apps/<project>/` sources vendored first (see
  [local development](local-development.md)). The job runs with
  `--skip-unbuildable`, so until you vendor those sources it builds what it can
  and skips the rest with a logged reason. `scripts/build-and-push.sh --list`
  shows the table.

**`seed`** runs `scripts/seed-keyvault.sh`, which idempotently ensures every
secret the Container Apps mount exists: internal secrets (JWT signing keys,
admin passwords, `postgres-admin-password`) are generated if absent; external
ones (provider keys, bot tokens, connection strings) come from the repository
secrets listed above. Existing secrets are left untouched unless `--force`.

**`smoke`** runs `scripts/smoke-test.sh`, which calls `az containerapp show` for
the deployed apps (and any `SMOKE_URL`) and pipes the result to
`installer.smoke`. An app that is not `provisioningState=Succeeded`, or a probe
that is not 2xx/3xx, exits non-zero and fails the run. The verdict logic is
unit-tested offline in `installer/tests/test_smoke.py`.

### First deploy: a one-time Key Vault bootstrap

The Key Vault module reads `postgres-admin-password` from the vault as a data
source (`infrastructure/modules/keyvault/main.tf`), so that secret must exist
before the first `plan` can resolve. The `seed` job covers steady state but
cannot seed a vault that does not exist yet. For the first deploy, create the
vault and seed the password once:

```bash
# 1. create just the resource group + vault
terraform -chdir=infrastructure/environments/dev apply \
  -target=azurerm_resource_group.main -target=module.keyvault

# 2. seed the bootstrap secret (and any others you have ready)
POSTGRES_CONNECTION_STRING=... scripts/seed-keyvault.sh -v <your-vault-name>

# 3. now run the full pipeline (or terraform apply) as normal
```

After that, every run's `seed` job keeps the vault current and the bootstrap is
not needed again.

## Notes & limits

- `terraform plan` reads remote state and talks to Azure, so the federated SP
  needs read access to the state storage account as well as the subscription.
- The saved-plan artifact is retained for 5 days; an apply must consume a plan
  produced by the same run (Terraform rejects a stale plan).
