<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Agent Roles

An **[agent](GLOSSARY.md#agent)** here isn't one general-purpose assistant — it's a team of 14, each scoped to a job (coding, security review, cost watching, and so on) and each allowed only the tools that job needs. AzureAgentForge uses this 14-role model. Each role is defined by a YAML profile in [`../agents/profiles/`](../agents/profiles/), validated against a JSON schema, and resolved to a concrete model deployment at runtime by the [model router](GLOSSARY.md#model-router).

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

`Orchestrator` is the single entry point. All other roles report up through the tree above. Work arrives at `Orchestrator`, which classifies it and delegates to the appropriate specialist.

## Role summary

| Name           | Role            | Tier     | Toolsets                | Reports to     |
|----------------|-----------------|----------|-------------------------|----------------|
| Orchestrator   | orchestrator    | frontier | terminal, file          | -              |
| Coder          | coder           | standard | terminal, file, browser | Orchestrator   |
| CostGuardian   | cost-guardian   | economy  | terminal, file          | Orchestrator   |
| Curator        | curator         | economy  | terminal, file          | Orchestrator   |
| Generalist     | generalist      | economy  | terminal, file          | Orchestrator   |
| Infrastructure | infrastructure  | standard | terminal, file          | Orchestrator   |
| Strategy       | strategy        | standard | terminal, file          | Orchestrator   |
| QA             | qa              | economy  | terminal, file          | Coder          |
| Security       | security        | standard | terminal, file          | Infrastructure |
| Planner        | planner         | economy  | terminal, file          | Strategy       |
| Business       | business        | economy  | file                    | Planner        |
| Coach          | coach           | economy  | file                    | Planner        |
| Researcher     | researcher      | frontier | terminal, file, browser | Planner        |
| Psychology     | psychology      | economy  | file                    | Business       |

## Profile schema

Each profile is a YAML file in [`../agents/profiles/`](../agents/profiles/) validated against [`../agents/profile.schema.json`](../agents/profile.schema.json).

| Field        | Type             | Required | Description                                              |
|--------------|------------------|----------|----------------------------------------------------------|
| `name`       | string           | yes      | Display name used in logs and task headers               |
| `role`       | string           | yes      | Stable identifier (slug); used as the routing key        |
| `description`| string           | yes      | One-sentence purpose statement                           |
| `model_tier` | enum (see below) | yes      | Abstract tier resolved by the model router at runtime    |
| `toolsets`   | array of enums   | yes      | Capability grants for this role (see below)              |
| `reports_to` | string or null   | yes      | Parent role name; `null` for the root (Orchestrator)     |

### `model_tier` values

| Value      | Meaning                                                                     |
|------------|-----------------------------------------------------------------------------|
| `frontier` | Highest-capability deployment, for complex reasoning and coordination       |
| `standard` | Mid-tier deployment, for implementation and structured tasks                |
| `economy`  | Lowest-cost deployment, for focused, well-scoped subtasks                   |

`model_tier` is a human-facing label. The router does not read it directly. The operational role-to-deployment mapping is configured separately via `PERSONA_TIERS_JSON`, which maps each role to a concrete registered tier key (e.g. `gpt4o-mini`, `phi4`). Keep that mapping consistent with the labels here: a `frontier` role should point at your most capable deployment. See [`../services/model-router/README.md`](../services/model-router/README.md) and [`../services/model-router/persona-tiers.example.json`](../services/model-router/persona-tiers.example.json).

### `toolsets` values

| Value      | Grants                                   |
|------------|------------------------------------------|
| `terminal` | Shell execution and CLI tool access      |
| `file`     | File read/write access                   |
| `browser`  | Web browsing and HTTP fetch capability   |

## Example profile

```yaml
name: Orchestrator
role: orchestrator
description: >-
  Single front door for the team. Classifies incoming work and either answers,
  delegates to a specialist, or opens a tracked task. Does not write code or
  change infrastructure directly.
model_tier: frontier
toolsets: [terminal, file]
reports_to: null
```

## Adding an agent

1. Create [`../agents/profiles/<role>.yaml`](../agents/profiles/) following the schema above.
2. Run the validator from the repo root:
   ```
   python agents/validate_profiles.py
   ```
   Expected output: `OK: <N> profiles valid.`
3. If the new role needs a non-default tier mapping, add an entry to your `persona-tiers.json` override. See [`../services/model-router/README.md`](../services/model-router/README.md) for details.

---

For the full agent directory and source profiles, see [`../agents/`](../agents/).
