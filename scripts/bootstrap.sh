#!/usr/bin/env bash
# First-deploy bootstrap for AzureAgentForge — the one-command path through the
# Key Vault catch-22.
#
# The Key Vault module reads postgres-admin-password from the vault as a data
# source (infrastructure/modules/keyvault), so that secret must EXIST before the
# first full `terraform apply` can resolve — but you can't seed a vault that
# doesn't exist yet. The documented workaround (docs/deploy-pipeline.md) is a
# three-step dance; this script runs it as one idempotent command:
#
#   1. terraform apply -target resource group + key vault   (create just the vault)
#   2. scripts/seed-keyvault.sh                             (generate
#        postgres-admin-password + seed every referenced secret/placeholder)
#   3. terraform apply                                      (the full stack)
#
# Safe to re-run: every step is a no-op once satisfied (targeted apply converges,
# seed keeps existing secrets, full apply is a normal converge). Provide external
# provider keys as env vars (the same names seed-keyvault.sh reads, e.g.
# CLAUDE_API_KEY=...) and they're seeded in step 2; anything unset gets a
# placeholder you can fill in and re-run later.
#
# This is for the FIRST deploy. By default every `terraform apply` is interactive
# (you review the plan and confirm); pass --yes to auto-approve (first deploy is
# all creates, so there's nothing to destroy).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENVIRONMENT="dev"
VAULT=""
AUTO_APPROVE=0
DRY_RUN=0

die() { echo "error: $*" >&2; exit 2; }

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap.sh [options]

  -e, --environment NAME  Terraform environment under infrastructure/environments
                          (default: dev).
  -v, --vault NAME        Key Vault name. Optional — by default it's read from the
                          `key_vault_name` Terraform output after the vault is
                          created. Pass it to skip the lookup.
  -y, --yes               Auto-approve both `terraform apply` runs (first deploy
                          only — it's all creates).
  -n, --dry-run           Print the steps without running terraform/az/seed.
  -h, --help              This help.

Provide external secrets as env vars (claude-api-key -> CLAUDE_API_KEY); they're
seeded in step 2. See `scripts/seed-keyvault.sh --list` for the inventory.

  CLAUDE_API_KEY=... AI_FOUNDRY_API_KEY=... scripts/bootstrap.sh --yes
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -e|--environment) ENVIRONMENT="${2:-}"; shift 2 ;;
    -v|--vault) VAULT="${2:-}"; shift 2 ;;
    -y|--yes) AUTO_APPROVE=1; shift ;;
    -n|--dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

TF_DIR="$ROOT/infrastructure/environments/$ENVIRONMENT"
[ -d "$TF_DIR" ] || die "no such environment: '$ENVIRONMENT' ($TF_DIR)"

# Preflight (skipped on dry-run): the tools must exist and az must be logged in,
# else terraform/seed fail deep in step 2 with a less obvious message.
if [ "$DRY_RUN" -eq 0 ]; then
  command -v terraform >/dev/null 2>&1 || die "terraform not found on PATH"
  command -v az >/dev/null 2>&1 || die "az (Azure CLI) not found on PATH"
  az account show >/dev/null 2>&1 || die "not logged in to Azure — run 'az login' first"
fi

APPLY_OPTS=(-input=false)
[ "$AUTO_APPROVE" -eq 1 ] && APPLY_OPTS+=(-auto-approve)

tf() {
  echo "+ terraform -chdir=$TF_DIR $*"
  [ "$DRY_RUN" -eq 1 ] && return 0
  terraform -chdir="$TF_DIR" "$@"
}

echo "== AzureAgentForge first-deploy bootstrap =="
echo "environment=$ENVIRONMENT vault=${VAULT:-<from terraform output>} auto_approve=$AUTO_APPROVE dry_run=$DRY_RUN"
echo

# terraform init — only when the working dir isn't initialized yet, so we never
# disturb an existing backend configuration.
if [ ! -d "$TF_DIR/.terraform" ]; then
  echo "-- step 0: terraform init --"
  tf init -input=false
  echo
fi

echo "-- step 1: create the resource group + key vault --"
tf apply "${APPLY_OPTS[@]}" -target=azurerm_resource_group.main -target=module.keyvault
echo

echo "-- step 2: seed Key Vault (postgres-admin-password + referenced secrets) --"
if [ -z "$VAULT" ] && [ "$DRY_RUN" -eq 0 ]; then
  VAULT="$(terraform -chdir="$TF_DIR" output -raw key_vault_name 2>/dev/null || true)"
  [ -n "$VAULT" ] || die "could not read the 'key_vault_name' Terraform output — pass --vault NAME"
fi
if [ "$DRY_RUN" -eq 1 ]; then
  echo "+ scripts/seed-keyvault.sh -v <vault-from-output>"
else
  "$ROOT/scripts/seed-keyvault.sh" -v "$VAULT"
fi
echo

echo "-- step 3: apply the full stack --"
tf apply "${APPLY_OPTS[@]}"
echo

echo "== bootstrap complete =="
cat <<EOF
Next steps:
  - The Postgres connection strings (postgres-connection-string, paperclip-db-url)
    and any provider keys you didn't pass as env vars were seeded as the
    '__unset__' placeholder. Fill them in and re-run the seed to activate:
      POSTGRES_CONNECTION_STRING=... PAPERCLIP_DB_URL=... \\
        scripts/seed-keyvault.sh -v ${VAULT:-<vault>}
    (derive the connection strings from \`terraform -chdir=$TF_DIR output\`),
    then restart the affected container apps (or re-apply).
  - Verify health: scripts/smoke-test.sh -g <resource-group> -e $ENVIRONMENT
EOF
