<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Behavioural Replay Gate — CI for prompts

> **Technical reference for contributors.** For the operational overview, start at [README](../../README.md) or [the agent role model](../agents.md).

![status](https://img.shields.io/badge/status-shipped%20%E2%80%94%20CI%20enforcing-brightgreen)

> **One sentence.** A CI job (`Prompt Replay Gate`) that composes an agent
> persona edit's candidate prompt, runs a set of behavioural contract
> fixtures against it and against the pull request's base ref, and fails the
> build if a fixture that used to pass now fails — the same shape as a code
> regression test, applied to the text that actually governs an agent's
> behaviour.

**Audience.** Anyone extending AzureAgentForge's agent roster, or building
governed multi-agent systems generally, who wants their persona prompts to
get the same regression protection their code already gets.

---

## 1. The failure class: prompt edits are code changes nobody tests

Every role in this platform runs on a system prompt —
[`agents/profiles/<role>.AGENTS.md`](../../agents/profiles/) — that carries
the actual governance logic: which lane the role stays in, how it escalates,
what it's forbidden to touch, and how it records that a task finished
honestly instead of trailing off into silence. [`agents/README.md`](../../agents/README.md)
describes what each of those files contains: a scope guard, a
no-cancel-without-comment gate, allowed/forbidden tool tables, escalation
triggers, and a disposition protocol. That is not decoration — it is the
control surface. A role's actual behaviour is determined far more by this
prose than by the YAML sidecar that names its model tier.

And yet a prompt edit is nearly invisible to CI. `agents/validate_profiles.py`
and the schema tests in `agents/tests/test_profiles.py` check the *YAML*
sidecars for structural validity, plus one hard-won lint: every issue-scoped
`.AGENTS.md` file must contain the literal disposition-protocol heading and
its `no silent terminal states` / `plan_only` / `missing_disposition`
language (a lint the project's own history explains — see the comment above
`DISPOSITION_HEADING` in that test file). That lint is real and it is
useful. It also only checks the disposition section, only checks it's
*present in the source file*, and has no equivalent for the other guardrail
blocks — the scope guard, the no-cancel gate, a role's specific escalation
carve-outs. A pull request can delete Security's direct-to-operator
escalation path, or quietly drop CostGuardian's read-only qualifier on its
Azure tool grant, and every existing check stays green: the YAML still
validates, the disposition heading is still there, gitleaks and
`scan-internal-refs.sh` have nothing secret-shaped to flag. The diff *looks*
like a wording change. It behaves like a governance rollback.

This is the same failure class every other kind of "config that behaves like
code" eventually gets a guard for in this repo — see
[`docs/design/vendored-config-schema-guard.md`](vendored-config-schema-guard.md)
for the config-drift version of the same argument. The fix here is the same
shape: stop trusting that a file *parsing* means it still *means* the same
thing, and check the specific properties that matter, on every change, at
the byte layer that actually ships.

## 2. What "composing" a prompt means in THIS repo (read before you assume otherwise)

If you've seen a "replay gate" or "compose the prompt" pattern elsewhere,
the word "compose" usually implies real assembly: a shared preamble
concatenated with a role body, `{{PLACEHOLDER}}` tokens substituted from a
config record, maybe an `<!-- include: ... -->` directive pulling in a
shared contract fragment. **AzureAgentForge does not do any of that today.**

Per `agents/README.md`: "the `.AGENTS.md` file is the actual prompt
prepended to every task the role runs." There is no deploy script in this
repo that concatenates, substitutes, or transforms these files before a role
uses them — they ship as the file, minus its YAML frontmatter. Grepping the
tree confirms it: no `{{...}}` placeholder tokens and no
`<!-- include: ... -->` directives exist anywhere in `agents/profiles/`. The
angle-bracket tokens you *will* see scattered through the shipped
files — `<cause>`, `<repo>`, `<your-id>` — are not template placeholders
waiting to be filled by a compose step; they're intentional prose, example
values the *model* fills in when it writes its own comment (see, for
example, the "platform issue: `<cause>`" quoted template in every profile's
platform-failure-refusal section). They are meant to ship exactly as
written.

So `scripts/replay-gate/compose_prompt.py` is honestly described as a
**validating canonicalizer**, not a composer in the multi-source-assembly
sense. For a given role it:

1. Locates `agents/profiles/<role>.AGENTS.md`.
2. Validates the YAML frontmatter: present, the first thing in the file,
   valid YAML, limited to the five house-convention keys (`role`,
   `voice_id`, `color`, `emoji`, `vibe` — see
   [`agents/templates/AGENTS.template.md`](../../agents/templates/AGENTS.template.md)'s
   own guidance comment), and a `role` value that is a well-formed slug
   matching the filename.
3. Strips the frontmatter. What's left **is** the composed, deployable
   prompt — there is nothing further to resolve.
4. Fails loudly, with a specific message, on every defect above, and on any
   leftover `{{PLACEHOLDER}}` token in the body — a forward-looking check:
   no current profile has one, but it's there so that the day a real
   include or templating mechanism lands, a partial failure in it gets
   caught here instead of shipping a literal `{{...}}` string as prompt
   text.

If AAF ever grows a real compose step — a shared preamble all roles inherit,
a templating pass driven by the YAML sidecar — extend `compose()`, not a
second implementation. This module is deliberately the *one* place prompt
bytes get produced, so a future deploy tool and this gate cannot silently
diverge on what "the composed prompt" means. That divergence is exactly what
caused the incident this pattern is adapted from in a private deployment of
a similar platform: a deploy script's `\A`-anchored frontmatter strip
silently no-op'd on a misplaced fence and shipped raw YAML as prompt text.
AAF has no such deploy script today — check 2 in the list above
(`misplaced frontmatter`) exists to guard the day one is added, not because
it has ever fired here.

### A real defect this canonicalizer found

Building this gate meant running `compose_prompt.py compose-many` against
the full roster for the first time. It failed loudly:

```
FAILED  generalist: no prompt file for role 'generalist' at agents/profiles/generalist.AGENTS.md
        — this role has a YAML contract but no system prompt (see agents/README.md:
        every role ships both layers)
```

`agents/profiles/generalist.yaml` — a fully valid, schema-passing profile —
had no matching `generalist.AGENTS.md`. Per the platform's own design, a
role with a YAML contract and no system prompt has a model tier, a set of
toolset grants, and *zero governance*: no scope guard, no no-cancel gate, no
disposition protocol. `agents/tests/test_profiles.py`'s roster-completeness
assumptions (`expected 14 profiles`, `expected 16 prompt files`) were
internally consistent with this gap — they count 14 YAML profiles and only
13 + 3 template prompt files, so nothing in the existing suite ever asserted
the two counts should match. This is exactly the class of "technically valid,
actually broken" state a byte-level composer surfaces and a schema-only
validator can't: `generalist.AGENTS.md` now exists (added in its own commit,
following the shipped template pattern used by every other economy-tier,
`terminal`+`file`, Orchestrator-reporting role), and `compose-many` on the
full roster is part of this gate's own CI run and test suite, so this
specific gap cannot silently reappear.

## 3. The pipeline: compose → contract check → diff

```
   agents/profiles/*.AGENTS.md  (candidate: this ref)
                │
                ▼
   compose_prompt.py compose-many   ──►  candidate composed bytes + manifest
                │                         (byte count, sha256, chars/4 + tiktoken
                │                          token ESTIMATES — see §6)
                ▼
   prompt_contract_check.py  ──runs fixtures against──►  candidate results
                │
   (same fixtures, same compose_prompt.py, against a base-ref checkout)
                ▼
   prompt_contract_check.py  ──runs fixtures against──►  base results
                │
                ▼
        pass/fail delta:
          base PASS → candidate FAIL   =  REGRESSION  (blocks the build)
          base FAIL → candidate FAIL   =  FAILING     (blocks the build)
          base FAIL → candidate PASS   =  IMPROVEMENT (noted, never blocks)
          otherwise                    =  ok
```

Two scripts, one responsibility split:

- **`scripts/replay-gate/compose_prompt.py`** owns *composition integrity* —
  can these bytes even be produced correctly? Frontmatter well-formed,
  roster complete, no leftover placeholder. This is the layer where a defect
  means the deployed prompt is flatly wrong (missing, truncated, or
  contaminated with raw YAML).
- **`scripts/replay-gate/prompt_contract_check.py`** owns *behavioural
  content* — given correctly-composed bytes, do they still say the things
  they're supposed to say? Required sections present, forbidden content
  absent, token budget respected. This is the layer where a defect means the
  prompt is well-formed but has quietly stopped meaning what it used to mean.

Composition failures and contract failures are deliberately not conflated:
a composition failure (`CompositionError`) always means "these bytes cannot
be produced" and is fatal immediately; a contract failure means "these bytes
were produced, but a specific fixture doesn't like them," which is only a
*regression* — and thus a blocking gate failure — if it used to pass on the
base ref.

## 4. Fixture format reference

A fixture is a YAML file in `scripts/replay-gate/fixtures/` with a
`prompt_contract` block:

```yaml
fixture_id: "05-cost-guardian-readonly-contract"   # unique, used in reports/JUnit
description: >
  Free text. Explain WHY the contract matters, not just what it checks —
  the fixture file is the first thing a reviewer reads when this gate fails.
prompt_contract:
  agents: [cost-guardian]        # a list of role slugs, or the string "all"
                                  # (resolved from agents/profiles/*.yaml at
                                  # check time, so it can't drift from the roster)
  must_contain:                  # list of regexes (re.search); every agent's
    - "READ-ONLY"                # composed prompt must match every pattern
  must_not_contain:               # list of regexes; NO agent's composed prompt
    - '\bgpt-[0-9][a-zA-Z0-9.\-]*'  # may match any pattern
  max_chars_per_4_estimate: 9000  # optional: a chars/4 ESTIMATE ceiling
                                   # (see §6 for why this is a heuristic, not
                                   # a real token count)
```

All four `prompt_contract` fields are optional except `agents` — a fixture
can check `must_contain` alone, `must_not_contain` alone, a token ceiling
alone, or any combination.

### The eight fixtures shipped with this gate

| # | Fixture | Checks |
|---|---|---|
| 01 | `roster-disposition-protocol` | Every role carries the disposition-protocol section, at the composed-bytes layer (the counterpart to `agents/tests/test_profiles.py`'s source-file lint). |
| 02 | `roster-no-cancel-without-comment` | Every *active* role requires a comment before any `cancelled` PATCH, in the required order. `curator` is exempt — see the fixture file for why. |
| 03 | `roster-scope-guard-intact` | Every *non-root* role opens with an intact scope-guard block. `orchestrator` is exempt — it's the root of the routing tree. |
| 04 | `security-critical-escalation` | Security's direct-to-operator carve-out for critical findings (CVSS ≥ 9.0, exposed secret, RCE) survives edits. |
| 05 | `cost-guardian-readonly` | CostGuardian's Azure tool grants stay explicitly read-only; its forbidden-mutations guardrail stays intact. |
| 06 | `no-hardcoded-model-names` | No role hardcodes a concrete model name — `agents/README.md` is explicit that model selection is an abstract tier resolved by the model-router, never a prompt literal. |
| 07 | `no-internal-urls` | No role references a URL outside `localhost` and the RFC 2606 example domains — these prompts are meant to be generic and platform-agnostic. |
| 08 | `token-budget-ceiling` | No role's composed prompt exceeds a chars/4 ESTIMATE ceiling (headroom above the current largest role, Orchestrator). |

Fixtures 02 and 03 are worth reading in full: they're not blanket
"every role must have everything" checks. `curator` is a documented
"design target — not yet deployed" role and doesn't carry the no-cancel
gate; `orchestrator` is the routing tree's root and has no scope guard to
bounce work through, because there's nothing above it to bounce to. Both
exemptions are explicit, named, and justified in the fixture file — the same
pattern `agents/tests/test_profiles.py` already uses for its
`DISPOSITION_EXEMPT` set. A gate that can't tell a legitimate structural
exception from a regression is a gate nobody trusts; encoding the exception
explicitly, with a reason, is what keeps the gate meaningful instead of
becoming a false-positive machine contributors learn to ignore.

## 5. How to add a fixture

1. Pick the smallest true statement you want to defend — "Security's
   critical findings still escalate directly to the operator," not "Security
   is good." Specific, falsifiable claims make good fixtures; vague ones
   make noisy ones.
2. Compose the role(s) in question locally and read the actual text you want
   to assert on:
   ```bash
   python3 scripts/replay-gate/compose_prompt.py compose security
   ```
3. Write the fixture YAML. Use `must_contain` for "this text must survive";
   use `must_not_contain` for "this text must never appear." Prefer a few
   short, precise regexes over one long one — each failure line names which
   pattern broke, which is what a reviewer actually needs.
4. Run it against the real roster:
   ```bash
   python3 scripts/replay-gate/prompt_contract_check.py --profiles-dir agents/profiles
   ```
5. Prove it actually catches the regression it's meant to catch — temporarily
   break the text it protects in a scratch copy and confirm the fixture
   fails, the way `--self-test` does for the gate itself (§7). A fixture
   nobody has ever seen fail is a fixture nobody has verified works.
6. Add tests in `scripts/replay-gate/tests/` if the fixture's regex is
   subtle enough that a future edit could break it silently (see
   `test_cli_diff_mode_detects_seeded_regression` for the pattern).

## 6. Token estimates — read the labels

`compose_prompt.py` reports two token counts per role, and both are labeled
**ESTIMATE** on purpose:

- **`chars_per_4_estimate`** — `len(text) / 4`. Always available, no
  dependency required. This is what fixture 08's budget ceiling checks
  against, precisely because it never depends on an optional package being
  installed.
- **`cl100k_base_estimate`** — via `tiktoken`, if installed (`pip install
  tiktoken`; it is not a hard dependency of this gate). This is the
  GPT-3.5/4-family tokenizer, used only because it's the most widely
  available reference tokenizer — **not** because any AAF role is
  necessarily served by a cl100k-tokenized model. Per `agents/README.md`,
  `model_tier` is an abstract label; the concrete deployment behind it is
  configured separately via `PERSONA_TIERS_JSON` in the model-router and can
  change without a prompt edit. Neither number is a measurement of real
  provider token usage — treat them as "is this prompt roughly this big,"
  not "this prompt costs exactly this many tokens."

## 7. Running it

```bash
# Install once
pip install pyyaml pytest tiktoken   # tiktoken optional

# Compose the whole roster, print a byte/token table
python3 scripts/replay-gate/compose_prompt.py compose-many --out-dir /tmp/composed
python3 scripts/replay-gate/compose_prompt.py report --manifest /tmp/composed/manifest.json

# Run every fixture against the current tree
python3 scripts/replay-gate/prompt_contract_check.py --profiles-dir agents/profiles

# Prove the gate itself works before trusting a pass — seeds a synthetic
# regression, confirms the gate reports it, cleans up. Mirrors the house
# convention in scripts/validate_vendored_config.py --self-test.
python3 scripts/replay-gate/prompt_contract_check.py --self-test

# Full test suite
pytest -q scripts/replay-gate/tests
```

In CI, [`.github/workflows/prompt-replay-gate.yml`](../../.github/workflows/prompt-replay-gate.yml)
triggers on any pull request touching `agents/profiles/**` or
`scripts/replay-gate/**`, exports the PR's base-ref `agents/profiles/` tree
to a temp directory, composes both trees, and runs the diff check. A
REGRESSION or a still-FAILING fixture exits non-zero; the byte/token report
and the pass/fail delta both publish to the job summary so a reviewer sees
the specific claim that broke, not just a red X.

## 8. Future: replay against recorded traffic

Everything above checks the composed prompt's *text* — required sections,
forbidden content, size. It never runs the prompt against a model and
observes what the agent actually *does*. That's a real, and real-er, layer
of behavioural testing, and it's deliberately out of scope for this gate:
running it needs either a live deployed candidate (this repo's posture is
explicitly "no live model calls in an automated gate") or a recorded corpus
of realistic tool-calling traffic to replay offline.

The natural source for that corpus already exists in this repo:
[`services/model-router/flight_recorder.py`](../../services/model-router/flight_recorder.py).
It's a bounded, redacted-by-default ring buffer of exactly the call shape a
live-replay test would need — caller, requested messages (or a
`prompt_fingerprint` hash when redaction is on), response, latency, and
whether a waste-breaker would have flagged it. It exists today for spend
debugging, not replay, but the event shape is most of what a replay harness
needs for free:

- **A recorded corpus, not a synthetic one.** Today's fixtures assert on
  static prompt text. A flight-recorder-backed replay would instead take
  real historical `(role, input, tool calls, disposition)` traces — the
  actual shapes of work each role has handled — and re-run them against a
  candidate prompt, checking that the *pattern* of tool calls and
  dispositions doesn't regress (does the role still stay in its lane on the
  same class of task it saw before; does it still record a disposition
  instead of trailing off).
- **What's missing.** The recorder is bounded and redacted by default — full
  prompt/response text is dropped in favor of a fingerprint unless an
  operator explicitly turns redaction off for local debugging. A corpus
  usable for replay would need either redaction off in a controlled capture
  window, or a replay design that works from the fingerprint plus tool-call
  shape alone (structural replay: "did the same category of tool calls
  happen," not "was the text identical"). It would also need a model API
  key to actually execute the replay — the same blocker this gate's own
  scoping note calls out for a live-model layer generally.
- **Why this is a design note and not a build.** Wiring a live replay
  harness is a materially different project — it needs the capture-window
  decision above, a model API key available in CI (this repo's CI has none
  today), and a policy for what "still the same behaviour" means for a tool
  call pattern rather than a text pattern. That's worth scoping deliberately
  rather than bolting on. This section exists so the next contributor
  reaching for "let's replay real traffic" starts from "the flight recorder
  already has the event shape" instead of rebuilding a capture pipeline from
  scratch.

## 9. What this gate is not

- **Not a live-model test.** It never calls a model. See §8.
- **Not a composer in the multi-source-assembly sense.** See §2 — there is
  no include/placeholder mechanism in this repo today; the module is a
  validating canonicalizer, and says so in its own docstring.
- **Not a substitute for `agents/tests/test_profiles.py`.** That suite
  checks YAML schema validity and the disposition-protocol lint on the
  source file; this gate checks composed-bytes behavioural contracts. Keep
  both — they check different things at different layers, and the composed-
  bytes layer is exactly what would catch the two of them silently
  diverging if a real compose step is ever added.
