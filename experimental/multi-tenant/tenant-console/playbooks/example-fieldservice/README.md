# Field-service inspection vertical playbook pack

An example vertical playbook pack for the Tenant Console. This is a neutral,
invented example (an equipment-inspection field service) — it demonstrates the
pack mechanism, not a real business. A pack is everything the provisioning
executor needs to turn one row of tenant config into a working vertical
deployment: agent prompts, the intake skill, seeded workspace memory, and a
smoke-conversation fixture — all templated on a small, declared variable set.

## Contents

```
playbooks/example-fieldservice/
├── pack.yaml                        # manifest: agents, variables, file pointers
├── agents/
│   ├── intake.AGENTS.md.tmpl        # Band-1 intake (customer-facing pattern)
│   └── coordinator.AGENTS.md.tmpl   # Band-2 tenant coordinator (lean orchestrator pattern)
├── skills/
│   └── intake-assessment.md         # V1 7-question intake
├── seed-memories.yaml               # pinned/durable_fact seeds for the tenant workspace
└── smoke-fixture.yaml               # verification-lane conversation + mechanical asserts
```

The loader/renderer lives at `src/tenantconsole/playbook.py` (tests: `tests/`). It is the pack's contract with the executor: `load_pack` → `validate_pack` → `render_agent` / `load_seed_memories` / `load_smoke_fixture`.

## How the wizard fills it

Template variables are `{{name}}` — double curly braces, identifier names, nothing else (no sections, no conditionals, no filters; logic belongs in the executor). Every variable used anywhere in the pack is declared in `pack.yaml` under `variables:` with a description, an example, and whether the wizard must collect it (`required: true`) or the executor derives it (`required: false` — e.g. `workspace` = `tenant-<tenant_slug>`, `vertical` = fixed by the pack).

The console wizard walks the `required: true` variables as form fields, derives the rest, renders a full preview (preview-first — nothing executes on submit), and then the executor:

1. creates the PaperClip company + the two agents from `agents:` (model + toolsets from the manifest, name from `name_template`, reporting line from `reports_to`), uploading each rendered `*.AGENTS.md.tmpl` as the agent's instructions;
2. renders `seed-memories.yaml` and writes each entry into the tenant workspace via the governor — idempotently, upserting on `seed_key`;
3. registers budgets and channel bindings (outside this pack — see the tenant contract in DESIGN.md);
4. runs `smoke-fixture.yaml` against the intake agent and asserts the `expect` block mechanically.

Rendering is strict: any `{{placeholder}}` without a matching variable raises with the full list of missing names, and `validate_pack` fails any pack that uses an undeclared variable — so a template typo dies in preflight, not mid-provision.

## How to add a vertical

A second pack is the proof the abstraction holds. To add one:

1. `cp -r playbooks/example-fieldservice playbooks/<vertical>` and set `vertical:` + `display_name:` in `pack.yaml`.
2. Re-skin the seven questions in `skills/<vertical>-assessment.md`: keep the base V1 flow, swap the Q1–Q5 domain framing, keep the budget-bucket enum and follow-up contact verbatim.
3. Update both agent templates' lane text and hard rules to the trade (never-quote-prices and the sandbox rules are invariant across verticals — keep them).
4. Rewrite `seed-memories.yaml` content for the trade; keep the field vocabulary (`memory_class`, `scope_kind: workspace`, `source_type: operator_entered`, `verification_state: confirmed`) and unique `seed_key`s.
5. Write a fresh `smoke-fixture.yaml` prompt in the vertical's voice; keep `must_not_contain` price-quote tripwires.
6. Declare any new variables in `pack.yaml` (or better: don't — the canonical ten cover a services vertical) and run the pack through the test suite.

## Invariants (do not loosen per-vertical)

- Intake is Band 1, sandboxed to `{{workspace}}` only; coordinator is Band 2, same partition. Neither ever routes to platform agents.
- No agent quotes prices. Pricing tiers are seeded memory for the human follow-up team.
- Frontmatter carries `voice_id`/`color`/`emoji`/`vibe` only — never `model` (that lives in `pack.yaml`).
- Scope-guard fences (`<!-- scope-guard:start/end -->`), the `## Band` section, the No-Cancel-Without-Comment gate, and the platform-failure refusal protocol are structural — every template keeps all four.
