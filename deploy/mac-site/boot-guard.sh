#!/usr/bin/env bash
# boot-guard.sh — split-brain backstop for the self-hosted PRIMARY site.
#
# Docker's `restart: unless-stopped` brings the aaf-* containers back after a
# reboot WITHOUT lease-guard re-running (lease-guard only gates `compose up`, not
# restart-after-boot). If the lease moved to the cloud standby while this host was
# off, that reboot would split-brain the platform. This guard runs at load and
# every 300s (com.azureagentforge.boot-guard.plist) and stops the stack ONLY when
# the Key Vault lease reads a DEFINITIVE value that is not "local" while aaf-*
# containers are running.
#
# On ANY az/Key Vault failure — offline, expired login, transient 5xx — it logs
# and does NOTHING: never kill the live site on a transient network failure. The
# local mirror file is deliberately NOT trusted here; the whole point is to catch
# the case where the mirror is stale.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
AAF_HOME="${AAF_HOME:-$HOME/aaf}"
VAULT="${KEY_VAULT_NAME:-your-kv}"
LOG_DIR="$AAF_HOME/logs"
LOG="$LOG_DIR/boot-guard.log"
mkdir -p "$LOG_DIR"
log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }

# Anything of ours running? If not (or docker itself is down) there is nothing to
# guard — exit quietly.
running="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -c '^aaf-' || true)"
if [ "${running:-0}" -eq 0 ]; then
  exit 0
fi

# Read the AUTHORITATIVE lease from Key Vault. Any az failure => indeterminate =>
# no-op (never kill on a transient failure).
if ! lease="$(az keyvault secret show --vault-name "$VAULT" \
      --name platform-active-site --query value -o tsv 2>>"$LOG")"; then
  log "Key Vault unreachable — lease indeterminate; leaving $running running container(s) alone"
  exit 0
fi
lease="$(printf '%s' "$lease" | tr -d '[:space:]')"
if [ -z "$lease" ]; then
  log "Key Vault returned an EMPTY lease — indeterminate; doing nothing"
  exit 0
fi

if [ "$lease" = "local" ]; then
  # Definitive and ours — the containers are supposed to be up. Stay quiet.
  exit 0
fi

log "DEFINITIVE foreign lease '$lease' with $running aaf-* container(s) running — stopping the stack (split-brain guard)"
if docker compose -f "$HERE/docker-compose.yml" --profile live --profile jobs down >>"$LOG" 2>&1; then
  log "stack stopped"
else
  log "compose down FAILED — investigate immediately"
fi
# Desktop notification (macOS). No-op elsewhere (e.g. inside WSL) — check the log.
osascript -e "display notification \"Lease is '$lease' — the local stack was stopped to prevent split-brain.\" with title \"AzureAgentForge boot-guard\"" >/dev/null 2>&1 || true
exit 0
