#!/usr/bin/env bash
# pg-firewall-update.sh — keep the home WAN IP allowed on the SHARED managed PG.
#
# The database never moves: both sites talk to the public endpoint of the shared
# PostgreSQL Flexible Server, which is firewalled to known IPs. Home ISPs rotate
# WAN addresses, so this runs every 900s via com.azureagentforge.pg-firewall.plist:
# look up the current IPv4, compare to the cache, and on change upsert the
# `home-ip` firewall rule (`az ... firewall-rule create` is an idempotent upsert).
set -u

AAF_HOME="${AAF_HOME:-$HOME/aaf}"
PG_SERVER="${PG_SERVER:-your-pg}"
RG="${RG:-your-rg}"
CACHE="$AAF_HOME/wan-ip.cache"
LOG_DIR="$AAF_HOME/logs"
LOG="$LOG_DIR/pg-firewall.log"
mkdir -p "$LOG_DIR"
log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }

ip="$(curl -4 -s --max-time 15 https://ifconfig.me || true)"
if ! printf '%s' "$ip" | grep -Eq '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'; then
  log "WAN IP lookup failed or non-IPv4 ('$ip') — skipping this cycle"
  exit 0
fi

cached="$(cat "$CACHE" 2>/dev/null || true)"
if [ "$ip" = "$cached" ]; then
  exit 0
fi

log "WAN IP changed '${cached:-<none>}' -> '$ip' — upserting firewall rule home-ip on $PG_SERVER"
if az postgres flexible-server firewall-rule create \
     --rule-name home-ip --name "$PG_SERVER" --resource-group "$RG" \
     --start-ip-address "$ip" --end-ip-address "$ip" >>"$LOG" 2>&1; then
  printf '%s' "$ip" > "$CACHE"
  log "firewall rule updated; cache written"
else
  log "az firewall-rule create FAILED — cache NOT updated (will retry next cycle)"
fi
exit 0
