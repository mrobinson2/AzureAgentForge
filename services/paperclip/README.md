# Paperclip Orchestrator

> **Technical reference for contributors.** For the operational overview, start at [README](../../README.md) or [Architecture](../../docs/architecture.md).

The AI-agent-company orchestrator AzureAgentForge runs on. Unlike Hermes and
Honcho (git submodules under `apps/`), Paperclip is not vendored as source in
this repo — [`Dockerfile`](./Dockerfile) clones the upstream
[`paperclipai/paperclip`](https://github.com/paperclipai/paperclip) repo at a
pinned release tag (`PAPERCLIP_VERSION`) at build time, verifies the resolved
commit matches a pinned SHA (`PAPERCLIP_EXPECTED_SHA`, fails the build on tag
drift), and patches it before compiling. The patch scripts themselves —
AAF-authored, small, single-purpose — live in [`apps/paperclip/`](../../apps/paperclip)
and are `COPY`'d into the build context by name.

## Patch mechanism

Every patch is a standalone Node (`.mjs`) or Python (`.py`) script that:

1. Reads a known target file at a known build-time path (e.g.
   `/app/server/src/services/heartbeat.ts` before the `tsc` build, or a
   compiled `dist/*.js` file after it, or a resolved `node_modules` path for
   npm-published upstream packages).
2. Anchors on a short, literal snippet of upstream source that's unlikely to
   drift independently of a real refactor.
3. Applies the change idempotently (checks its own marker first) and **fails
   the build loudly** (non-zero exit, explicit log message) if the anchor
   isn't found — a silently un-patched image that ships anyway is worse than
   a build that stops and tells you upstream moved.

See [`apps/paperclip/patch-adapter.mjs`](../../apps/paperclip/patch-adapter.mjs)
for the fullest example of this pattern (provider routing, auth token
injection, prompt template, sandbox seam), and the Dockerfile comments next to
each `COPY .../patch-*.mjs` line for what each one fixes and why.

Pure transform logic is factored out into sibling modules (e.g.
`patch-adapter-provider.mjs`, `patch-adapter-sandbox.mjs`) so it can be
exercised offline in `tests/paperclip/*.test.mjs` without a Docker build —
CI's `node-tests` job runs `node --test tests/paperclip/*.test.mjs` on every
push.

## Zero-LLM metadata fastpath

`PAPERCLIP_METADATA_FASTPATH` (default unset/off) short-circuits heartbeat
wakes for pure platform-metadata questions on assigned issues — "what LLM are
you running", "is this subscription or metered billing", "which run answered
this", "how long did that run take" — with a deterministic, server-side
comment instead of a full agent spawn. Zero LLM calls, zero terminal, issue
closed, run finalized.

- Classifier + answer composer: [`apps/paperclip/metadata-fastpath.mjs`](../../apps/paperclip/metadata-fastpath.mjs)
  (pure, dependency-free, unit-tested)
- Build-time patch into `executeRun`: [`apps/paperclip/patch-metadata-fastpath.mjs`](../../apps/paperclip/patch-metadata-fastpath.mjs)
- Tests: `node --test tests/paperclip/metadata-fastpath.test.mjs`
- Design + rollout guidance: [`docs/design/paperclip-metadata-fastpath.md`](../../docs/design/paperclip-metadata-fastpath.md)

The classifier is deliberately conservative — see the design doc for why a
false negative (one extra LLM call) is a far cheaper mistake than a false
positive (a real task silently marked done with no real answer). Anything
that isn't unambiguously a metadata question falls straight through to the
normal LLM-backed path, unchanged.

Flip it on with `PAPERCLIP_METADATA_FASTPATH=1` in the container environment;
optionally set `METADATA_FASTPATH_ROUTE_NOTE` to append your own routing/
billing pointer to every fastpath answer. See
[`.env.example`](../../.env.example) for both.
