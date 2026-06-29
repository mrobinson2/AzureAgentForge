# Vendoring & Buildability — Design Spec

**Date:** 2026-06-18 (rev 2 — incorporates review feedback + repo/license findings)
**Status:** Approved (design); ready for implementation planning
**Sub-project:** #1 of 5 in the "make AAF deployment super easy for an external adopter" track
**Audience target:** External adopter — a stranger who finds AAF on GitHub with a fresh Azure subscription and zero context

> **Scope boundary (read first).** This sub-project makes the platform **buildable, not yet
> fully deployable**; the README must not claim one-command full-platform deployment until
> sub-project #2 is complete. The deliverable here ends at "a stranger can build all seven
> images from a public clone." One-command deployment, the `deploy_upstream_apps` default flip,
> and the deploy orchestration are sub-project #2.

---

## 1. Problem

The public AzureAgentForge README sells a full platform — PaperClip (orchestration + UI),
Hermes (agent runtime), Honcho (private memory) — wrapped in an Azure foundation. But a
stranger who clones the public repo **cannot build three of the seven service images**:

- `services/agent-runtime/Dockerfile` → `COPY apps/hermes/src/` and `COPY apps/hermes/overrides/skills`
- `services/honcho/Dockerfile` → `COPY apps/honcho/src/...` and `COPY apps/honcho/docker-entrypoint.sh`
- `services/paperclip/Dockerfile` → `COPY apps/paperclip/*` (10 AAF files), `COPY apps/hermes/src/...`,
  `COPY apps/hermes/overrides/skills`, and `COPY build/skills/*.json`

`apps/` has **never existed in the public repo** (not gitignored; in no branch's history), and
`build/skills/*.json` is also absent. The upstream source and AAF's own patch files live only
in the private MRTek platform repo. `scripts/build-and-push.sh` therefore marks the three as
"upstream-dependent / unbuildable" and refuses to build them without `--skip-unbuildable`. The
deploy must run with `deploy_upstream_apps = false`, which stands up the Azure foundation and
the four self-contained services but **not the actual agent platform**.

This sub-project is the enabling gate: nothing downstream delivers the full platform until a
fresh clone can build all seven images.

## 2. Goal & success criteria

**Goal.** A fresh `git clone --recursive` of the **public** repo can build all seven images
(including the three upstream-dependent ones) via `scripts/build-and-push.sh`, with **no access
to the private MRTek repo** and **no manual file copying**.

**Success criteria** (all must hold — see §11 for the testable acceptance checklist):
clean-room clone from the public URL only · submodules initialized automatically or via
`--recursive` · all seven images build · no private path required · no manual copy required ·
no private MRTek references in committed `apps/` files · gitleaks + trufflehog pass · custom
internal-reference scanner passes · License/IP checklist complete · PaperClip pin reproducibility
verified · `--self-contained` and `--skip-unbuildable` still work · missing submodules produce a
clear recovery path · README accurately states what is / isn't supported after #1.

## 3. Chosen approach — A: Submodules + committed AAF files

Approved in principle; **kept** — full vendoring (copying upstream trees into the repo) is only
adopted if a concrete technical or licensing blocker is found, and none was: all three upstreams
are public, and submodules are the cleanest posture for the AGPL Honcho dependency (§5).

- **Upstream Hermes & Honcho** → git submodules pinned to exact commit SHAs.
- **AAF-authored patch/override/wrapper files** → committed directly after sanitization (§4) and
  License/IP review (§5).
- **PaperClip** → kept as a build-time clone, **now with expected-SHA verification** to guard
  against tag drift (§6).

## 4. Workstreams (in scope)

### WS1 — Upstream submodules
Add `.gitmodules` + gitlinks pinned to exact SHAs (not floating tags) for reproducibility:

| Path | Upstream | License | Pin (SHA) | Tag/describe |
|---|---|---|---|---|
| `apps/hermes/src` | `github.com/NousResearch/hermes-agent.git` | MIT | `a91a57fa5a13d516c38b07a141a9ce8a3daabeb0` | `v2026.5.16` |
| `apps/honcho/src` | `github.com/plastic-labs/honcho.git` | **AGPL-3.0** | `72753721282b289bb63f51c03e4c69b5203d1f92` | `archive-v0.0.1-444-g7275372` |

### WS2 — AAF-authored file port + runtime-transitive completeness
Port **only the files required by the public Dockerfiles and their runtime transitive
dependencies** — not "everything in the private `apps/`." Anti-sprawl, but do not stop at COPY
validation if the service would fail at runtime. Proof obligations in §7.

Authoritative port inventory (derived from the public Dockerfiles' actual `COPY` lines):

| File | Referenced by | Notes |
|---|---|---|
| `apps/honcho/docker-entrypoint.sh` | honcho L56 | alembic + server start (AGPL mere-aggregation call, §5) |
| `apps/hermes/overrides/skills/` | agent-runtime L55; paperclip L255 | AAF skill overrides overlaid on the submodule copy |
| `apps/paperclip/patch-plugin-host.mjs` | paperclip L73 | |
| `apps/paperclip/patch-plugin-secrets-handler.mjs` | paperclip L85 | **embeds verbatim PaperClip code** (§5 attribution) + `[MRTEK PATCH]` marker (§4 sanitize) |
| `apps/paperclip/patch-plugin-worker-manager.mjs` | paperclip L92 | references Discord/Voice-Live config |
| `apps/paperclip/patch-adapter.mjs` | paperclip L174 | |
| `apps/paperclip/patch-hermes-src.py` | paperclip L201 | |
| `apps/paperclip/ddgs-wrapper.sh` | paperclip L235 | |
| `apps/paperclip/brave-search-wrapper.sh` | paperclip L242 | |
| `apps/paperclip/docker-entrypoint.sh` | paperclip L279 | |
| `apps/paperclip/auth-proxy.mjs` | paperclip L283 | |
| `apps/paperclip/skills-ui.html` | paperclip L286 | |
| `build/skills/skills-manifest.json` | paperclip L273 | **MISSING + PowerShell-generated — see WS3 decision** |
| `build/skills/agent-skill-mapping.json` | paperclip L274 | **MISSING + PowerShell-generated — see WS3 decision** |

**Explicitly NOT ported** (present in the private repo but unreferenced by any public Dockerfile —
these are the in-flight §0.3/§0.7 patches): `cost-envelope.mjs`, `patch-hermes-cost-envelope.mjs`,
`patch-adapter-router-env.mjs`, `patch-wake-kick.mjs`, `discord-plugin-selfheal.mjs`. Keeping them
out preserves the small public footprint.

### WS3 — `build/skills/*.json` resolution (blocker decision)
The PaperClip image `COPY build/skills/*.json` but those files are absent and were generated by
`Stage-Skills.ps1` (PowerShell, not cross-platform). Options, in recommended order:

- **(b) Cross-platform generator** *(preferred long-term)* — a small Python/bash script that
  builds both manifests from `apps/hermes/src/skills` + `apps/hermes/overrides/skills` at build
  time, removing the PowerShell dependency. Most aligned with "builds from the repo alone."
- **(a) Commit the generated manifests** *(fastest unblock)* — commit sanitized
  `build/skills/*.json` now; flag drift-from-source as tech debt for (b). Requires the same
  sanitization + License/IP review as `apps/` files (they enumerate skill/agent names).
- **(c) Tolerant COPY** — make the Dockerfile generate-or-empty; weakest, hides missing inputs.

**Open decision for the operator (see §13).** Recommendation: (a) to unblock #1, (b) as fast-follow.

### WS4 — Build wiring (`scripts/build-and-push.sh`)
Two changes (full diffs proposed separately, not yet applied):
1. **Expand the preflight** so each upstream service validates *all* its required inputs, not a
   single marker path (PaperClip needs `apps/hermes/src` + `build/skills/*.json` too; Honcho needs
   `apps/honcho/docker-entrypoint.sh`).
2. **Submodule auto-init UX** (§8): detect missing submodule content → warn → attempt
   `git submodule update --init --recursive` → continue on success → fail with manual instructions
   only if auto-init fails.

`--self-contained` and `--skip-unbuildable` behavior is **preserved unchanged** (regression-tested).

### WS5 — Deploy default (verify-only, no change)
Confirm `deploy_upstream_apps` stays opt-in within this sub-project. Flipping it before the
image-build step exists (sub-project #2) would make `terraform apply` reference images not yet
in ACR.

### WS6 — README accuracy guard
Minimal, surgical edit only: ensure the README states **image build is enabled; full deployment
automation follows in #2** and does not imply one-command full-platform deploy. The full
golden-path docs rewrite is sub-project #5. (Current README already hedges with "planned for
v1.2"; this guard keeps it honest as the build path lands.)

## 5. License / IP gate (hard blocker before any public commit/push)

"Buildable" is not "publishable." Before any AAF-authored wrapper/patch/`.mjs`/`.py`/`.sh`/
override or `build/skills/*.json` file is committed to a public branch, complete this checklist:

- [ ] **Hermes** — MIT (confirmed). Submodule preserves upstream LICENSE/copyright. ✅
- [ ] **PaperClip** — MIT, © 2025 Paperclip AI (confirmed at `paperclipai/paperclip`). ✅
- [ ] **Honcho** — **AGPL-3.0** (confirmed). Keep as an **unmodified** submodule; do **not** copy
      AGPL code into any MIT-licensed AAF file; treat `apps/honcho/docker-entrypoint.sh` as
      mere-aggregation (document the reasoning); record AGPL §13 network-use source-offer as an
      operator obligation for #2.
- [ ] **Copied/modified upstream code inside AAF patch files** — `patch-plugin-secrets-handler.mjs`
      embeds PaperClip source verbatim (commit `87011615^`). Add attribution + upstream license
      note in the file header; confirm no AGPL (Honcho) code is embedded in any patch.
- [ ] **Attribution** — add `NOTICE` / `THIRD-PARTY-LICENSES.md` enumerating per-component
      licenses so the repo's MIT `LICENSE` is not read as relicensing the AGPL/MIT submodules.
- [ ] **Compatibility** — AAF MIT + Hermes MIT + PaperClip MIT are mutually compatible; Honcho
      AGPL is isolated to its own image via the submodule boundary (no source-level mixing).
- [ ] **Authorization** — every file ported from the private repo is confirmed AAF-authored and
      cleared for public release (no third-party-proprietary content, no customer data).

## 6. PaperClip tag-drift mitigation — Option A (expected-SHA verification)

A git tag is not immutable. Keep the build-time clone (minimizes change) but **verify the cloned
commit matches an expected SHA** and fail the build otherwise. Pin both the tag and the expected
SHA as build args; the Dockerfile asserts `git rev-parse HEAD == PAPERCLIP_EXPECTED_SHA` right
after clone. Option B (convert to submodule) and Option C (tag-only, documented) were considered;
A is preferred because it adds reproducibility with the least structural change. (Record the
expected SHA for `v2026.517.0` during implementation by resolving the tag.)

## 7. Runtime-transitive proof (how we prove the port is complete)

Porting "what Dockerfiles reference" is necessary but not sufficient — a service can pass COPY
validation and still fail at runtime on a missing script, template, config, package assumption,
or import. Prove completeness with, in order:

1. **Dockerfile COPY-path validation** — every `COPY` source resolves in a clean checkout.
2. **Full image build** — `az acr build` (or `docker build`) completes for all three.
3. **Minimal container startup / smoke test where practical** — container boots and reaches a
   known-good signal (e.g. honcho `/openapi.json`; hermes gateway import; paperclip server
   `dist/index.js` present and process stays up briefly).
4. **Missing-input audit** — grep each Dockerfile/entrypoint for referenced scripts, templates,
   config files, and required env vars; **document the required environment variables**.

## 8. Submodule UX (auto-init, not just documentation)

Goal: remove the most common footgun rather than merely document it. `build-and-push.sh`
behavior when `apps/*/src` is empty/absent:

1. Detect missing submodule content.
2. Print a clear warning naming the missing path.
3. Attempt `git submodule update --init --recursive`.
4. Continue if it succeeds.
5. Fail with explicit manual instructions only if auto-init fails (e.g. no network, detached
   non-git tarball download).

The Forge Console doctor (sub-project #3) adds a UI-level check later; this script-level
auto-init is the immediate mitigation.

## 9. Architecture & data flow

```
git clone --recursive <public-url>
        ├─ apps/hermes/src   (NousResearch/hermes-agent @ a91a57f, MIT)
        └─ apps/honcho/src   (plastic-labs/honcho       @ 7275372, AGPL-3.0)
        │
        ▼  scripts/build-and-push.sh -r <acr>   (preflight: ALL required inputs per service)
        ├─ self-contained: model-router, memory-governor, watchdog, teams-bridge
        └─ upstream:
             • agent-runtime → apps/hermes/src + apps/hermes/overrides/skills
             • honcho        → apps/honcho/src + apps/honcho/docker-entrypoint.sh
             • paperclip     → apps/paperclip/* + apps/hermes/src + build/skills/*.json
                               + build-time clone of paperclipai/paperclip (SHA-verified)
        │
        ▼  az acr build (server-side; context must include resolved submodule contents)
        ▼  7 images in <acr>  → referenced by Terraform (deploy step, sub-project #2)
```

## 10. Risk register

| Risk | Why it matters | Mitigation |
|---|---|---|
| Secret / internal leak | Public exposure is hard to reverse | Manual review + gitleaks + trufflehog + custom internal-reference regex + **commit-history scan before push** |
| License / IP issue | Buildable does not mean publishable | License review for all three upstreams + AAF patch files; AGPL-Honcho isolated via submodule; NOTICE/THIRD-PARTY-LICENSES; attribution for verbatim-embedded code |
| PaperClip tag drift | Tag-based build can become non-reproducible | Verify expected commit SHA after clone (Option A); fail build on mismatch |
| Submodule UX | Users frequently forget recursive clone | Auto-init in build script; doctor (#3) later |
| Runtime dependency miss | COPY paths pass but service fails at runtime | Full build + minimal container smoke tests + missing-input/env audit (§7) |
| `build/skills/*.json` missing | PaperClip build hard-fails at COPY; PowerShell-only generator | Commit sanitized manifests now (a) and/or cross-platform generator (b) — WS3 |
| Azure build-context mismatch | `az acr build` uploads local context; submodule content must be present | CI validates the build context includes resolved submodule contents (checkout `submodules: recursive`) |
| Public README overpromises | Users may think #1 means deploy works end-to-end | README states "image build enabled; deployment automation follows" until #2 lands |

## 11. Acceptance criteria (testable)

- [ ] Clean-room clone using only the public repo URL (no private repo on the machine).
- [ ] Submodules initialized automatically (script auto-init) or via `--recursive`.
- [ ] All seven images build successfully.
- [ ] No private repo path is required at any step.
- [ ] No manual file copying is required.
- [ ] No private MRTek references appear in committed `apps/` (or `build/skills/`) files.
- [ ] gitleaks passes; trufflehog filesystem passes.
- [ ] Custom internal-reference scanner passes.
- [ ] License/IP review checklist (§5) complete; NOTICE/THIRD-PARTY-LICENSES present.
- [ ] PaperClip tag/commit reproducibility verified (expected-SHA assert).
- [ ] Self-contained builds still work (`--self-contained`).
- [ ] `--skip-unbuildable` behavior still works.
- [ ] Missing submodules produce a clear, actionable recovery path.
- [ ] README accurately states what is and is not supported after sub-project #1.

## 12. Recommended implementation order

Do not port private files before the submodule and build-script mechanics are proven.

1. Start from `public/main`, not stale local `main`.
2. Add `.gitmodules` and pin Hermes/Honcho to exact SHAs.
3. Add submodule presence validation + auto-init to `scripts/build-and-push.sh`; expand the preflight.
4. Build Hermes/Honcho images cleanly **before** touching any private files.
5. Inventory exact PaperClip/Hermes-override files **and runtime/env dependencies**; resolve `build/skills` (WS3).
6. Sanitize files locally (strip `MRTEK`, internal hostnames, IDs, paths, tokens).
7. Run gitleaks + trufflehog + custom scanner **locally before commit**; scan history before any push.
8. Add CI gates (trufflehog + custom scan + Dockerfile context validation + optional smoke tests).
9. Commit sanitized `apps/`/`build/skills` files **only after gates pass**.
10. Run clean-room clone + full seven-image build.
11. Only then prepare the PR.

## 13. Decisions (resolved 2026-06-18 with safe defaults — operator-overridable)

1. **`build/skills/*.json`** → **(a)** commit sanitized manifests now to unblock #1; **(b)**
   cross-platform generator as fast-follow tech debt. *Default chosen: (a).*
2. **Honcho AGPL posture** → ship Honcho strictly as an **unmodified submodule**; treat
   `apps/honcho/docker-entrypoint.sh` as mere-aggregation (document the reasoning); AGPL §13
   network source-offer is an operator obligation documented in #2. *Default: confirmed.*
3. **Pin freshness** → **freeze** at the current pins (`a91a57f` / `7275372` / PaperClip
   `v2026.517.0` → `3e6610fb938d04638fa578a1fc0d119b434fa2e4`) — conservative, matches the
   known-good private platform. Bump deliberately later if desired. *Default: freeze.*
4. **security-auditor gate** → **yes**, a `security-auditor` agent pass over the full `apps/`
   diff is a required gate before any public commit, alongside gitleaks + trufflehog + the custom
   scanner + history scan. *Default: required.*

> Any of these can be reversed before Phase 2 work begins; they are defaults chosen to keep the
> path moving, not irreversible commitments.

## 15. Implementation status (Phase 1 — as of 2026-06-18, uncommitted)

**Done + validated (local, nothing committed/pushed):**
- `.gitmodules` + Hermes (`a91a57f`, MIT) and Honcho (`7275372`, AGPL-3.0) submodules pinned.
- `scripts/build-and-push.sh`: submodule auto-init, multi-input preflight, PaperClip SHA build-arg
  — validated by `--dry-run` (self-contained builds; submodules recognized; only un-ported AAF
  files flagged) and shellcheck-clean.
- `services/paperclip/Dockerfile`: expected-SHA verification (`3e6610fb…`).
- `scripts/scan-internal-refs.sh` (+ `.internal-refs-allow`) — working; caught a real finding
  (below). `scripts/validate-build-context.sh` — working.
- CI (`ci.yml`): `secret-scan` extended (history + trufflehog + custom scan), new `build-context`
  job, `.trufflehog-exclude`.

**Surfaced finding (pre-existing, operator's call):** `installer/tests/test_core.py` contains a
real-looking AAD object ID `d4c41ac3-986d-4f52-95f4-22ca268cd058`, committed in #15 and already on
public `main`. Decide whether to scrub (touches a merged commit / history).

**Blocked on environment or operator (not "local edits"):**
- A real clean-room image build of Hermes/Honcho needs Docker/Azure **and** the two small AAF files
  those Dockerfiles COPY (`apps/hermes/overrides/skills`, `apps/honcho/docker-entrypoint.sh`) — so a
  full build crosses into Phase 2's port step. Submodule mechanics themselves are proven.
- Committing the above (gated on operator approval per the session's "do not push / show diffs first").

## 14. Dependencies & sequencing

- **Base branch:** `public/main` (local `main` is stale/divergent — do not branch from it).
- **Blocks:** sub-project #2 (one-command deploy) consumes buildable images + flips `deploy_upstream_apps`.
- **No dependency on:** #3–#5.
