#!/usr/bin/env bash
# make-demos.sh — (re)render every demo asset into docs/assets/.
#
#   ./demos/make-demos.sh          render everything (VHS gifs + Playwright png)
#   ./demos/make-demos.sh gifs     only the VHS terminal gifs
#   ./demos/make-demos.sh shot     only the console screenshot
#
# Tools (install once):
#   brew install vhs
#   npx playwright install chromium
#
# The repo venv (.forge-venv) must exist — run ./forge once, or:
#   python3 -m venv .forge-venv && ./.forge-venv/bin/pip install -r installer/requirements.txt
#
# PUBLIC REPO: after rendering, eyeball every file in docs/assets/ for any
# absolute home path or personal identifier before committing. The demos use a
# generic 'demo$' prompt and relative paths, but verify the pixels yourself.
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root

WHAT="${1:-all}"
mkdir -p docs/assets

render_gifs() {
  if ! command -v vhs >/dev/null; then
    echo "make-demos: vhs not found — install with: brew install vhs" >&2
    return 1
  fi
  for tape in demos/governance-refusal.tape demos/destroy-gate.tape; do
    echo "==> vhs $tape"
    vhs "$tape"
  done
}

render_shot() {
  if [[ ! -d node_modules/playwright && ! -d node_modules/.bin ]]; then
    echo "make-demos: installing playwright (npm)…"
    npm install --no-save playwright >/dev/null 2>&1 || true
  fi
  echo "==> node demos/forge-console-shot.mjs"
  node demos/forge-console-shot.mjs
}

case "$WHAT" in
  gifs) render_gifs ;;
  shot) render_shot ;;
  all)  render_gifs; render_shot ;;
  *)    echo "usage: $0 [all|gifs|shot]" >&2; exit 2 ;;
esac

echo
echo "Rendered into docs/assets/. Eyeball each file for home-path / personal data"
echo "before committing — this is a public repo."
