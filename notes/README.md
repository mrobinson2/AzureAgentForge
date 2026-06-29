# notes

Build-process artifacts from developing AzureAgentForge: the design specs and
implementation plans behind shipped features. They record how and why each piece
was built.

These are not setup or usage docs. If you're deploying AAF or building your own
agentic stack on it, start with the [README](../README.md) and
[docs/getting-started.md](../docs/getting-started.md). For architecture and
design written for adopters, see [docs/architecture.md](../docs/architecture.md)
and [docs/design/](../docs/design/).

## What's here

- `specs/`: design docs written before building a feature. The "why" and the shape of the change.
- `plans/`: task-by-task implementation plans used to build it.

Both are point-in-time. Once a feature ships, its plan is mostly history; a spec
may still explain a design decision. Kept for contributors and as a record of the
process, not maintained as living documentation.
