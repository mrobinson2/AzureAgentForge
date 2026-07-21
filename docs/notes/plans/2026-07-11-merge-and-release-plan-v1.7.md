# v1.7.0 merge-and-release plan — 2026-07-11

Repo: `AzureAgentForge` (public). Remote is **`public`** (not `origin`). Default branch **`main`**.
All findings below were verified against live `gh`/`git`/`gitleaks` state, not assumed.

---

## TL;DR

- **main is GREEN.** Latest pushes (docs #105, foundry-chat #106, governed-UI #107, and the
  `#108` revert of the `#98` Python-3.14 bump) all pass `CI`, `security-checks`, and
  `local-stack-smoke`. Python 3.14 breakage is reverted and gone.
- **Per-PR verdict:**
  - **#110 (agents + skills)** — READY. Pure additive docs/templates. Merge FIRST.
  - **#111 (Honcho 3.0.11 + PaperClip 707)** — READY. `local-stack-smoke` passes.
    The pc707/Hermes-0.18.1 `x-api-key` landmine does **NOT** apply (see §4). Merge SECOND.
  - **#109 (tenant console + deploy UX)** — BLOCKED on a **false-positive** gitleaks finding.
    Fix is a one-block `.gitleaks.toml` allowlist (verified below). Merge LAST after the fix.
- **gitleaks finding = FALSE POSITIVE**, not a real secret. Rule `generic-api-key` matched
  `key: "outbound_sms_10dlc"` (a feature-flag string) in `demos/tenant-console/index.html:261`.
  No rotation / history purge needed. Fix = allowlist the static demo HTML.
- **#110/#111 also show `secret-scan` red, but that is STALE cross-contamination**, not a real
  finding — faithful isolated re-scans of their exact merge/head refs are clean. Explanation in §3.
- **Release notes NEED a small update** for #110 and #111 (the draft body's item 14 still says
  V1/V2 have "no open or merged PRs yet" — now false). #109's content is already in the draft.
- **Recommended merge order: #110 → #111 → #109 (after the gitleaks fix).**

---

## 1. Verified current state

### main CI
```
gh run list --branch main --limit 10
```
Latest main commits (`docs #105`, `#106`, `#107`, revert `#108`) → `CI` success,
`security-checks` success, `local-stack-smoke` success. **main is green.**
The `#98` Python-3.14 honcho base bump that broke main was reverted by `#108` (honcho stays on
`3.13-slim-bookworm`; the `astral-sh/uv 0.11.24→0.11.28` bump `#99` stands).

### Draft release
```
gh api repos/mrobinson2/AzureAgentForge/releases   # (gh release view v1.7.0 says "not found" — it is an untagged DRAFT)
```
- Draft id **352365870**, name `v1.7.0 — security hardening, governance examples, Foundry chat sample, governor features`
- `draft: true`, `tag_name: untagged-128f71e05c121af3f128`, `target_commitish: main`
- Body already enumerates items 1–14, **including** #109's V3 addendum (items 12–13) and an
  "honest ledger" item 14 that says V1 (vendored) and V2 (agents/skills) had "no open or merged
  PRs yet." That statement is now stale (see §5).

### The three PRs (all base `main`, all `MERGEABLE` / `mergeStateStatus: UNSTABLE` due to the red `secret-scan`)

| PR | Branch | Head SHA | Footprint |
|----|--------|----------|-----------|
| **#110** agents + skills | `v17/agents-and-skills` | `fb69345` | +1966 LOC, **docs only**: `agents/README.md`, `agents/templates/*` (AGENTS templates, coordinator/intake, small-model overlay), `docs/skills/*` (6 skill playbooks + README). No code, no deps, no infra. |
| **#111** vendor bumps | `v17/vendor-upgrades` | `046dc20` | `.env.example` (+8/-6), `apps/honcho/src` (submodule pointer → v3.0.11), `docker-compose.yml` (+34/-33, Honcho model-config migration), `services/honcho/README.md` (new, migration note), `services/paperclip/Dockerfile` (707 + SHA + cache-bust). **Does NOT touch `apps/hermes`.** |
| **#109** tenant + deploy UX | `v17/tenant-and-deploy-ui` | `d65c611` | +1015 LOC: `demos/tenant-console/*` (static demo + shot), `installer/preflight.py`, `installer/static/index.html`, `forge`, `AI-ASSISTED-SETUP.md`, `README.md`, `ROADMAP.md`, `docs/releases/v1.7.0.md` (V3 addendum). |

Per-PR check status (`gh pr checks`): every job passes EXCEPT `secret-scan` (red on all three).
#111 additionally runs — and passes — the `smoke` (local-stack-smoke) job, because it touches
`docker-compose.yml`/`services/**` (path-filtered; #109/#110 are docs-only so smoke is skipped).

---

## 2. The gitleaks finding — FALSE POSITIVE (exact fix)

CI job `secret-scan` (in `.github/workflows/ci.yml`) runs, with **gitleaks v8.30.1** and
`.gitleaks.toml`:
```
./gitleaks dir . --config .gitleaks.toml --redact --exit-code 1   # working tree
./gitleaks git . --config .gitleaks.toml --redact --exit-code 1   # history
```
The CI log only prints the summary (`leaks found: 1`), not the finding block. Reproduced locally
with the identical binary (`gitleaks 8.30.1`) + config + a JSON report:

```
RULE:  generic-api-key        (a gitleaks DEFAULT rule, not a custom AAF rule)
FILE:  demos/tenant-console/index.html
LINE:  261
MATCH: key: "outbound_sms_10dlc"
SECRET: outbound_sms_10dlc
```

Context (`demos/tenant-console/index.html` ~ lines 256–264) — a per-tenant **feature-flag
catalog** in inline demo JS, every value a plain snake_case flag name:
```js
flagCatalog: [
  { key: "governed_memory",      label: "Governed memory partition", ... },
  { key: "track_record_routing", label: "Track-record routing",      ... },
  ...
  { key: "outbound_sms_10dlc",   label: "Outbound SMS (10DLC)",       ... },  // <- line 261
  ...
]
```

**Verdict: FALSE POSITIVE.** `outbound_sms_10dlc` is a UI feature-flag identifier, not a
credential. The default `generic-api-key` rule fires on the `key: "…"` JS property assignment.
No real secret is present → **no rotation, no `git filter-repo`/BFG history purge required.**

The existing allowlist only exempts `*.example` and `README.md`:
```toml
[[allowlists]]
description = "example and docs files"
paths = ['''(.*)?\.example$''', '''README\.md''']
```
Static demo `.html` fixtures are not covered.

### Exact fix (verified to clear both `dir` and `git` scans)

Append to `.gitleaks.toml`:
```toml

[[allowlists]]
description = "static demo/sample UI fixtures — feature-flag & capability keys are not secrets"
paths = [
  '''demos/.*\.html$''',
  '''installer/static/.*\.html$''',
]
```

Verification performed (in a worktree of `v17/tenant-and-deploy-ui`, gitleaks 8.30.1):
```
gitleaks dir . --config .gitleaks.toml --redact --exit-code 1   -> exit 0, "no leaks found"
gitleaks git . --config .gitleaks.toml --redact --exit-code 1   -> exit 0, "no leaks found"
```

Notes:
- `installer/static/*.html` is included proactively — #109 also edits `installer/static/index.html`,
  which carries similar `key:`-shaped UI config and is the same class of static fixture.
- This is a **repo-wide security-config change**, so it is beneficial to land it on `main` early
  (see §6) — once on `main`, the same false positive can never block any future PR (including the
  transient cross-contamination in §3).
- Trade-off: a path allowlist means gitleaks stops scanning those demo `.html` files entirely. That
  is acceptable — they are hand-authored, reviewer-controlled static fixtures with no real creds. If
  you prefer to keep scanning them, the surgical alternative is a `regexes` allowlist matching the
  flag catalog values instead of a `paths` entry, but the path allowlist is simpler and durable.

---

## 3. Why #110 and #111 also show `secret-scan` red (STALE, not a real finding)

Faithful reproduction — isolated clones of the **exact** merge SHAs CI checked out
(`f5b2a5a` for #110, `56588da` for #111), and of each PR's `refs/pull/N/head`, gitleaks 8.30.1,
same config, same commands:

| Ref scanned | Result |
|---|---|
| #109 head/merge | **1 leak** — the `index.html` false positive (reproduces) |
| #110 head + merge | **0 leaks** (clean) |
| #111 head + merge | **0 leaks** (clean) |

So #110/#111 have **no real finding**. Root cause of the red check:

- `gitleaks git .` scans **every commit object present in the checkout's object DB**, not just the
  ancestry of `HEAD` (confirmed: a scan in a repo holding all three branches reported 292 commits
  and flagged `d65c611`, which is not an ancestor of #110/#111).
- All three PRs were pushed within ~6 minutes (17:45–17:51). When #110/#111's `secret-scan` ran,
  #109's `d65c611` object (carrying `demos/tenant-console/index.html`) was transiently reachable in
  the fetched object DB, so gitleaks scanned it and reported the same false positive.
- public `main` is a small, sanitized 23-commit history; `d65c611` and the demo file are **not** on
  `main` (`git merge-base --is-ancestor` = false), which is why main's own `secret-scan` stays green.

**Consequence:** once the §2 allowlist is on `main`, a re-run/rebase of #110 and #111 is green even
if the transient object reappears. Landing the allowlist on `main` first (via #110) is the robust
de-risk. If you did nothing, a plain re-run would very likely pass too — but rely on the allowlist,
not luck.

---

## 4. #111 dependency check — pc707 / Hermes-0.18.1 `x-api-key` landmine → DOES NOT APPLY

Tonight's MRTek production incident: PaperClip **2026.707.0** ships Hermes **v0.18.1**, whose
`anthropic_messages` client for `custom` + `anthropic_messages` providers sends
`x-api-key` instead of `Authorization: Bearer`, breaking any router that requires Bearer auth.

Checked against #111's diff and AAF's docs/samples:

1. **#111 does not bump Hermes.** Its file list touches `apps/honcho/src` (submodule → 3.0.11) but
   **not** `apps/hermes`. Confirmed the `apps/hermes` tree SHA is identical on `main` and the #111
   branch (`6381106c…`). The PR title is explicit: "Hermes 0.18.1 **deferred**." So AAF's agent
   runtime keeps its current Hermes pin — the pc707 auth-header change is not pulled in.
2. **No AAF-authored sample/config uses `anthropic_messages` or a `custom` LLM-router provider.**
   `grep -rn anthropic_messages` and `grep -rn api_mode` hit **only** the vendored trees
   (`apps/hermes/src/*`, `apps/honcho/src/sdks/*`) — never an AAF-owned `.env.example`,
   `docker-compose.yml`, or docs sample. There is no AAF sample that pairs pc707 with a
   Bearer-requiring `anthropic_messages` router, so there is nothing to break.
3. **The `Authorization: Bearer $PAPERCLIP_API_KEY` lines** in `agents/profiles/*.AGENTS.md` are
   auth for **PaperClip's own REST platform API** (a different surface), not the Hermes→LLM-router
   auth the bug concerns. Unaffected.
4. The PaperClip Dockerfile change is version + expected-SHA + `CACHE_BUST` only.
   `local-stack-smoke` passes on #111, exercising the Honcho 3.0.11 model-config migration and the
   707 image.

**Verdict: #111 needs NO follow-up config/doc fix before release.** The landmine is a live-MRTek
router-pairing concern, not reproduced by any AAF public sample.

**Forward-looking note (optional, not a blocker):** when the deferred **Hermes → 0.18.1** bump is
eventually done in AAF, re-verify the auth header for any `custom`/`anthropic_messages` provider,
and revisit `docs/architecture.md:51` (the Claude tier already uses a direct Anthropic SDK call to
work around LiteLLM's handling of Foundry's `/anthropic` endpoint). Consider adding a one-line
caution to `services/honcho/README.md` or the deferred-Hermes tracking issue.

---

## 5. Do the draft release notes need updating? YES (small)

- The draft body **already contains** #109's V3 content (items 12–13: tenant-console demo +
  deployment experience). Once #109 merges, `docs/releases/v1.7.0.md` on `main` matches the draft.
- The draft's **item 14 ("honest ledger")** says V1 (vendored upgrades) and V2 (agents & skills)
  "were in flight on their own branches … with no open or merged PRs yet." **This is now false** —
  #110 and #111 exist and are landing. Before publishing:
  - Add landed entries for **#110** (agent templates + curated skills library) and **#111**
    (Honcho 3.0.11 + PaperClip 2026.707.0; Hermes deferred) as new enumerated items.
  - Correct item 14's "no PRs yet" wording and its item-count prose ("Fifteen substantive items"
    → refreshed count).
  - Optionally have #110/#111 each append their own `docs/releases/v1.7.0.md` entry in-branch
    (mirroring how #109 added its addendum), so the on-`main` notes and the draft body converge.
    Neither PR currently edits release notes (they only add their own `README.md`s).

This is doc polish, **not a release blocker** — it can be done in the draft body at publish time.

---

## 6. Recommended merge order + justification

**Order: #110 → #111 → #109 (after the §2 fix).**

Justification from the diffs:
- **#110 first** — pure additive docs/templates (`agents/`, `docs/skills/`); zero code, deps, infra,
  or conflict surface. Lowest risk, and the natural carrier to land the `.gitleaks.toml` allowlist
  onto `main` early (de-risks #109's real finding AND the #110/#111 transient false positives per §3).
- **#111 second** — build/deps only, isolated to the Honcho model-config migration + PaperClip
  image pin; `local-stack-smoke` already green; landmine cleared (§4). Land it on a stable base
  before the larger #109 UI/demo change.
- **#109 last** — largest surface and the only one that edits shared narrative docs
  (`README.md`, `ROADMAP.md`, `docs/releases/v1.7.0.md`), so it should absorb any doc conflicts
  after the others land; and it is the PR that carries the flagged demo file, so it must go in
  **after** its gitleaks blocker is fixed.

There are no code/build dependencies **between** the three PRs — the ordering is chosen for
risk isolation and to get the security-config fix onto `main` first, not because one needs another.

---

## 7. Ordered execution steps (exact commands)

> Preconditions: `main` green (verified). You have push rights to remote `public`.
> Note: the three branches are already checked out in sibling worktrees
> (`aaf-wt-v1/v2/v3`) — either operate there or from fresh checkouts.

### Step 0 — land the gitleaks allowlist on the first PR (#110)
Add the §2 block to `.gitleaks.toml` on `v17/agents-and-skills` so it reaches `main` with the
first merge and neutralizes the false positive repo-wide.
```bash
cd /path/to/aaf-wt-v2                 # worktree for v17/agents-and-skills  (or: git checkout v17/agents-and-skills)
# append the §2 [[allowlists]] block to .gitleaks.toml, then:
gitleaks dir . --config .gitleaks.toml --redact --exit-code 1     # expect: no leaks found
git add .gitleaks.toml
git commit -m "ci(gitleaks): allowlist static demo/sample UI fixtures (feature-flag keys are not secrets)"
git push public HEAD:v17/agents-and-skills
gh pr checks 110 --watch                                          # expect all green incl. secret-scan
```
(Optionally also append #110's own `docs/releases/v1.7.0.md` entry here — see §5.)

### Step 1 — merge #110
```bash
gh pr merge 110 --squash --delete-branch    # or --merge, per repo convention
gh run list --branch main --limit 5         # confirm CI green on main
```

### Step 2 — rebase + merge #111
```bash
cd /path/to/aaf-wt-v1                        # worktree for v17/vendor-upgrades
git fetch public
git rebase public/main                       # picks up the allowlist; resolve trivial conflicts if any
git push --force-with-lease public HEAD:v17/vendor-upgrades
gh pr checks 111 --watch                      # expect all green (smoke + secret-scan)
# optional: append #111's docs/releases/v1.7.0.md entry, commit, push
gh pr merge 111 --squash --delete-branch
gh run list --branch main --limit 5
```

### Step 3 — rebase + merge #109 (its blocker is now fixed via the on-main allowlist)
```bash
cd /path/to/aaf-wt-v3                        # worktree for v17/tenant-and-deploy-ui
git fetch public
git rebase public/main                       # gets the allowlist; index.html now allowlisted
git push --force-with-lease public HEAD:v17/tenant-and-deploy-ui
gh pr checks 109 --watch                      # expect all green, incl. secret-scan
# (belt-and-suspenders: if you prefer #109 self-contained, also add the §2 allowlist
#  block directly on this branch before rebase — harmless duplicate once main has it)
gh pr merge 109 --squash --delete-branch
gh run list --branch main --limit 5           # confirm main green after all three
```

### Step 4 — update the draft release body (§5), then publish/tag
```bash
# Review the draft body against merged reality:
gh api repos/mrobinson2/AzureAgentForge/releases/352365870 -q .body | less
# Edit item 14 (remove "no PRs yet"), add landed entries for #110 and #111, refresh the item count.
# Then publish the draft — this creates the tag v1.7.0 pointing at current main:
gh release edit untagged-128f71e05c121af3f128 \
  --tag v1.7.0 --target main --draft=false --latest \
  --notes-file <edited-notes.md>            # or edit body via the web UI / --notes
gh release view v1.7.0                        # verify published + tag created
```

---

## 8. Release checklist (what CI/smoke exists)

Workflows in `.github/workflows/`:
- **`ci.yml`** (`push: main`, `pull_request`) — jobs: `secret-scan` (gitleaks 8.30.1 dir+git,
  trufflehog verified-only, `scripts/scan-internal-refs.sh`), `build-context`, `compose`,
  `node-tests`, `python`, `scripts`, `terraform`.
- **`security-checks.yml`** — `no-hermes-node-surface` (agent-runtime must stay Python-only),
  `python-security-override-present` (constraints pins wired).
- **`local-stack-smoke.yml`** — `smoke`: brings the local compose stack up (path-filtered to
  compose/services changes; ran & passed on #111).
- **`deploy.yml`** — Azure deploy pipeline (gated/manual; **not** part of tagging the release).

Pre-tag gate (all must be green on `main` after Step 3):
- [ ] `CI` (all 7 jobs) green on `main`
- [ ] `security-checks` green
- [ ] `local-stack-smoke` green
- [ ] `secret-scan` green (allowlist landed)
- [ ] Draft body reconciled with merged PRs (#110, #111 entries added; item 14 corrected)
- [ ] Publish draft with `--tag v1.7.0 --target main --latest` → tag created from `main` tip

There is **no dedicated release/tag automation** — publishing the draft (`draft=false` + a real
tag) is the release action; it tags whatever `main` points at, so merge all three PRs first.

---

## Appendix — evidence log (commands run for this plan)

- `gh run list --branch main` → main green; `#108` reverted `#98`.
- `gh pr checks 109/110/111` → only `secret-scan` red on each; #111 `smoke` green.
- `gh pr view N --json mergeable,mergeStateStatus,files` → all MERGEABLE/UNSTABLE; footprints in §1.
- gitleaks 8.30.1 JSON reproduction (isolated `refs/pull/N/{head,merge}` clones): #109 = 1 false
  positive (`generic-api-key`, `demos/tenant-console/index.html:261`, `key: "outbound_sms_10dlc"`);
  #110 = 0; #111 = 0.
- Allowlist fix re-scan on #109 branch → `dir` and `git` both `no leaks found`, exit 0.
- `git diff public/main..public/v17/vendor-upgrades` → no `apps/hermes` change; `apps/hermes` tree
  SHA identical on main and #111 (`6381106c…`).
- `grep -rn anthropic_messages|api_mode` → matches only in vendored `apps/hermes/src` / `apps/honcho`
  SDK, never in AAF-authored samples/configs.
- `gh api …/releases` → draft id 352365870, target main, body already includes #109 V3 (items 12–13),
  item 14 stale re: #110/#111.
