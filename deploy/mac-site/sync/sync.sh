#!/usr/bin/env bash
# Hourly local -> cloud file-share sync — keeps the WARM STANDBY fresh.
#
# The self-hosted site is the PRIMARY and is authoritative for the file shares
# while it holds the lease. The DATABASE is SHARED (both sites use the same
# managed PostgreSQL Flexible Server) and is NEVER synced — no database dumps or
# restores. Only the two file shares move, local -> cloud, so a failover to the
# cloud standby starts from shares at most an hour stale.
#
# Fired by com.azureagentforge.sync.plist (hourly). Prereqs on the host:
#   - az login (Key Vault Secrets User) + azcopy login (Storage File Data
#     Privileged Contributor on the storage account — this direction WRITES)
#   - libpq (psql) for the freshness event
#
# Safe by construction: it only runs when the lease mirror says THIS site is live
# (local). When the lease is cloud, pushing local shares over the cloud's would
# clobber the authoritative copies — so it skips (exit 0; the skip is the normal
# steady state after a failover, not an error).
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"   # deploy/mac-site
# shellcheck disable=SC1091
set -a; . "$HERE/.env"; set +a
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-your-storage-account}"
SHARES="$AAF_HOME/shares"; LOGS="$AAF_HOME/logs"
mkdir -p "$SHARES/paperclip-data" "$SHARES/hermes-data" "$LOGS"

echo "[$(date -u +%FT%TZ)] sync start (local -> cloud file shares, warm-standby refresh)"

# Only when this site is LIVE.
if [ ! -f "$AAF_HOME/active-site" ] || \
   [ "$(tr -d '[:space:]' < "$AAF_HOME/active-site")" != "local" ]; then
  echo "SKIP: lease mirror does not say 'local' — the cloud site is (or may be) live;"
  echo "      pushing local shares over its authoritative copies would clobber state."
  exit 0
fi

# 1. File shares: local (authoritative) -> cloud (warm standby).
for share in paperclip-data hermes-data; do
  echo "  azcopy sync $share (local -> cloud)"
  azcopy sync "$SHARES/$share" "https://$STORAGE_ACCOUNT.file.core.windows.net/$share" \
    --recursive --delete-destination=true
done

# 2. Stamp freshness into the SHARED agent_events spine (honcho DB) so the
#    watchdog's stale-sync detector knows the standby's shares are current.
PGPASSWORD="$POSTGRES_PASSWORD" psql \
  "host=$AZURE_PG_HOST port=5432 dbname=honcho user=$POSTGRES_USER sslmode=require" \
  -v ON_ERROR_STOP=1 -c \
  "INSERT INTO agent_events (actor_peer, event_type, channel, payload)
   VALUES ('local-sync', 'standby_sync_completed', 'system',
           jsonb_build_object('host', '$(hostname)', 'direction', 'local_to_cloud', 'at', now()::text));"

echo "[$(date -u +%FT%TZ)] sync complete"
