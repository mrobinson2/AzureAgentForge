# Agent Templates

> **Technical reference for contributors.** For the operational overview, start at [README](../../README.md) or [Architecture](../../docs/architecture.md).

Starting points for building a new agent role. Where [`../profiles/`](../profiles/) holds the **validated, shipped role instances**, this directory holds the **reusable templates** you adapt when you need a role the profiles don't already cover — or when you want to understand the house pattern before editing a profile.

Every file here is generic and sanitized: no persona names, no tenant names, no real hostnames or secrets. Fill the placeholders, keep the load-bearing patterns, and you get a role prompt that behaves like the ones already in `../profiles/`.

## What's in here

| File | What it is | Use it when |
|---|---|---|
| [`AGENTS.template.md`](AGENTS.template.md) | The **annotated blank skeleton** — the full section order every role prompt follows, with `<PLACEHOLDER>` fields and inline `<!-- guidance: … -->` comments naming each proven pattern and why it exists. | You're building a brand-new role from scratch and want the master fill-in template. |
| [`coordinator.AGENTS.template.md`](coordinator.AGENTS.template.md) | A **filled, generic front-door coordinator** (frontier tier): classifies inbound work and routes it to specialists; never writes code or changes infra directly. Includes the Proof-of-Source and Proof-of-Delegation gates. | You need a router / chief-of-staff role — the single entry point that delegates rather than executes. |
| [`intake.AGENTS.template.md`](intake.AGENTS.template.md) | A **filled, generic customer-facing intake agent** (voice/chat/SMS front desk): greets external parties, qualifies leads, and produces a structured assessment payload for internal roles. Low-trust sandbox with strong approval gates. | You need a customer-facing surface that must never read internal memory and never commit, quote, or agree without human approval. |
| [`small-model-overlay.md`](small-model-overlay.md) | An **overlay, not a role**: additional constraints you prepend-at-the-end to any role's prompt when it runs on a small / economy-tier model. | You're downgrading a role to a cheap model and need to compensate for weaker instruction-following. |

## The house pattern

Every role prompt (template or profile) has the same shape. From top to bottom:

1. **YAML frontmatter** — `role`, `voice_id`, `color`, `emoji`, `vibe` (metadata for deploy/provision scripts; no `model` field).
2. **Scope guard** — the role's lane in three sentences, and what to do with off-lane work. Read first, always.
3. **Identity** and **Trust Tier & Cross-Tier Notes** — who the role is and which memory/peers it may and may not touch.
4. **No-Cancel-Without-Comment gate** — honest, auditable cancellation.
5. **Picking the right issue** — select work from the issue/agent API, not the filesystem.
6. **In-Scope / Out-of-Scope** — the Out-of-Scope table routes every off-lane category to a *named* role.
7. **Allowed / Forbidden Tools** — the concrete tools, and the explicit never-list.
8. **Honcho Memory Access**, **Tool Discipline**, **Self-Test**, **Escalation Triggers**.
9. **Platform-failure refusal protocol** — the honest-failure path that's distinct from an out-of-lane refusal.
10. **Memory Contract** (Current vs Design Target) and **Identity Reminder**.

The recurring, proven patterns the templates annotate: **identity + scope**, **tool-truth** (HTTP 2xx = success; never claim success you can't show), **refuse-with-a-named-human** (route off-lane work to a specific role, never silently mishandle), **approval gates** (draft-then-human-approves for any external side effect), **the never-list** (Forbidden Tools), and **budget awareness** (a bounded retry budget / cost envelope).

## Composing a working agent

A deployable agent is three layers:

1. **Pick and adapt a role template.** Start from `AGENTS.template.md` for a new role, or from `coordinator`/`intake` if one matches your need. Fill every `<PLACEHOLDER>`, keep the load-bearing patterns, delete the `<!-- guidance -->` comments once you've internalized them. (Or, if a shipped profile in [`../profiles/`](../profiles/) is close, copy and adapt that instead — see below.)
2. **Attach the relevant skills.** A role's prompt defines its lane; a **skill** teaches it a concrete procedure inside that lane. Pick the skills the role needs from the [Skills Library](../../docs/skills/README.md) and grant them to the role. The skill specs there (business assessment, executive-assistant digest/triage/prep, security review, and others) carry their own guardrails and approval gates that compose with the role's.
3. **Optionally layer the small-model overlay.** If the role will run on an economy-tier model, append [`small-model-overlay.md`](small-model-overlay.md) to the end of the composed prompt (in a working copy — don't overwrite the canonical role file). On a frontier model, skip it: those rules are net-negative on a capable model.

The result — role prompt + attached skills (+ optional overlay) — is what gets deployed as the agent's system prompt.

## Relationship to `../profiles/`

**Templates are starting points; [profiles](../profiles/) are the shipped, validated instances.** The profiles are the concrete role prompts (14 roles — Orchestrator, Coder, Infrastructure, Security, Researcher, Strategy, Planner, Business, Coach, Psychology, Curator, CostGuardian, QA, Generalist) that ship with the reference platform, each validated against the profile schema and wired into the role hierarchy. See [`../README.md`](../README.md) for the roster, the hierarchy, and the tool grants.

Use a template when you're inventing a role the profiles don't cover, or learning the pattern. Use a profile — copy and adapt it — when a shipped role is close to what you need. Either way, the section structure and the proven patterns stay the same; that consistency is what makes the fleet auditable.
