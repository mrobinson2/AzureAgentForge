#!/usr/bin/env bash
# Scaffold the Azure side of the reference CI/CD pipeline (scaffold-1).
#
# The pipeline (.github/workflows/deploy.yml) authenticates to Azure via OIDC and
# keeps Terraform state in an Azure Storage account. Standing those up by hand is
# the biggest one-time gate before a first CI deploy (docs/deploy-pipeline.md
# §"One-time setup"). This script does the Azure half as one idempotent command:
#
#   A. an Entra ID app registration + service principal, a Contributor role
#      assignment on the subscription, and a GitHub OIDC federated credential
#      (no client secret is ever created or stored);
#   B. the Terraform remote-state backend (resource group + storage account +
#      blob container).
#
# It then prints the GitHub repository **variables** to set (the non-secret
# identifiers the workflow reads) as ready-to-paste `gh variable set` lines — the
# GitHub side (variables/secrets/the deploy-destroy environment) is scaffold-2.
#
# PREVIEW-FIRST: with no --apply it prints exactly what it would create and
# changes nothing (it mutates identity + RBAC, so nothing happens without intent).
# Idempotent: with --apply every step is find-or-create, so re-runs converge.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

APPLY=0
REPO=""
APP_NAME="aaf-deploy"
SUBSCRIPTION=""
LOCATION="eastus"
STATE_RG="aaf-tfstate-rg"
STATE_ACCOUNT=""
STATE_CONTAINER="tfstate"
GRANT_UAA=0
ENV_SUBJECT=0

die() { echo "error: $*" >&2; exit 2; }

usage() {
  cat <<'EOF'
Usage: scripts/scaffold-cicd.sh [options]

Stands up the Azure OIDC identity + Terraform state backend for the CI/CD
pipeline. Preview-first: prints the plan and changes nothing unless --apply.

      --apply              Actually create resources (default: preview only).
      --repo OWNER/REPO    GitHub repo to trust for OIDC. Default: detected via
                           `gh repo view`, else required with --apply.
      --app-name NAME      Entra app registration display name (default: aaf-deploy).
      --subscription ID    Target subscription (default: current `az account`).
      --location LOC       Region for the state RG/account (default: eastus).
      --state-rg NAME      Resource group for TF state (default: aaf-tfstate-rg).
      --state-account NAME Storage account for TF state (default: derived from the
                           subscription id; must be globally unique, override here).
      --state-container N  Blob container for state (default: tfstate).
      --grant-uaa          Also assign User Access Administrator (needed if your
                           deploy creates RBAC role assignments).
      --environment-subject Also add a federated credential scoped to the
                           `deploy-destroy` environment (tighter than the main ref).
  -h, --help               This help.

Prerequisites: `az` logged in (`az login`) with rights to create an app
registration and assign roles on the subscription; optionally `gh` for repo
auto-detection. Run scaffold-2 (the GitHub side) after this.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --repo) REPO="${2:-}"; shift 2 ;;
    --app-name) APP_NAME="${2:-}"; shift 2 ;;
    --subscription) SUBSCRIPTION="${2:-}"; shift 2 ;;
    --location) LOCATION="${2:-}"; shift 2 ;;
    --state-rg) STATE_RG="${2:-}"; shift 2 ;;
    --state-account) STATE_ACCOUNT="${2:-}"; shift 2 ;;
    --state-container) STATE_CONTAINER="${2:-}"; shift 2 ;;
    --grant-uaa) GRANT_UAA=1; shift ;;
    --environment-subject) ENV_SUBJECT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

# Echo a command; run it only under --apply. Used for mutating az calls so a
# preview prints the exact action (incl. role/scope) without performing it.
run() {
  echo "+ $*"
  [ "$APPLY" -eq 1 ] || return 0
  "$@"
}

# Resolve subscription + tenant. Reads are harmless; if not logged in we fall
# back to placeholders for the preview, but --apply requires a real login.
SUBSCRIPTION_ID="" ; TENANT_ID=""
resolve_account() {
  if az account show >/dev/null 2>&1; then
    SUBSCRIPTION_ID="${SUBSCRIPTION:-$(az account show --query id -o tsv)}"
    TENANT_ID="$(az account show --query tenantId -o tsv)"
  else
    [ "$APPLY" -eq 1 ] && die "not logged in to Azure — run 'az login' first"
    SUBSCRIPTION_ID="${SUBSCRIPTION:-<subscription-id>}"
    TENANT_ID="<tenant-id>"
  fi
}

resolve_repo() {
  [ -n "$REPO" ] && return 0
  if command -v gh >/dev/null 2>&1 && REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)" && [ -n "$REPO" ]; then
    return 0
  fi
  [ "$APPLY" -eq 1 ] && die "could not detect the GitHub repo — pass --repo OWNER/REPO"
  REPO="<owner>/<repo>"
}

if [ "$APPLY" -eq 1 ]; then
  command -v az >/dev/null 2>&1 || die "az (Azure CLI) not found on PATH"
fi

resolve_account
resolve_repo

# Storage account names must be globally unique, <=24 lowercase alphanumerics.
# Derive a deterministic default from the subscription id (so re-runs target the
# same account = idempotent); override with --state-account.
if [ -z "$STATE_ACCOUNT" ]; then
  SUFFIX="$(printf '%s' "$SUBSCRIPTION_ID" | tr -dc 'a-z0-9' | cut -c1-8)"
  STATE_ACCOUNT="aaftfstate${SUFFIX}"
fi

MAIN_SUBJECT="repo:${REPO}:ref:refs/heads/main"
ENV_SUBJECT_STR="repo:${REPO}:environment:deploy-destroy"
SUB_SCOPE="/subscriptions/${SUBSCRIPTION_ID}"

echo "== AzureAgentForge CI/CD scaffold (Azure side) =="
echo "mode=$([ "$APPLY" -eq 1 ] && echo APPLY || echo preview) repo=$REPO app=$APP_NAME"
echo "subscription=$SUBSCRIPTION_ID tenant=$TENANT_ID location=$LOCATION"
echo "state: rg=$STATE_RG account=$STATE_ACCOUNT container=$STATE_CONTAINER"
[ "$APPLY" -eq 1 ] || echo "(preview — nothing will be created; re-run with --apply)"
echo

# ── Step A: OIDC identity ────────────────────────────────────────────────────
echo "-- step A: Entra app + service principal + role + federated credential --"
APP_ID="<app-id>"
if [ "$APPLY" -eq 1 ]; then
  APP_ID="$(az ad app list --display-name "$APP_NAME" --query '[0].appId' -o tsv 2>/dev/null || true)"
  if [ -n "$APP_ID" ]; then
    echo "  app exists: $APP_NAME ($APP_ID)"
  else
    echo "  creating app registration: $APP_NAME"
    APP_ID="$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)"
    echo "  created: $APP_ID"
  fi
  if az ad sp show --id "$APP_ID" >/dev/null 2>&1; then
    echo "  service principal exists"
  else
    run az ad sp create --id "$APP_ID"
  fi
else
  echo "+ az ad app create --display-name $APP_NAME            (find-or-create)"
  echo "+ az ad sp create --id <app-id>                        (find-or-create)"
fi

# Role assignment(s) on the subscription.
ROLES="Contributor"
[ "$GRANT_UAA" -eq 1 ] && ROLES="$ROLES|User Access Administrator"
IFS='|' read -r -a ROLE_ARR <<< "$ROLES"
for role in "${ROLE_ARR[@]}"; do
  if [ "$APPLY" -eq 1 ]; then
    existing="$(az role assignment list --assignee "$APP_ID" --role "$role" --scope "$SUB_SCOPE" --query '[0].id' -o tsv 2>/dev/null || true)"
    if [ -n "$existing" ]; then
      echo "  role exists: $role on $SUB_SCOPE"
    else
      run az role assignment create --assignee "$APP_ID" --role "$role" --scope "$SUB_SCOPE"
    fi
  else
    echo "+ az role assignment create --assignee <app-id> --role \"$role\" --scope $SUB_SCOPE"
  fi
done

# Federated credential(s) for GitHub OIDC.
add_fic() {
  # add_fic NAME SUBJECT
  local name="$1" subject="$2"
  if [ "$APPLY" -eq 1 ]; then
    local existing
    existing="$(az ad app federated-credential list --id "$APP_ID" --query "[?subject=='$subject'].id" -o tsv 2>/dev/null || true)"
    if [ -n "$existing" ]; then
      echo "  federated credential exists: $subject"
      return 0
    fi
    run az ad app federated-credential create --id "$APP_ID" --parameters "{
  \"name\": \"$name\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"$subject\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"
  else
    echo "+ az ad app federated-credential create --id <app-id> --parameters {subject: $subject}"
  fi
}
add_fic "aaf-deploy-main" "$MAIN_SUBJECT"
[ "$ENV_SUBJECT" -eq 1 ] && add_fic "aaf-deploy-env" "$ENV_SUBJECT_STR"
echo

# ── Step B: Terraform state backend ──────────────────────────────────────────
echo "-- step B: Terraform remote-state backend --"
if [ "$APPLY" -eq 1 ]; then
  if az group show -n "$STATE_RG" >/dev/null 2>&1; then
    echo "  resource group exists: $STATE_RG"
  else
    run az group create -n "$STATE_RG" -l "$LOCATION"
  fi
  if az storage account show -n "$STATE_ACCOUNT" -g "$STATE_RG" >/dev/null 2>&1; then
    echo "  storage account exists: $STATE_ACCOUNT"
  else
    run az storage account create -n "$STATE_ACCOUNT" -g "$STATE_RG" -l "$LOCATION" \
      --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 --allow-blob-public-access false
  fi
  if az storage container show -n "$STATE_CONTAINER" --account-name "$STATE_ACCOUNT" --auth-mode login >/dev/null 2>&1; then
    echo "  container exists: $STATE_CONTAINER"
  else
    run az storage container create -n "$STATE_CONTAINER" --account-name "$STATE_ACCOUNT" --auth-mode login
  fi
else
  echo "+ az group create -n $STATE_RG -l $LOCATION"
  echo "+ az storage account create -n $STATE_ACCOUNT -g $STATE_RG -l $LOCATION --sku Standard_LRS --kind StorageV2"
  echo "+ az storage container create -n $STATE_CONTAINER --account-name $STATE_ACCOUNT --auth-mode login"
fi
echo

# ── Summary: the GitHub repo variables to set (scaffold-2 / by hand) ─────────
echo "== done — set these GitHub repository variables (scaffold-2 will automate this) =="
cat <<EOF
  gh variable set AZURE_CLIENT_ID --repo $REPO --body "$APP_ID"
  gh variable set AZURE_TENANT_ID --repo $REPO --body "$TENANT_ID"
  gh variable set AZURE_SUBSCRIPTION_ID --repo $REPO --body "$SUBSCRIPTION_ID"
  gh variable set TFSTATE_RESOURCE_GROUP --repo $REPO --body "$STATE_RG"
  gh variable set TFSTATE_STORAGE_ACCOUNT --repo $REPO --body "$STATE_ACCOUNT"
  gh variable set TFSTATE_CONTAINER --repo $REPO --body "$STATE_CONTAINER"

Still to do (manual or scaffold-2): repo secrets for provider keys
(CLAUDE_API_KEY, …) and the 'deploy-destroy' Environment with required reviewers.
See docs/deploy-pipeline.md §"One-time setup".
EOF
[ "$APPLY" -eq 1 ] || echo "(this was a preview — re-run with --apply to create the resources above)"
