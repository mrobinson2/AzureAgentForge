#!/usr/bin/env bash
#
# provision.sh — AzureAgentForge: Terraform state backend + GitHub OIDC + pipeline wiring
# -----------------------------------------------------------------------------------------
# Idempotent. Safe to re-run. Run from a shell where you have already done:
#     az login        (account must be Owner or User Access Administrator on the subscription,
#                       because step 3c creates role assignments)
#     gh auth login   (must have admin rights on the repo to set Variables/Secrets)
#
# Usage:
#     ./provision.sh                 # provision everything; secrets read from env / secrets.env
#     ./provision.sh --no-secrets    # skip the gh secret step entirely
#     ./provision.sh --help
#
# State: writes non-secret IDs (storage account, app id, SP object id) to .provision.state
#        so re-runs reuse the same resources instead of creating duplicates.
# -----------------------------------------------------------------------------------------

set -euo pipefail

# ---- Inputs (override via environment if you like) --------------------------------------
SUB="${SUB:-307383d9-1e15-4775-9250-66fd53eef2bf}"
TENANT="${TENANT:-992ecc7d-660a-4aa9-aa12-8e0e270e54d4}"
REPO="${REPO:-mrobinson2/AzureAgentForge}"
LOC="${LOC:-centralus}"

APP_DISPLAY_NAME="${APP_DISPLAY_NAME:-github-aaf-deploy}"            # privileged: apply only
PLAN_APP_DISPLAY_NAME="${PLAN_APP_DISPLAY_NAME:-github-aaf-deploy-plan}"  # low-priv: build/seed/plan/smoke
STATE_RG="${STATE_RG:-rg-terraform-state}"
TFSTATE_CONTAINER="${TFSTATE_CONTAINER:-tfstate}"
SA_PREFIX="${SA_PREFIX:-staaftfstate}"        # storage account names: <=24 chars, [a-z0-9] only

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="$SCRIPT_DIR/.provision.state"
SECRETS_FILE="$SCRIPT_DIR/secrets.env"

DO_SECRETS=1
DO_DEPLOY=0          # --deploy also triggers the deploy.yml workflow (3g)
DEPLOY_PROFILE="${DEPLOY_PROFILE:-cost-optimized}"
DEPLOY_ENV_INPUT="${DEPLOY_ENV_INPUT:-dev}"
for arg in "$@"; do
  case "$arg" in
    --no-secrets) DO_SECRETS=0 ;;
    --deploy)     DO_DEPLOY=1 ;;
    -h|--help)    sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# ---- Helpers ----------------------------------------------------------------------------
c_blue=$'\033[1;34m'; c_grn=$'\033[1;32m'; c_yel=$'\033[1;33m'; c_red=$'\033[1;31m'; c_off=$'\033[0m'
log()  { printf '%s==>%s %s\n' "$c_blue" "$c_off" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$c_grn" "$c_off" "$*"; }
warn() { printf '%s warn%s %s\n' "$c_yel" "$c_off" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }

save_state() { # save_state KEY VALUE
  touch "$STATE_FILE"
  grep -v "^$1=" "$STATE_FILE" > "$STATE_FILE.tmp" 2>/dev/null || true
  echo "$1=$2" >> "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
}
load_state() { [[ -f "$STATE_FILE" ]] && grep "^$1=" "$STATE_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true; }

# ---- 0. Preconditions -------------------------------------------------------------------
log "Checking prerequisites"
command -v az >/dev/null || die "az (Azure CLI) not found. Install: https://aka.ms/azure-cli"
command -v gh >/dev/null || die "gh (GitHub CLI) not found. Install: https://cli.github.com"
az account show >/dev/null 2>&1 || die "Not logged in to Azure. Run: az login"
gh auth status >/dev/null 2>&1 || die "Not logged in to GitHub. Run: gh auth login"

log "Selecting subscription $SUB"
az account set --subscription "$SUB" || die "Cannot select subscription $SUB"
CURRENT_TENANT="$(az account show --query tenantId -o tsv)"
[[ "$CURRENT_TENANT" == "$TENANT" ]] || warn "Logged-in tenant ($CURRENT_TENANT) != expected TENANT ($TENANT)"
ok "Subscription selected"

# =========================================================================================
# 3a. Terraform state backend (resource group + storage account + container)
# =========================================================================================
log "3a. Terraform state backend"
az group create -n "$STATE_RG" -l "$LOC" --subscription "$SUB" -o none
ok "Resource group $STATE_RG"

# Decide which storage account to use, in priority order:
#   1) the one recorded in .provision.state (if it still exists)
#   2) an existing account in the state RG whose name starts with $SA_PREFIX
#   3) a brand-new globally-unique name
SA="$(load_state SA)"
if [[ -n "$SA" ]] && az storage account show -n "$SA" -g "$STATE_RG" >/dev/null 2>&1; then
  ok "Reusing storage account from state: $SA"
else
  SA="$(az storage account list -g "$STATE_RG" --query "[?starts_with(name,'$SA_PREFIX')].name | [0]" -o tsv 2>/dev/null || true)"
  if [[ -n "$SA" ]]; then
    ok "Reusing existing storage account: $SA"
  else
    # find a free, valid, globally-unique name
    for _ in 1 2 3 4 5; do
      candidate="${SA_PREFIX}${RANDOM}"
      candidate="${candidate:0:24}"
      avail="$(az storage account check-name --name "$candidate" --query nameAvailable -o tsv)"
      [[ "$avail" == "true" ]] && { SA="$candidate"; break; }
    done
    [[ -n "$SA" ]] || die "Could not find an available storage account name"
    log "Creating storage account $SA"
    az storage account create -n "$SA" -g "$STATE_RG" -l "$LOC" \
      --sku Standard_LRS --subscription "$SUB" \
      --allow-blob-public-access false \
      --min-tls-version TLS1_2 -o none
    ok "Storage account $SA created"
  fi
fi
save_state SA "$SA"

# Create the container. Control-plane Owner/Contributor does NOT grant blob data-plane
# access, so the original `--auth-mode login` often fails with AuthorizationPermissionMismatch.
# Prefer shared-key (works for Contributor/Owner); fall back to AAD if shared key is disabled.
log "Ensuring container '$TFSTATE_CONTAINER' in $SA"
if az storage container create -n "$TFSTATE_CONTAINER" --account-name "$SA" \
      --auth-mode key -o none 2>/dev/null; then
  ok "Container ready (shared-key auth)"
else
  warn "Shared-key create failed (key access may be disabled); trying AAD auth with RBAC propagation retries"
  ME_OID="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
  if [[ -n "$ME_OID" ]]; then
    az role assignment create --assignee-object-id "$ME_OID" --assignee-principal-type User \
      --role "Storage Blob Data Contributor" \
      --scope "/subscriptions/$SUB/resourceGroups/$STATE_RG/providers/Microsoft.Storage/storageAccounts/$SA" \
      -o none 2>/dev/null || true
  fi
  created=0
  for i in $(seq 1 12); do
    if az storage container create -n "$TFSTATE_CONTAINER" --account-name "$SA" --auth-mode login -o none 2>/dev/null; then
      created=1; break
    fi
    sleep 15
  done
  [[ "$created" == 1 ]] && ok "Container ready (AAD auth)" || die "Could not create container '$TFSTATE_CONTAINER'"
fi
echo "state storage account = $SA"

# =========================================================================================
# 3b. GitHub OIDC identity (app registration + 2 federated credentials)
# =========================================================================================
log "3b. GitHub OIDC identities (least-privilege split)"

# ensure_app DISPLAY_NAME -> echoes appId; reuses an existing app of that name and
# guarantees a service principal exists (idempotent, no duplicate registrations).
ensure_app() {
  local dn="$1" id
  id="$(az ad app list --filter "displayName eq '$dn'" --query "[0].appId" -o tsv 2>/dev/null || true)"
  [[ -z "$id" ]] && id="$(az ad app create --display-name "$dn" --query appId -o tsv)"
  az ad sp show --id "$id" >/dev/null 2>&1 || az ad sp create --id "$id" -o none
  echo "$id"
}

# ensure_fic APP_ID NAME SUBJECT  (idempotent)
ensure_fic() {
  local app="$1" name="$2" subject="$3" params
  if az ad app federated-credential list --id "$app" --query "[?name=='$name'] | length(@)" -o tsv 2>/dev/null | grep -q '^1'; then
    ok "FIC '$name' already present"; return 0
  fi
  params=$(cat <<JSON
{"name":"$name","issuer":"https://token.actions.githubusercontent.com","subject":"$subject","audiences":["api://AzureADTokenExchange"]}
JSON
)
  az ad app federated-credential create --id "$app" --parameters "$params" -o none
  ok "Created FIC '$name'"
}

# --- Privileged identity: used ONLY by the apply job ---
APP_ID="$(ensure_app "$APP_DISPLAY_NAME")"
APP_OID="$(az ad sp show --id "$APP_ID" --query id -o tsv)"
save_state APP_ID "$APP_ID"; save_state APP_OID "$APP_OID"
ensure_fic "$APP_ID" "gh-main"               "repo:${REPO}:ref:refs/heads/main"
ensure_fic "$APP_ID" "gh-env-deploy-destroy" "repo:${REPO}:environment:deploy-destroy"
ok "Privileged (apply) identity: $APP_ID"

# --- Low-privilege identity: used by build/seed/plan/smoke (run un-gated on dispatch) ---
PLAN_APP_ID="$(ensure_app "$PLAN_APP_DISPLAY_NAME")"
PLAN_OID="$(az ad sp show --id "$PLAN_APP_ID" --query id -o tsv)"
save_state PLAN_APP_ID "$PLAN_APP_ID"; save_state PLAN_OID "$PLAN_OID"
ensure_fic "$PLAN_APP_ID" "gh-main" "repo:${REPO}:ref:refs/heads/main"
ok "Low-privilege (plan) identity: $PLAN_APP_ID"

# =========================================================================================
# 3c. Role assignments (Contributor for resources; blob data role for state)
# =========================================================================================
log "3c. Role assignments (least privilege)"
ensure_role() { # ensure_role SP_OID ROLE SCOPE
  local oid="$1" role="$2" scope="$3" err
  for i in $(seq 1 10); do
    if err="$(az role assignment create \
        --assignee-object-id "$oid" --assignee-principal-type ServicePrincipal \
        --role "$role" --scope "$scope" -o none 2>&1)"; then
      ok "Granted '$role' on $scope"; return 0
    fi
    if grep -qiE 'already exists|RoleAssignmentExists' <<<"$err"; then
      ok "'$role' already granted on $scope"; return 0
    fi
    # SP may not have replicated in AAD yet — wait and retry
    sleep 15
  done
  die "Failed to grant '$role' on $scope: $err"
}
STATE_SCOPE="/subscriptions/$SUB/resourceGroups/$STATE_RG"

# Privileged (apply): full Contributor so `terraform apply` can create/modify/destroy infra.
ensure_role "$APP_OID"  "Contributor"                  "/subscriptions/$SUB"
ensure_role "$APP_OID"  "Storage Blob Data Contributor" "$STATE_SCOPE"

# Low-privilege (plan/build/seed/smoke): read resources to compute a plan + read/write
# Terraform state (lock blob). No power to mutate infrastructure.
ensure_role "$PLAN_OID" "Reader"                        "/subscriptions/$SUB"
ensure_role "$PLAN_OID" "Storage Blob Data Contributor" "$STATE_SCOPE"

# NOTE — deferred data-plane grants for the low-priv identity, needed only once you enable
# the optional jobs (they skip today because CONTAINER_REGISTRY_NAME / KEY_VAULT_NAME are
# unset). Run these after Terraform creates the registry / vault, scoped to those resources:
#   az role assignment create --assignee-object-id "$PLAN_OID" --assignee-principal-type ServicePrincipal \
#     --role AcrPush --scope <ACR resource id>                       # for the build job
#   az role assignment create --assignee-object-id "$PLAN_OID" --assignee-principal-type ServicePrincipal \
#     --role "Key Vault Secrets Officer" --scope <Key Vault resource id>   # for the seed job

# =========================================================================================
# 3d. Repo Variables (gh variable, NOT gh secret) — these are non-secret config values
# =========================================================================================
log "3d. Repo variables"
set_var() { gh variable set "$1" -R "$REPO" -b "$2" >/dev/null && ok "variable $1"; }
set_var AZURE_CLIENT_ID         "$APP_ID"        # privileged identity (apply job)
set_var AZURE_CLIENT_ID_PLAN    "$PLAN_APP_ID"   # low-priv identity (build/seed/plan/smoke)
set_var AZURE_TENANT_ID         "$TENANT"
set_var AZURE_SUBSCRIPTION_ID   "$SUB"
set_var TFSTATE_RESOURCE_GROUP  "$STATE_RG"
set_var TFSTATE_STORAGE_ACCOUNT "$SA"
set_var TFSTATE_CONTAINER       "$TFSTATE_CONTAINER"

# =========================================================================================
# 3e. Repo Secrets — only set the ones you actually provide.
# Values are read from the environment, or from ./secrets.env (KEY=VALUE lines, gitignored).
# Missing ones are skipped (the seed step just seeds empty for those).
# =========================================================================================
if [[ "$DO_SECRETS" == 1 ]]; then
  log "3e. Repo secrets"
  [[ -f "$SECRETS_FILE" ]] && { set -a; # shellcheck disable=SC1090
    source "$SECRETS_FILE"; set +a; ok "Loaded values from secrets.env"; }
  SECRET_KEYS=(AI_FOUNDRY_API_KEY OPENAI_API_KEY CLAUDE_API_KEY BRAVE_SEARCH_API_KEY \
               TELEGRAM_BOT_TOKEN DISCORD_BOT_TOKEN CF_TUNNEL_TOKEN \
               POSTGRES_CONNECTION_STRING PAPERCLIP_DB_URL)
  set_count=0
  for key in "${SECRET_KEYS[@]}"; do
    val="${!key:-}"
    if [[ -n "$val" ]]; then
      printf '%s' "$val" | gh secret set "$key" -R "$REPO" >/dev/null && { ok "secret $key"; set_count=$((set_count+1)); }
    else
      warn "skip $key (no value in env / secrets.env)"
    fi
  done
  ok "Set $set_count secret(s)"
else
  warn "3e. Skipping secrets (--no-secrets)"
fi

# =========================================================================================
# 3f. Approval-gate environment: deploy-destroy, with you as Required reviewer.
# The operator's note does this in the UI; gh api does it idempotently. (UI alt in README.)
# =========================================================================================
log "3f. Approval-gate environment 'deploy-destroy'"
OWNER="${REPO%%/*}"; NAME="${REPO##*/}"
MY_GH_ID="$(gh api user --jq .id 2>/dev/null || true)"
if [[ -n "$MY_GH_ID" ]]; then
  if gh api -X PUT "repos/$OWNER/$NAME/environments/deploy-destroy" \
       -H "Accept: application/vnd.github+json" --input - >/dev/null <<JSON
{"wait_timer":0,"reviewers":[{"type":"User","id":$MY_GH_ID}],"deployment_branch_policy":null}
JSON
  then ok "Environment 'deploy-destroy' set with you as required reviewer"
  else warn "Could not set environment via API — do it in the UI (Settings → Environments)"
  fi
else
  warn "Could not resolve your GitHub user id; create env 'deploy-destroy' in Settings → Environments and add yourself as Required reviewer"
fi

# =========================================================================================
# 3g. (opt-in) Trigger the deploy workflow and watch it.
# =========================================================================================
if [[ "$DO_DEPLOY" == 1 ]]; then
  log "3g. Triggering deploy.yml (env=$DEPLOY_ENV_INPUT profile=$DEPLOY_PROFILE location=$LOC)"
  gh workflow run deploy.yml -R "$REPO" \
    -f environment="$DEPLOY_ENV_INPUT" -f profile="$DEPLOY_PROFILE" -f location="$LOC"
  sleep 5
  gh run watch -R "$REPO" || true
else
  warn "3g. Skipped workflow trigger. Re-run with --deploy, or run manually:"
  echo "      gh workflow run deploy.yml -R $REPO -f environment=$DEPLOY_ENV_INPUT -f profile=$DEPLOY_PROFILE -f location=$LOC"
  echo "      gh run watch -R $REPO"
fi

# ---- Summary ----------------------------------------------------------------------------
echo
log "Done. Summary:"
cat <<SUMMARY
  Subscription          : $SUB
  Tenant                : $TENANT
  Repo                  : $REPO
  Apply identity (priv) : $APP_ID   (Contributor — apply job only)
  Plan identity (low)   : $PLAN_APP_ID   (Reader + state — build/seed/plan/smoke)
  State resource group  : $STATE_RG
  State storage account : $SA
  State container       : $TFSTATE_CONTAINER

  Non-secret IDs saved to: $STATE_FILE  (safe to keep; not secret)
SUMMARY
