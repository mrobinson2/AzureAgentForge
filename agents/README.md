<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Agents

This directory contains the agent role model in two layers:

- **YAML profiles** (`profiles/<role>.yaml`) — the machine-readable contract for each generic role: name, model tier, toolsets, and reporting line.
- **System prompts** (`profiles/<role>.AGENTS.md`) — the full, self-contained prompt injected at the top of every task the role runs. This is where each role's lane, skills, guardrails, and escalation rules live.

## Role hierarchy

```
Orchestrator  (frontier)
├── Coder  (standard)
│   └── QA  (economy)
├── CostGuardian  (economy)
├── Curator  (economy)
├── Generalist  (economy)
├── Infrastructure  (standard)
│   └── Security  (standard)
└── Strategy  (standard)
    └── Planner  (economy)
        ├── Business  (economy)
        │   └── Psychology  (economy)
        ├── Coach  (economy)
        └── Researcher  (frontier)
```

`Orchestrator` is the single entry point; all other roles report up through the tree shown above.

## Role summary

| Name          | Role            | Tier     | Toolsets              | Reports to    |
|---------------|-----------------|----------|-----------------------|---------------|
| Orchestrator  | orchestrator    | frontier | terminal, file        | —             |
| Coder         | coder           | standard | terminal, file, browser | Orchestrator |
| CostGuardian  | cost-guardian   | economy  | terminal, file        | Orchestrator  |
| Curator       | curator         | economy  | terminal, file        | Orchestrator  |
| Generalist    | generalist      | economy  | terminal, file        | Orchestrator  |
| Infrastructure| infrastructure  | standard | terminal, file        | Orchestrator  |
| Strategy      | strategy        | standard | terminal, file        | Orchestrator  |
| QA            | qa              | economy  | terminal, file        | Coder         |
| Security      | security        | standard | terminal, file        | Infrastructure|
| Planner       | planner         | economy  | terminal, file        | Strategy      |
| Business      | business        | economy  | file                  | Planner       |
| Coach         | coach           | economy  | file                  | Planner       |
| Researcher    | researcher      | frontier | terminal, file, browser | Planner     |
| Psychology    | psychology      | economy  | file                  | Business      |

## Role system prompts

Each role ships a full system prompt at `profiles/<role>.AGENTS.md` (for example `profiles/coder.AGENTS.md`). Where the YAML profile is the machine-readable contract, the `.AGENTS.md` file is the actual prompt prepended to every task the role runs. Each prompt is a self-contained, adaptable role definition containing:

- a **scope guard** that fixes the role's lane and says what to do with off-lane work (refuse and route back, never silently mis-handle);
- a **No-Cancel-Without-Comment gate** and a **platform-failure refusal protocol** for honest, auditable failure handling;
- **Allowed / Forbidden tool** tables — the per-role skills summarized below;
- **escalation triggers** and a **memory contract** (current behavior plus a clearly-labeled design target).

The prompts are written to be platform-agnostic — adapt hostnames, tool names, and peer IDs to your own stack. They assume a PaperClip-style issue/agent API and an optional Honcho-style memory helper (`pc-honcho`); both are credited open-source components used by this reference platform.

## Skills & tools by role

Each role's `toolsets` (above) are the coarse capability grants; within those grants the system prompt names the concrete tools and skills the role actually uses:

| Role          | Key tools & skills (per its system prompt)                                                                 |
|---------------|------------------------------------------------------------------------------------------------------------|
| Orchestrator  | Classifies and routes all inbound work via the issue/agent API (`terminal` + curl); `pc-delegate` (delegation helper), `pc-honcho` (memory), `web-search`, and email/calendar/Drive skills (`email-read`, `email-send`, `email-archive`, `drive-organize`). Does **not** write code or change infrastructure directly. |
| Strategy      | `terminal` (orchestration only); `file` read/write for decision matrices, roadmaps, OKR sheets; `pc-honcho ask`. |
| Planner       | `terminal` (issue queries and status rollups); `file` read/write for plans and status reports; Calendar API (read-only); `pc-honcho ask`. |
| Business      | `terminal` (orchestration); `file` read/write for playbooks, communication templates, and specs; `pc-honcho ask` / `record`. |
| Coach         | `file` read/write for review notes and feedback only (never the deliverable); `pc-honcho ask`. No terminal. |
| Psychology    | `file` read/write for framing, pattern, and UX notes; `pc-honcho ask`. No terminal.                        |
| Researcher    | `browser` (with a search-API CLI and `curl` as the deployed fallback); `terminal`; `file`; `pc-honcho`.    |
| Coder         | `terminal`; `git` (branch/commit/push/PR, no force-push); language package managers (`npm`/`pip`/`uv`/`cargo`/`go`/`mvn`); `file` read/write for code and tests; `pc-honcho ask`. |
| QA            | `terminal` for test/lint runners (read-only against the system under test); `git` (read-only diffs and PR comments); `file` read/write for tests and reports; `pc-honcho ask`. |
| Infrastructure| `terraform` (`init`/`plan`/`apply`/`state`); `az`; `docker` / ACR builds; `kubectl`; `git`; `terminal`; `file` read/write for IaC and runbooks; `pc-honcho ask`. |
| Security      | Security scanners (`trivy`, `gitleaks`, `semgrep`, OWASP ZAP); `az` (read-only); `terminal` (read-only); `file` read/write for findings and threat models; `pc-honcho ask`. |
| CostGuardian  | `az` cost and billing queries (read-only: `az consumption`, `az costmanagement`); `terminal` (read-only); `file` read/write for cost reports; `pc-honcho ask`. |
| Curator       | `pc-honcho ask` / `record` for memory curation; `terminal`; `file` read/write for documentation and durable-notes entries. |
| Generalist    | `terminal`; `file` read/write for well-scoped general tasks that do not require a narrower specialist; routes specialist work back to Orchestrator. |

Read each role's `.AGENTS.md` for the authoritative tool list, including the matching **Forbidden tools** section (for example, specialists never `terraform apply` or `git push --force`).

## Profile schema

Each profile is a YAML file validated against `profile.schema.json`.

| Field        | Type            | Required | Description                                              |
|--------------|-----------------|----------|----------------------------------------------------------|
| `name`       | string          | yes      | Display name used in logs and task headers               |
| `role`       | string          | yes      | Stable identifier (slug); used as the routing key        |
| `description`| string          | yes      | One-sentence purpose statement                           |
| `model_tier` | enum (see below)| yes      | Abstract tier resolved by the model-router at runtime    |
| `toolsets`   | array of enums  | yes      | Capability grants for this role (see below)              |
| `reports_to` | string or null  | yes      | Parent role name; `null` for the root (Orchestrator)     |

### `model_tier` values

| Value      | Meaning                                                  |
|------------|----------------------------------------------------------|
| `frontier` | Highest-capability deployment — used for complex reasoning and coordination |
| `standard` | Mid-tier deployment — used for implementation and structured tasks |
| `economy`  | Lowest-cost deployment — used for focused, well-scoped subtasks |

`model_tier` is a human-facing capability label. The router does not read it directly; the operational role→deployment routing is configured separately in the model-router via `PERSONA_TIERS_JSON`, which maps each role to a concrete *registered tier key* (e.g. `gpt4o-mini`, `phi4`). Keep that mapping consistent with the labels here — a `frontier` role should point at your most capable deployment. See `services/model-router/README.md` and `services/model-router/persona-tiers.example.json`.

### `toolsets` values

| Value      | Grants                                      |
|------------|---------------------------------------------|
| `terminal` | Shell execution and CLI tool access         |
| `file`     | File read/write access                      |
| `browser`  | Web browsing and HTTP fetch capability      |

## Adding an agent

1. Create `profiles/<role>.yaml` following the schema above.
2. Run the validator:
   ```
   python agents/validate_profiles.py
   ```
   Expected output: `OK: <N> profiles valid.`
3. If the new role needs a non-default tier mapping, add an entry to your `persona-tiers.json` override (see `services/model-router/README.md`).
