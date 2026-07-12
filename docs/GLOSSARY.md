<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Glossary

Plain-language definitions for terms this repo's docs use without stopping to explain — mostly AI/agent vocabulary and a few repo-specific engineering terms. Azure and infrastructure basics (VNet, Key Vault, Container Apps, Terraform) are assumed knowledge for this repo's audience and aren't repeated here.

Each entry links to where the term matters most, so you can go deeper once you know what the word means. Other docs deep-link to specific entries below (e.g. `docs/GLOSSARY.md#litellm`).

## AI and agent concepts

#### LLM

Short for large language model — the AI model that reads a prompt and generates text: GPT-4o, Phi-4, Claude, and similar. Every "model call" in this repo's docs means a request sent to one of these.

#### Agent

A piece of software that uses an LLM to complete multi-step tasks on its own: reading a request, deciding what to do, calling tools (a terminal, a file, a web search), and reporting back. AzureAgentForge runs a team of them, each with a defined role. See [`docs/agents.md`](agents.md).

#### Agent runtime

The program that actually runs an agent: it holds the conversation loop, calls the LLM, executes tool calls, and reports results. AzureAgentForge uses **Hermes** as its agent runtime today. "A second runtime" in the roadmap means a second engine playing the same role.

#### Orchestrator

The one agent role every request hits first. It reads a task, decides which specialist should handle it, and hands it off — it does not do the work itself. See the [governance walkthrough](walkthroughs/governance-and-blast-radius.md) for why that matters for safety.

#### Multi-agent

Instead of one general-purpose AI, the platform runs several narrowly-scoped agents (a coder, a security reviewer, a cost watcher, and so on) that hand work to each other, the way a team of specialists would.

#### Model tier

A cost/capability label (`frontier` / `standard` / `economy`) attached to each agent role, not a specific model name. `frontier` is the most capable, and most expensive, model available; `economy` is the cheapest. A separate lookup table maps each tier to an actual model deployment, so you can swap models without touching every agent's definition. See [`docs/why-azure.md`](why-azure.md).

#### Model router

The internal traffic cop between agents and the LLM provider. It picks the right model for the calling agent's tier, enforces a spending cap, and falls back to a cheaper model if the primary one is unavailable or over budget.

#### Embeddings

A way of turning text into a list of numbers (a "vector") that captures its meaning, so a database can find semantically similar text later even if the wording differs. This repo's agent memory uses embeddings for recall. **pgvector** is the PostgreSQL extension that stores and searches those vectors.

#### Trigram matching

A plain-text search technique — matching overlapping 3-character chunks of words — used as a fallback or complement to vector search. You'll see it as `pg_trgm` (the Postgres extension) or "trigram" in retrieval-related docs.

#### HITL

Short for human-in-the-loop — a control point where an agent must pause and wait for a person to approve an action before it proceeds, instead of acting fully autonomously.

#### Disposition protocol

The house rule that every agent task must end in exactly one clearly labeled outcome (done, blocked, refused — never silence), posted as a comment on the task. It exists so a task can never go quiet without anyone knowing why.

#### Golden replay fixture

A saved, version-controlled example of "here is the input, here is the exact behavior we expect," used as a regression test for agent behavior instead of application code. See [`tests/replay/README.md`](../tests/replay/README.md).

#### Blast radius

How much damage a single action (a command, a compromised credential, a bad decision) could cause if it went wrong. Used throughout the security docs as a way of asking "what's the worst case here."

#### Fail closed and fail open

What a system does when something is misconfigured or breaks. "Fail closed" means it stops and denies the request — the safer default for anything security-related. "Fail open" means it lets the request through anyway — the safer default only when availability matters more than strictness. This repo defaults to fail-closed almost everywhere.

## Engineering-practice terms used in release notes

#### Vendored source

Copying a dependency's source code directly into this repository, pinned to a specific version, instead of pulling it from the internet at build time. AzureAgentForge vendors PaperClip, Hermes, and Honcho so the whole platform builds from one repo.

#### Feature flag

A setting that turns a feature on or off without a code change. "Ships flag-gated off" means the code is in the repo and tested, but inert until an operator deliberately turns it on.

#### Ship-dark

A feature that's fully built and deployed but not yet wired into anything that acts on its output, so it reports what it observes without changing platform behavior. A safe way to prove a feature works before trusting it with real consequences.

#### Canary

An automated test that exercises a real, end-to-end path — not just "did the container start" — to catch failures that health checks miss. Named for the miner's canary.

#### Pydantic settings

A Python library the vendored Honcho app uses to define and validate its configuration. Mentioned here only because AzureAgentForge's config-validation tooling reads that library's model directly to confirm the settings this repo ships still match what Honcho actually reads.

#### AST parse

Short for Abstract Syntax Tree — reading a program's source code to understand its structure, without running it. Used by this repo's config-validation tooling to check what settings the vendored Hermes runtime actually consumes.

#### LiteLLM

The open-source library the model router uses to talk to different LLM providers — Azure AI Foundry, OpenAI, Anthropic, and others — through one common interface.

#### Sidecar

A helper container that runs alongside a main service to do one supporting job (in this repo, mostly routing model calls) without being a separate deployment a user interacts with directly.

#### OIDC

Short for OpenID Connect — the mechanism this repo's deploy pipeline uses to authenticate to Azure without storing a long-lived credential in GitHub. GitHub proves its identity token-for-token at deploy time instead.

#### RLS

Short for Row-Level Security — a PostgreSQL feature that restricts which rows a database query can see, enforced by the database itself rather than trusted to application code. Used here to back up tenant isolation in the multi-tenant design.

#### IaC

Short for Infrastructure as Code — describing your cloud infrastructure (servers, databases, networks) as version-controlled configuration files instead of clicking through a portal. This repo's IaC is written in Terraform.

---

Still hit an unfamiliar term? Most docs link the concept to where it's implemented — follow the link rather than guessing. If a term is missing here and it should be, that's a documentation bug; open an issue.
