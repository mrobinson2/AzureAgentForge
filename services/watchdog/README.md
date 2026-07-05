# Watchdog

Platform self-watch: detects failure signatures across run results +
`agent_events`, files deduped issues with evidence, and (optionally) writes the
failure as a governed memory lesson so the agent that keeps hitting it gets
reminded. Design: [`docs/design/memory-system.md`](../../docs/design/memory-system.md)
(the self-improvement loop).

## Run

```bash
# Gated: does nothing unless feature_flags.AGENT_EVENTS_ENABLED is true.
export WATCHDOG_COMPANY_ID=<company-uuid>
export WATCHDOG_JWT=$(...)            # admin/automation JWT
export AGENT_EVENTS_DSN=postgresql://.../honcho?sslmode=require
python -m services.watchdog.watchdog   # one pass — wire to a 10-min cron
```

## Layout

| File | Role | Tested |
|---|---|---|
| `detectors.py` | pure failure-signature library | ✅ offline |
| `filer.py` | Finding → PaperClip issue (camelCase) | ✅ offline (stub poster) |
| `memory.py` | Finding → governed durable_fact lesson | ✅ offline (stub poster) |
| `scorecards.py` | per-agent delegation track records | ✅ offline |
| `attribution.py` | success → earned-trust attribution | ✅ offline |
| `roster.py` | display name ↔ live agent slug | ✅ offline |
| `watchdog.py` | orchestrator: fetch → detect → dedup → file | I/O glue |

## Tests

```bash
python -m pytest services/watchdog/tests -q   # offline — no network/DB
```

## Adding a detector

A detector is a pure `fn(runs, events, ...) -> list[Finding]`. Add one when a
new failure class is worth surfacing to an operator; describe it in its
docstring; register it in `run_detectors()`; add a test with a synthetic window.
