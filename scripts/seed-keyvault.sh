#!/usr/bin/env bash
# Seed the AzureAgentForge Key Vault with the secrets the Container Apps mount.
#
# Idempotent: an existing secret is left untouched unless --force is given.
# Two categories (the names match what infrastructure/modules/container-apps
# references, so a deploy can resolve every secretRef):
#
#   generate  internal secrets with no external source — created with a random
#             value if absent. Includes postgres-admin-password, which the
#             keyvault module reads as a data source and feeds to Postgres, so
#             it MUST exist before the first `terraform apply`.
#
#   external  values that come from you (LLM/provider keys, bot tokens, the
#             Postgres connection strings). Read from an env var named after the
#             secret, upper-cased with dashes as underscores
#             (claude-api-key -> CLAUDE_API_KEY). Every external is referenced by
#             a container's Key Vault mount, and ACA fails the deploy if a
#             referenced secret is missing — so unset ones are seeded as an EMPTY
#             placeholder (the feature/tier stays inert until you fill it in and
#             re-run). You only need real values for the providers/surfaces you
#             enable; a provided value always overwrites an earlier placeholder.
#
# The connection strings (postgres-connection-string, paperclip-db-url) are
# external on purpose: build them from your `terraform output` after Postgres
# exists and pass them in, rather than have this script guess a URI shape.
set -euo pipefail

VAULT=""
FORCE=0
DRY_RUN=0

# Internal secrets generated locally if missing.
# router-api-key (aaf-0005): shared bearer every in-mesh model-router caller
# (hermes.tf, memory_governor.tf, paperclip.tf) and the router itself read —
# generating one value here keeps them all in sync without operator input.
GENERATE="postgres-admin-password governor-api-key router-api-key \
paperclip-admin-password \
paperclip-agent-jwt-secret paperclip-auth-secret paperclip-automation-jwt-secret \
paperclip-automation-token auth-password gateway-token"

# Secrets sourced from the environment. Every name here is referenced by a
# container app's Key Vault secret mount (infrastructure/modules/container-apps),
# so each must EXIST in the vault for `terraform apply` to succeed — see the
# external loop below, which seeds a non-empty placeholder for any you don't set.
EXTERNAL="ai-foundry-api-key brave-search-api-key cf-tunnel-token claude-api-key \
claude-base-url discord-bot-token gpt4o-api-key grok-api-key grok-base-url \
gws-credentials kimi-api-key kimi-base-url openai-api-key paperclip-admin-email \
phi-api-key phi-base-url telegram-bot-token postgres-connection-string \
paperclip-db-url"

# Non-empty sentinel seeded for any unset external. `az keyvault secret set`
# REJECTS an empty --value, so we cannot seed "" — yet every external above is
# referenced by a container's Key Vault mount and must EXIST for `terraform apply`
# to succeed. CAVEAT: consumers do not yet treat this value as "unconfigured"
# (the model-router registers a tier whenever its *_BASE_URL / *_API_KEY are
# truthy), so an unset OPTIONAL tier registers against this placeholder and fails
# at request time rather than fail-soft skipping. Follow-up: teach the router (and
# other readers) to treat PLACEHOLDER_VALUE as empty.
PLACEHOLDER_VALUE="__unset__"

# The externals that are Postgres DSNs. Their userinfo must match the server's
# administrator_login — see check_dsn_username below for why that is worth a
# hard failure here.
DSN_SECRETS="postgres-connection-string paperclip-db-url"

# Expected Postgres administrator_login. Read from POSTGRES_ADMIN_USERNAME, else
# from the Terraform variable's default so the two cannot drift silently. Note
# the deploy pipeline threads its own value (deploy.yml passes
# -var="postgres_admin_username=${{ vars.POSTGRES_ADMIN_USERNAME || 'dbadmin' }}"),
# so set the env var here to match whatever that repo variable says if it is not
# the default.
tf_admin_username_default() {
  sed -n '/variable "postgres_admin_username"/,/^}/p' \
    "$(dirname "$0")/../infrastructure/environments/dev/variables.tf" 2>/dev/null \
    | sed -n 's/.*default *= *"\(.*\)".*/\1/p' | head -1
}
PG_ADMIN_USERNAME="${POSTGRES_ADMIN_USERNAME:-$(tf_admin_username_default)}"

die() { echo "error: $*" >&2; exit 2; }

usage() {
  cat <<'EOF'
Usage: scripts/seed-keyvault.sh --vault NAME [options]

  -v, --vault NAME   Key Vault name (the bare name, not the URL). Required.
      --force        Overwrite secrets that already exist.
  -n, --dry-run      Print what would happen; never call `az keyvault secret set`.
  -l, --list         Print the secret inventory (generate vs external) and exit.
      --postgres-admin-username NAME
                     Expected Postgres administrator_login for the DSN guard.
                     Default: $POSTGRES_ADMIN_USERNAME, else the Terraform
                     variable default. Empty string disables the check.
      --self-check   Offline self-test of the DSN guard; no vault, no network.
  -h, --help         This help.

External secrets are read from env vars (claude-api-key -> CLAUDE_API_KEY):
  CLAUDE_API_KEY=... AI_FOUNDRY_API_KEY=... POSTGRES_CONNECTION_STRING=... \
    scripts/seed-keyvault.sh -v aaf-dev-kv

Run once BEFORE `terraform apply` (so postgres-admin-password exists), then
again AFTER with the connection strings filled in from `terraform output`.
EOF
}

env_name() { echo "$1" | tr 'a-z-' 'A-Z_'; }

gen_value() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    # Fallback: still random, no openssl dependency.
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

secret_exists() {
  az keyvault secret show --vault-name "$VAULT" --name "$1" >/dev/null 2>&1
}

secret_value() {
  az keyvault secret show --vault-name "$VAULT" --name "$1" \
    --query value -o tsv 2>/dev/null
}

# Extract the userinfo name from a postgres DSN: scheme://USER:password@host/db
# Prints nothing for a value that is not a DSN (e.g. the placeholder). Never
# prints the password — callers echo only the username.
dsn_username() {
  printf '%s' "$1" | sed -n 's#^[a-zA-Z+]*://\([^:/@]*\):[^@]*@.*#\1#p'
}

# Guard: a DSN whose user disagrees with the server's administrator_login gets
# FATAL "password authentication failed for user ..." at container start, which
# reads as a password problem and sends you hunting the wrong secret. That cost
# a six-day dev outage (2026-07-15 → 07-21): the environment was rebuilt with
# the sanitized default `dbadmin` while both DSNs still carried the pre-v1.4
# `vaultadmin`, inherited from the platform this repo was extracted from.
#
# administrator_login is immutable — changing it forces Postgres replacement —
# so the DSN is what has to match, and mismatches are worth catching here rather
# than in container logs days later.
check_dsn_username() {
  # check_dsn_username NAME VALUE
  [ -n "$PG_ADMIN_USERNAME" ] || return 0
  [ "$2" = "$PLACEHOLDER_VALUE" ] && return 0
  actual="$(dsn_username "$2")"
  [ -n "$actual" ] || return 0
  [ "$actual" = "$PG_ADMIN_USERNAME" ] && return 0
  die "$1 connects as user '$actual' but the Postgres administrator_login is \
'$PG_ADMIN_USERNAME'. Every app using this DSN will fail with \
\"password authentication failed for user \\\"$actual\\\"\" — which is a USERNAME \
mismatch, not a password one. Fix the DSN (or pass \
--postgres-admin-username to match a server that really does use '$actual')."
}

set_secret() {
  # set_secret NAME VALUE
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    would set: $1"
    return 0
  fi
  az keyvault secret set --vault-name "$VAULT" --name "$1" --value "$2" \
    --output none && echo "    set: $1"
}

# Offline self-test of the DSN guard: no vault, no az, no network. Runs in CI
# next to the other scripts checks.
self_check() {
  fails=0
  t() {
    # t DESCRIPTION EXPECT_USER DSN EXPECT_RESULT(ok|fail)
    PG_ADMIN_USERNAME="$2"
    if ( check_dsn_username "self-check" "$3" ) >/dev/null 2>&1; then
      got=ok
    else
      got=fail
    fi
    if [ "$got" = "$4" ]; then
      echo "  PASS  $1"
    else
      echo "  FAIL  $1 (wanted $4, got $got)"; fails=$((fails + 1))
    fi
  }

  echo "seed-keyvault: self-check (offline)…"
  t "matching user passes" dbadmin \
    "postgresql+psycopg://dbadmin:pw@host.postgres.database.azure.com:5432/honcho" ok
  t "mismatched user fails (the 2026-07-15 outage)" dbadmin \
    "postgresql+psycopg://vaultadmin:pw@host.postgres.database.azure.com:5432/honcho" fail
  t "plain postgresql scheme parses" dbadmin \
    "postgresql://vaultadmin:pw@host:5432/postgres" fail
  t "placeholder is skipped" dbadmin "$PLACEHOLDER_VALUE" ok
  t "non-DSN value is skipped" dbadmin "not-a-dsn" ok
  t "empty expectation disables the check" "" \
    "postgresql://whoever:pw@host:5432/db" ok
  t "password containing an @ does not confuse the parser" dbadmin \
    "postgresql://dbadmin:p@ss@host:5432/db" ok

  if [ "$fails" -eq 0 ]; then
    echo "seed-keyvault: self-check PASS"
  else
    echo "seed-keyvault: self-check FAIL ($fails)"; exit 1
  fi
}

print_inventory() {
  echo "generate (random if absent):"
  for s in $GENERATE; do echo "  - $s"; done
  echo "external (from env, e.g. $(env_name claude-api-key)):"
  for s in $EXTERNAL; do echo "  - $s  <- $(env_name "$s")"; done
}

while [ $# -gt 0 ]; do
  case "$1" in
    -v|--vault) VAULT="${2:-}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -n|--dry-run) DRY_RUN=1; shift ;;
    -l|--list) print_inventory; exit 0 ;;
    --postgres-admin-username) PG_ADMIN_USERNAME="${2:-}"; shift 2 ;;
    --self-check) self_check; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[ -n "$VAULT" ] || [ "$DRY_RUN" -eq 1 ] || die "--vault is required (or use --dry-run)"
[ -n "$VAULT" ] || VAULT="<vault>"

echo "vault=$VAULT force=$FORCE dry_run=$DRY_RUN"
echo

generated=0 ; provided=0 ; kept=0 ; placeheld=0 ; missing=""

echo "generate:"
for s in $GENERATE; do
  if [ "$FORCE" -eq 0 ] && [ "$DRY_RUN" -eq 0 ] && secret_exists "$s"; then
    echo "    keep: $s (exists)"; kept=$((kept + 1)); continue
  fi
  set_secret "$s" "$(gen_value)"; generated=$((generated + 1))
done

echo
echo "external:"
for s in $EXTERNAL; do
  ev="$(env_name "$s")"
  val="$(printenv "$ev" || true)"
  if [ -n "$val" ]; then
    # Provided — set it, overwriting any prior value. This is what makes the
    # two-pass flow work: a placeholder seeded on pass 1 is replaced by the real
    # value (e.g. the connection strings from `terraform output`) on pass 2.
    case " $DSN_SECRETS " in *" $s "*) check_dsn_username "$s" "$val" ;; esac
    set_secret "$s" "$val"; provided=$((provided + 1)); continue
  fi
  if [ "$DRY_RUN" -eq 0 ] && secret_exists "$s"; then
    # Already present (real value set earlier or out-of-band) — never clobber it
    # with an empty placeholder. Still check a DSN kept this way: the outage the
    # guard exists for came from exactly this path, a stale vault value nobody
    # was re-seeding.
    case " $DSN_SECRETS " in
      *" $s "*) check_dsn_username "$s" "$(secret_value "$s")" ;;
    esac
    echo "    keep: $s (exists)"; kept=$((kept + 1)); continue
  fi
  # Unset and absent — seed a non-empty placeholder ($PLACEHOLDER_VALUE). Every
  # external is referenced by a container's Key Vault secret mount, and ACA fails
  # the deploy if a referenced secret does not exist. We cannot seed "" because
  # `az keyvault secret set` rejects an empty --value and aborts the whole run on
  # the first unset external. The placeholder lets `terraform apply` succeed; fill
  # the real value in later and re-run to enable. See PLACEHOLDER_VALUE above for
  # the consumer-side caveat.
  set_secret "$s" "$PLACEHOLDER_VALUE"; placeheld=$((placeheld + 1)); missing="$missing $s"
done

echo
echo "summary: generated=$generated provided=$provided kept=$kept placeholders=$placeheld"
[ -z "$missing" ] || echo "seeded placeholder '$PLACEHOLDER_VALUE' (set the matching env var + re-run to enable):$missing"
