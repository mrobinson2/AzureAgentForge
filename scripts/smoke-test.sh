#!/usr/bin/env bash
# Post-deploy smoke test: confirm the deployed Container Apps are healthy.
#
# This is the thin collector. It runs `az containerapp show` for each app and
# optional `curl` HTTP probes, assembles one JSON payload, and pipes it to
# `python -m installer.smoke`, which holds the pass/fail logic and is unit-
# tested offline (installer/tests/test_smoke.py). The smoke module exits
# non-zero on any unhealthy app, so this script's exit code gates the deploy.
#
# Default apps are the always-present cost-optimized set (ca-paperclip / ca-hermes
# / ca-honcho). Add gated ones with --apps when you enable them
# (memory-governor, cloudflared) or pass an explicit list.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENVNAME="dev"
RG=""
APPS=""
EXPECTED=""
URLS=""
DRY_RUN=0

die() { echo "error: $*" >&2; exit 2; }

usage() {
  cat <<'EOF'
Usage: scripts/smoke-test.sh --resource-group RG [options]

  -g, --resource-group RG  Azure resource group of the deployment. Required.
  -e, --env ENV            Environment suffix for default app names. Default: dev.
  -a, --apps "A B C"       Container apps to check. Default: the cost-optimized
                           core (ca-paperclip-ENV ca-hermes-ENV ca-honcho-ENV).
      --expected "A B"     Apps that MUST be present. Default: same as --apps.
  -u, --url URL            HTTP probe (repeatable); passes on 2xx/3xx.
  -n, --dry-run            Don't call az/curl; show the assembled payload shape.
  -h, --help               This help.

Exit code is 0 only when every app is provisioned and every probe is 2xx/3xx.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -g|--resource-group) RG="${2:-}"; shift 2 ;;
    -e|--env) ENVNAME="${2:-}"; shift 2 ;;
    -a|--apps) APPS="${2:-}"; shift 2 ;;
    --expected) EXPECTED="${2:-}"; shift 2 ;;
    -u|--url) URLS="$URLS ${2:-}"; shift 2 ;;
    -n|--dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[ -n "$RG" ] || [ "$DRY_RUN" -eq 1 ] || die "--resource-group is required (or use --dry-run)"

[ -n "$APPS" ] || APPS="ca-paperclip-$ENVNAME ca-hermes-$ENVNAME ca-honcho-$ENVNAME"
[ -n "$EXPECTED" ] || EXPECTED="$APPS"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "resource_group=${RG:-<rg>} env=$ENVNAME dry_run=$DRY_RUN"
echo "apps: $APPS"
[ -n "$URLS" ] && echo "urls:$URLS"
echo

for app in $APPS; do
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "null" > "$TMP/$app.json"
    echo "    (dry-run) would: az containerapp show -g $RG -n $app -o json"
  else
    az containerapp show -g "$RG" -n "$app" -o json > "$TMP/$app.json" 2>/dev/null \
      || echo "null" > "$TMP/$app.json"
  fi
done

: > "$TMP/http.tsv"
for url in $URLS; do
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '%s\t0\n' "$url" >> "$TMP/http.tsv"
  else
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url" || echo 0)"
    printf '%s\t%s\n' "$url" "$code" >> "$TMP/http.tsv"
  fi
done

# Assemble the payload (JSON plumbing only — no verdict logic lives here).
python3 - "$TMP" "$APPS" "$EXPECTED" > "$TMP/payload.json" <<'PY'
import json, os, sys
tmp, apps_csv, expected_csv = sys.argv[1], sys.argv[2], sys.argv[3]
apps = []
for name in apps_csv.split():
    path = os.path.join(tmp, name + ".json")
    show = None
    if os.path.exists(path):
        try:
            show = json.load(open(path))
        except Exception:
            show = None
    apps.append({"name": name, "show": show})
http = []
ht = os.path.join(tmp, "http.tsv")
if os.path.exists(ht):
    for line in open(ht):
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 2 and parts[0]:
            http.append({"name": parts[0], "url": parts[0], "status": parts[1]})
json.dump({"expected": expected_csv.split(), "apps": apps, "http": http}, sys.stdout)
PY

if [ "$DRY_RUN" -eq 1 ]; then
  echo "assembled payload:"; cat "$TMP/payload.json"; echo; echo
fi

PYTHONPATH="$REPO_ROOT" python3 -m installer.smoke "$TMP/payload.json"
