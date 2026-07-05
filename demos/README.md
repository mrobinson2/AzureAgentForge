# Demos

Reproducible, version-controlled sources for the governance/safety GIFs and
screenshot shown in the project README. Everything renders deterministically
from these sources — no manual screen recording.

| Source | Renders | Shows |
|---|---|---|
| `governance-refusal.tape` | `docs/assets/governance-refusal.gif` | The "delete the whole resource group" request being refused — scope-guard + forbidden-tool reasoning, zero executor children. |
| `destroy-gate.tape` | `docs/assets/destroy-gate.gif` | The destroy-aware approval gate: a create-only plan applies; a plan with a delete trips a second approval. |
| `forge-console-shot.mjs` | `docs/assets/destroy-approval-dialog.png` | The red destructive-apply dialog in the installer console UI. |

## Prerequisites

```sh
brew install vhs                    # terminal GIF renderer (declarative .tape)
npx playwright install chromium     # headless browser for the screenshot
```

The Python demos use the repo virtualenv `.forge-venv` (note: bare `python` is
not on PATH). Create it once:

```sh
./forge --help          # the launcher creates .forge-venv on first real run, OR:
python3 -m venv .forge-venv && ./.forge-venv/bin/pip install -r installer/requirements.txt
```

## Regenerate everything

```sh
./demos/make-demos.sh           # all assets
./demos/make-demos.sh gifs      # just the two terminal GIFs
./demos/make-demos.sh shot      # just the console screenshot
```

Render a single GIF directly:

```sh
vhs demos/governance-refusal.tape
vhs demos/destroy-gate.tape
node demos/forge-console-shot.mjs
```

## Sanitization — read before committing

This is a **public** repo. The sources are written to render with a generic
`demo$` prompt and relative paths so no absolute home path or personal
identifier is ever drawn into the image.

After rendering, **eyeball every file in `docs/assets/`** (open the GIF/PNG)
and confirm none of the following are visible in the pixels:

- an absolute home directory path,
- any personal name or contact detail,
- internal/company/project identifiers.

A text scanner cannot read text baked into image pixels — only your eyes can.
If you change a `.tape`, re-check the rendered frames.
