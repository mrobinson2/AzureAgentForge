#!/usr/bin/env bash
# Scaffold the reference CI/CD pipeline's one-time setup (Azure + GitHub).
#
# The pipeline (.github/workflows/deploy.yml) authenticates to Azure via OIDC,
# keeps Terraform state in an Azure Storage account, and reads its config from
# GitHub repo variables/secrets + a `deploy-destroy` approval environment.
# Standing all that up by hand is the biggest one-time gate before a first CI
# deploy (docs/deploy-pipeline.md §"One-time setup"). This script does it as one
# idempotent command:
#
#   A. Entra app registration + service principal, a Contributor role assignment
#      (optionally User Access Administrator), and a GitHub OIDC federated
#      credential — no client secret is ever created or stored.
#   B. the Terraform remote-state backend (resource group + storage account +
#      blob container).
#   C. the GitHub repository variables the workflow reads (non-secret OIDC ids).
#   D. the GitHub repository secrets for the provider keys/tokens you've set in
#      the environment (values piped via stdin, never echoed).
#   E. the `deploy-destroy` GitHub Environment + required reviewers (the
#      destructive-plan approval gate).
#
# PREVIEW-FIRST: with no --apply it prints exactly what it would do and changes
# nothing (it mutates identity, RBAC, and repo config). Idempotent under --apply:
# every step is find-or-create / set, so re-runs converge. --skip-github does only
# the Azure half (A+B).
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
REGISTRY=""
KEY_VAULT=""
SMOKE_URL=""
REVIEWERS=""
SKIP_GITHUB=0

# The repo SECRETS the deploy workflow's seed job consumes (deploy.yml). Set as
# GitHub secrets in step D from like-named env vars; only the ones you've set are
# pushed (the rest stay placeholders downstream until you provide them).
SECRET_NAMES="AI_FOUNDRY_API_KEY OPENAI_API_KEY CLAUDE_API_KEY BRAVE_SEARCH_API_KEY \
TELEGRAM_BOT_TOKEN DISCORD_BOT_TOKEN CF_TUNNEL_TOKEN POSTGRES_CONNECTION_STRING \
PAPERCLIP_DB_URL"

die() { echo "error: $*" >&2; exit 2; }

usage() {
  cat <<'EOF'
Usage: scripts/scaffold-cicd.sh [options]

Stands up the Azure OIDC identity + Terraform state backend AND the GitHub repo
variables/secrets/deploy-destroy environment for the CI/CD pipeline.
Preview-first: prints the plan and changes nothing unless --apply.

      --apply               Actually create/set everything (default: preview).
      --repo OWNER/REPO     GitHub repo. Default: detected via `gh repo view`.
      --app-name NAME       Entra app display name (default: aaf-deploy).
      --subscription ID     Target subscription (default: current `az account`).
      --location LOC        Region for the state RG/account (default: eastus).
      --state-rg NAME       RG for TF state (default: aaf-tfstate-rg).
      --state-account NAME  Storage account for TF state (default: derived from the
                            subscription id; globally unique, override here).
      --state-container N   Blob container for state (default: tfstate).
      --grant-uaa           Also assign User Access Administrator.
      --environment-subject Also add a federated credential scoped to the
                            `deploy-destroy` environment.
      --registry NAME       Set the CONTAINER_REGISTRY_NAME repo variable (ACR).
      --key-vault NAME      Set the KEY_VAULT_NAME repo variable.
      --smoke-url URL       Set the SMOKE_URL repo variable.
      --reviewers a,b       Required reviewers for the deploy-destroy environment
                            (GitHub logins; default: the current gh user).
      --skip-github         Do only the Azure half (steps A+B).
  -h, --help                This help.

Provider keys become GitHub secrets (step D) from env vars of the same name;
only the ones you set are pushed, and values are never printed:
  CLAUDE_API_KEY=... AI_FOUNDRY_API_KEY=... \
    scripts/scaffold-cicd.sh --repo OWNER/REPO --apply

Prerequisites: `az` logged in with rights to create an app registration + assign
roles; `gh` authenticated with repo admin (unless --skip-github).
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
    --registry) REGISTRY="${2:-}"; shift 2 ;;
    --key-vault) KEY_VAULT="${2:-}"; shift 2 ;;
    --smoke-url) SMOKE_URL="${2:-}"; shift 2 ;;
    --reviewers) REVIEWERS="${2:-}"; shift 2 ;;
    --skip-github) SKIP_GITHUB=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

# Echo a command; run it only under --apply. Used for mutating calls whose
# arguments are safe to print (never used for secret values — see gh_secret).
run() {
  echo "+ $*"
  [ "$APPLY" -eq 1 ] || return 0
  "$@"
}

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
  if [ "$SKIP_GITHUB" -eq 0 ]; then
    command -v gh >/dev/null 2>&1 || die "gh (GitHub CLI) not found on PATH (or pass --skip-github)"
    gh auth status >/dev/null 2>&1 || die "gh not authenticated — run 'gh auth login' (or pass --skip-github)"
  fi
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
STATE_SCOPE="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${STATE_RG}"
PLAN_APP_NAME="${APP_NAME}-plan"

echo "== AzureAgentForge CI/CD scaffold =="
echo "mode=$([ "$APPLY" -eq 1 ] && echo APPLY || echo preview) repo=$REPO app=$APP_NAME github=$([ "$SKIP_GITHUB" -eq 1 ] && echo skip || echo yes)"
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

# ── Step B2: low-privilege PLAN identity (least-privilege split) ─────────────
# build/seed/plan/smoke run as this Reader-only identity; only `apply` uses the
# privileged app above (deploy.yml selects per job). A poisoned dependency in a
# pre-approval job therefore can't reach subscription Contributor.
echo "-- step B2: low-privilege plan identity ($PLAN_APP_NAME) --"
PLAN_APP_ID="<plan-app-id>"
if [ "$APPLY" -eq 1 ]; then
  PLAN_APP_ID="$(az ad app list --display-name "$PLAN_APP_NAME" --query '[0].appId' -o tsv 2>/dev/null || true)"
  if [ -n "$PLAN_APP_ID" ]; then
    echo "  app exists: $PLAN_APP_NAME ($PLAN_APP_ID)"
  else
    echo "  creating app registration: $PLAN_APP_NAME"
    PLAN_APP_ID="$(az ad app create --display-name "$PLAN_APP_NAME" --query appId -o tsv)"
  fi
  az ad sp show --id "$PLAN_APP_ID" >/dev/null 2>&1 || run az ad sp create --id "$PLAN_APP_ID"
  # Reader @ subscription (read, never mutate) + Storage Blob Data Contributor @
  # state RG (read/write tfstate only). NEVER Contributor on this identity.
  for pr in "Reader|$SUB_SCOPE" "Storage Blob Data Contributor|$STATE_SCOPE"; do
    prole="${pr%%|*}"; pscope="${pr##*|}"
    if [ -n "$(az role assignment list --assignee "$PLAN_APP_ID" --role "$prole" --scope "$pscope" --query '[0].id' -o tsv 2>/dev/null || true)" ]; then
      echo "  role exists: $prole on $pscope"
    else
      run az role assignment create --assignee "$PLAN_APP_ID" --role "$prole" --scope "$pscope"
    fi
  done
  # Same federated credentials as the privileged app (identical workflow runs).
  pfics="aaf-plan-main|$MAIN_SUBJECT"
  [ "$ENV_SUBJECT" -eq 1 ] && pfics="$pfics aaf-plan-env|$ENV_SUBJECT_STR"
  for fc in $pfics; do
    fname="${fc%%|*}"; fsubj="${fc##*|}"
    if [ -n "$(az ad app federated-credential list --id "$PLAN_APP_ID" --query "[?subject=='$fsubj'].id" -o tsv 2>/dev/null || true)" ]; then
      echo "  federated credential exists: $fsubj"
    else
      run az ad app federated-credential create --id "$PLAN_APP_ID" --parameters "{\"name\":\"$fname\",\"issuer\":\"https://token.actions.githubusercontent.com\",\"subject\":\"$fsubj\",\"audiences\":[\"api://AzureADTokenExchange\"]}"
    fi
  done
else
  echo "+ az ad app create --display-name $PLAN_APP_NAME       (find-or-create, Reader-only)"
  echo "+ roles: Reader @ subscription + Storage Blob Data Contributor @ $STATE_RG (never Contributor)"
fi
echo

if [ "$SKIP_GITHUB" -eq 1 ]; then
  echo "== Azure side done (--skip-github). Set the GitHub side with another run, or:"
  echo "   gh variable set AZURE_CLIENT_ID --repo $REPO --body \"$APP_ID\""
  echo "   gh variable set AZURE_CLIENT_ID_PLAN --repo $REPO --body \"$PLAN_APP_ID\"   (and the rest — see docs/deploy-pipeline.md)"
  [ "$APPLY" -eq 1 ] || echo "(this was a preview — re-run with --apply)"
  exit 0
fi

# ── Step C: GitHub repository variables (non-secret OIDC ids) ────────────────
echo "-- step C: GitHub repository variables --"
gh_var() {
  # gh_var NAME VALUE — variables are non-secret identifiers, safe to print.
  run gh variable set "$1" --repo "$REPO" --body "$2"
}
gh_var AZURE_CLIENT_ID "$APP_ID"
gh_var AZURE_CLIENT_ID_PLAN "$PLAN_APP_ID"
gh_var AZURE_TENANT_ID "$TENANT_ID"
gh_var AZURE_SUBSCRIPTION_ID "$SUBSCRIPTION_ID"
gh_var TFSTATE_RESOURCE_GROUP "$STATE_RG"
gh_var TFSTATE_STORAGE_ACCOUNT "$STATE_ACCOUNT"
gh_var TFSTATE_CONTAINER "$STATE_CONTAINER"
[ -n "$REGISTRY" ] && gh_var CONTAINER_REGISTRY_NAME "$REGISTRY"
[ -n "$KEY_VAULT" ] && gh_var KEY_VAULT_NAME "$KEY_VAULT"
[ -n "$SMOKE_URL" ] && gh_var SMOKE_URL "$SMOKE_URL"
echo

# ── Step D: GitHub repository secrets (provider keys present in the env) ─────
echo "-- step D: GitHub repository secrets (from env; values never printed) --"
gh_secret() {
  # gh_secret NAME — push $NAME from the environment via stdin (never echoed).
  local name="$1" val
  val="$(printenv "$name" || true)"
  if [ -z "$val" ]; then
    echo "  skip: $name (env \$$name unset)"
    return 0
  fi
  if [ "$APPLY" -eq 1 ]; then
    printf '%s' "$val" | gh secret set "$name" --repo "$REPO" >/dev/null && echo "  set secret: $name"
  else
    echo "+ gh secret set $name --repo $REPO    (from \$$name; value hidden)"
  fi
}
for s in $SECRET_NAMES; do gh_secret "$s"; done
echo

# ── Step E: the deploy-destroy approval environment ──────────────────────────
echo "-- step E: deploy-destroy environment + required reviewers --"
if [ "$APPLY" -eq 1 ]; then
  revspec="${REVIEWERS:-$(gh api user --jq .login 2>/dev/null || true)}"
  reviewers_json=""
  IFS=',' read -r -a revs <<< "$revspec"
  for login in "${revs[@]}"; do
    login="$(printf '%s' "$login" | tr -d '[:space:]')"
    [ -n "$login" ] || continue
    id="$(gh api "users/$login" --jq .id 2>/dev/null || true)"
    if [ -z "$id" ]; then echo "  warn: could not resolve reviewer '$login' — skipping"; continue; fi
    reviewers_json="${reviewers_json}{\"type\":\"User\",\"id\":$id},"
  done
  reviewers_json="${reviewers_json%,}"
  if [ -z "$reviewers_json" ]; then
    echo "  warn: no reviewers resolved — creating the environment WITHOUT a required reviewer (the gate won't block until you add one)"
    echo '{}' | gh api --method PUT "repos/$REPO/environments/deploy-destroy" --input - >/dev/null && echo "  environment deploy-destroy ensured (no reviewers)"
  else
    echo "  ensuring environment deploy-destroy (reviewers: $revspec)"
    printf '{"reviewers":[%s]}' "$reviewers_json" | gh api --method PUT "repos/$REPO/environments/deploy-destroy" --input - >/dev/null && echo "  environment deploy-destroy ensured"
  fi
else
  echo "+ gh api --method PUT repos/$REPO/environments/deploy-destroy   (reviewers: ${REVIEWERS:-<current gh user>})"
fi
echo

echo "== done =="
echo "The pipeline is ready to run: Actions -> Deploy (reference) -> Run workflow."
echo "First Azure deploy still needs the one-time Key Vault bootstrap — see scripts/bootstrap.sh."
[ "$APPLY" -eq 1 ] || echo "(this was a preview — re-run with --apply to create/set everything above)"
