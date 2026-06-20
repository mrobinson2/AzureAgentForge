#!/usr/bin/env bash
# One-command local full stack.
#   scripts/local-stack.sh up           preflight, .env bootstrap, build, wait, smoke, print URLs
#   scripts/local-stack.sh down [args]  stop and remove (pass -v to drop volumes)
#   scripts/local-stack.sh smoke        run the health gate against a running stack
#   scripts/local-stack.sh logs [svc]   follow logs
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE="docker compose"

up() {
  for s in apps/hermes/src apps/honcho/src; do
    if [ -z "$(ls -A "$s" 2>/dev/null || true)" ]; then
      echo "[local-stack] initializing submodule $s…"; git submodule update --init "$s"
    fi
  done
  if [ ! -f .env ]; then
    cp .env.example .env
    echo "[local-stack] created .env from .env.example — edit it to add a model key to drive agents."
  fi
  $COMPOSE --profile full up -d --build
  echo "[local-stack] waiting for services to become healthy (up to 5 min)…"
  deadline=$(( $(date +%s) + 300 ))
  while :; do
    unhealthy="$($COMPOSE ps --format '{{.Service}} {{.Health}}' 2>/dev/null | awk '$2!="" && $2!="healthy"{print $1}')"
    [ -z "$unhealthy" ] && break
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "[local-stack] timeout; still unhealthy: $unhealthy"; $COMPOSE ps; exit 1
    fi
    sleep 5
  done
  scripts/smoke-local.sh
  cat <<EOF

[local-stack] up. URLs:
  PaperClip UI/API : http://localhost:${PAPERCLIP_PORT:-3100}
  Model router     : http://localhost:${ROUTER_PORT:-8080}
  Honcho           : http://localhost:${HONCHO_PORT:-8000}
  Memory governor  : http://localhost:${GOVERNOR_PORT:-8090}

Set OPENAI_COMPAT_BASE_URL + OPENAI_COMPAT_API_KEY in .env to drive agents.
EOF
}

case "${1:-up}" in
  up)    up ;;
  down)  shift; $COMPOSE --profile full down "$@" ;;
  smoke) scripts/smoke-local.sh ;;
  logs)  $COMPOSE --profile full logs -f "${2:-}" ;;
  *)     echo "usage: $0 {up|down [-v]|smoke|logs [service]}"; exit 2 ;;
esac
