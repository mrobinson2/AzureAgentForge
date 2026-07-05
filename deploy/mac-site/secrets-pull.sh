#!/usr/bin/env bash
# secrets-pull.sh — regenerate the self-hosted site's .env secret values from Key
# Vault. Run once at setup and again on any secret rotation. Requires `az login`
# with Key Vault Secrets User on the vault (the same access the operator already
# has for the cloud deployment).
#
# It does an in-place UPSERT of each pulled secret into .env, preserving the
# operator-set [you] values (AZURE_PG_HOST, AAF_HOME, image tags). It NEVER prints
# secret values.
set -euo pipefail

VAULT="${KEY_VAULT_NAME:-your-kv}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${1:-$HERE/.env}"

[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found. Copy .env.example -> .env and set the [you] values first." >&2; exit 1; }
az account show >/dev/null 2>&1 || { echo "ERROR: not logged in. Run 'az login' (need Key Vault Secrets User on $VAULT)." >&2; exit 1; }

set_env() { # VAR VALUE — in-place upsert into ENV_FILE
  local var="$1" val="$2" esc
  esc=$(printf '%s' "$val" | sed -e 's/[\\/&|]/\\&/g')
  if grep -q "^${var}=" "$ENV_FILE"; then
    sed -i.bak "s|^${var}=.*|${var}=${esc}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
  else
    printf '%s=%s\n' "$var" "$val" >> "$ENV_FILE"
  fi
}

pull() { # KVNAME ENVVAR — fetch from KV; warn+skip if absent
  local kv="$1" var="$2" val
  if val=$(az keyvault secret show --vault-name "$VAULT" --name "$kv" --query value -o tsv 2>/dev/null); then
    set_env "$var" "$val"; echo "  ok   $var  <- $kv"
  else
    echo "  WARN $var  <- $kv  (secret not found — confirm the KV name)"
  fi
}

echo "Pulling secrets from $VAULT into $ENV_FILE"
pull platform-paperclip-automation-jwt-secret  PAPERCLIP_AUTOMATION_JWT_SECRET
pull platform-paperclip-agent-jwt-secret        PAPERCLIP_AGENT_JWT_SECRET
pull platform-paperclip-admin-email             PAPERCLIP_ADMIN_EMAIL
pull platform-paperclip-admin-password          PAPERCLIP_ADMIN_PASSWORD
pull platform-paperclip-auth-secret             BETTER_AUTH_SECRET
pull platform-memory-governor-api-key           GOVERNOR_API_KEY
pull platform-openai-key                         EMBEDDING_API_KEY
# Router tiers: primary (gpt4o) + budget fallback (phi4).
pull platform-gpt4o-api-key                      GPT4O_API_KEY
pull platform-phi4-uri-target                    PHI_BASE_URL
pull platform-phi4-api-key                        PHI_API_KEY
# The shared tunnel token (one tunnel, two connectors).
pull platform-cloudflared-token                  CLOUDFLARE_TUNNEL_TOKEN
# Shared managed PG admin password (the database never moves; both sites use it).
pull platform-postgres-admin-password            POSTGRES_PASSWORD

# The DB URLs in docker-compose.yml need a percent-encoded password — libpq /
# SQLAlchemy URI parsing rejects raw special characters in the userinfo component.
if pw=$(grep '^POSTGRES_PASSWORD=' "$ENV_FILE" | cut -d= -f2-); then
  enc=$(V="$pw" python3 -c 'import urllib.parse,os; print(urllib.parse.quote(os.environ["V"], safe=""))')
  set_env POSTGRES_PASSWORD_URLENC "$enc"
  echo "  ok   POSTGRES_PASSWORD_URLENC  (derived, percent-encoded)"
fi

chmod 600 "$ENV_FILE"
echo
echo "Done (.env chmod 600)."
echo "Verify completeness — any container that crash-loops on a missing env needs"
echo "its secret added above. Confirm AZURE_PG_HOST and the *_IMAGE_TAG pins are"
echo "set in $ENV_FILE."
