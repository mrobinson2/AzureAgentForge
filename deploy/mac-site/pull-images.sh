#!/usr/bin/env bash
# pull-images.sh — keep the PRIMARY self-hosted site on fresh registry images.
#
# Runs every 600s via com.azureagentforge.image-pull.plist: registry login,
# compose pull, and — only if an image actually changed — roll the live profile.
# MUST be a cheap no-op when the lease is not "local": it checks the local mirror
# file ONLY (no az call), because this fires every 10 minutes.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
AAF_HOME="${AAF_HOME:-$HOME/aaf}"
ACR_NAME="${ACR_NAME:-your-registry}"
LOG_DIR="$AAF_HOME/logs"
LOG="$LOG_DIR/image-pull.log"
mkdir -p "$LOG_DIR"
log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }

# No-op unless the lease mirror says this site is live (cheap file check only).
if [ ! -f "$AAF_HOME/active-site" ] || \
   [ "$(tr -d '[:space:]' < "$AAF_HOME/active-site")" != "local" ]; then
  exit 0
fi

az acr login --name "$ACR_NAME" >>"$LOG" 2>&1 || { log "az acr login failed — skipping this cycle"; exit 0; }

cd "$HERE"

# Snapshot local image IDs for every service (all profiles), pull, re-snapshot.
image_ids() {
  docker compose --profile live --profile jobs config --images 2>/dev/null | sort -u | \
    while IFS= read -r img; do
      docker image inspect --format '{{.Id}}' "$img" 2>/dev/null || printf 'missing:%s\n' "$img"
    done
}

before="$(image_ids)"
docker compose --profile live --profile jobs pull --quiet >>"$LOG" 2>&1 || { log "compose pull failed — skipping this cycle"; exit 0; }
after="$(image_ids)"

if [ "$before" != "$after" ]; then
  log "image digest change detected — rolling the live profile"
  if docker compose --profile live up -d >>"$LOG" 2>&1; then
    log "live profile rolled onto the new images"
  else
    log "compose up -d FAILED after image change — investigate"
  fi
else
  log "no image changes"
fi
exit 0
