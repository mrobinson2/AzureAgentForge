# Skills Library

> **Technical reference for contributors.** For the operational overview, start at [README](../../README.md) or [Architecture](../architecture.md).

A curated set of reusable **skill specs** for AzureAgentForge (AAF) agents. A
skill is a markdown playbook an agent loads at task time: it captures a genuine
methodology — the procedure, the guardrails, the failure handling — in a form an
agent can follow and an operator can audit. Skills are model-agnostic and
tenant-neutral; they describe *how* to do a job, not *who* does it.

These specs are sanitized reference material. Adapt the placeholders
(`example.com` addresses, `EXAMPLE_*` secret names, timezones, channel
registries) to your own deployment before use.

## Skill file format

Every skill file opens with the same header block, then a fixed set of sections:

```
# Skill: <Human Title>

- **Slug:** `<kebab-slug matching the filename>`
- **Used by:** <which agent role(s) — e.g. Business, Researcher, Security>
- **Toolsets:** <terminal / file / browser as relevant>
- **Trust tier:** <High-Trust internal | Customer-facing sandbox | n/a>

## Purpose          — what the skill accomplishes, in a sentence or two
## When to use       — the triggers, and when NOT to use it
## Inputs            — env vars, secrets, and data the skill consumes
## Procedure         — numbered, concrete steps (the load-bearing section)
## Output format     — the exact shape of what the skill produces
## Guardrails        — approval gates, budget/rate awareness, hard "never" rules
## Failure handling  — what to do when a source is empty or a tool fails
```

Individual skills add sections (e.g. *Mode gating*, *State and dedup*, *Edge
cases*) where the work needs them, but the header block is identical across all
of them. `Toolsets` and `Trust tier` mirror the fields in an AAF agent profile
(see [`../agents.md`](../agents.md)).

Anything that sends external communication (email, a message to a channel) is
**draft-only: a human approves before send.** That gate is stated in each such
skill's Guardrails.

## Skills by theme

### Business & advisory

- [**Business AI-Opportunity Assessment**](business-ai-opportunity-assessment.md)
  — Run a structured intake for a small business, then score it across fixed
  dimensions and produce a written recommendation whose deliverable doubles as
  the client's day-one AI system context.

### Executive assistant

- [**Executive-Assistant Daily Digest**](executive-assistant-daily-digest.md)
  — Assemble one morning brief per day: calendar, triaged email, unreplied
  messages, and reminders due, posted as a single issue.
- [**Executive-Assistant Email Triage**](executive-assistant-email-triage.md)
  — Classify the operator's inbox into four buckets, apply reversible labels,
  and draft (never send) replies for time-sensitive threads.
- [**Executive-Assistant Calendar Prep**](executive-assistant-calendar-prep.md)
  — Produce a 30-second pre-meeting primer per event: attendees, memory context,
  agenda-doc metadata, and conflict flags.

### Content

- [**YouTube Channel Digest**](youtube-channel-digest.md)
  — Poll a curated channel registry on a schedule, summarize new uploads with an
  economy-tier model, and deliver a themed digest by email.

### Security

- [**Multi-Tenant Service Security Review**](multi-tenant-python-vuln-scan.md)
  — A static, source-level review method for a Python/Node/Terraform
  multi-tenant service, covering injection, auth, tenant isolation, secret
  handling, and LLM/tool-calling risks.
