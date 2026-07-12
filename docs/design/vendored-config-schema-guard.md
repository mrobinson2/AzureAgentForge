<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Vendored-config schema guard (A3)

> **Technical reference for contributors.** For the operational overview, start at [README](../../README.md) or [Architecture](../architecture.md).

![status](https://img.shields.io/badge/status-shipped%20%E2%80%94%20CI%20enforcing-brightgreen)

> **One sentence.** A CI job (`validate-vendored-config`) that validates every
> config key AAF ships to a vendored app against the schema the *pinned vendored
> source* actually reads, so a key the app silently drops fails the build instead
> of silently degrading the deployment.

**Audience.** An engineer maintaining a platform that vendors upstream apps
(pinned submodules or pinned release builds) and ships its own config artifacts
for them — compose env blocks, Terraform env, generated config files. The
pattern generalizes; the app-specific adapters here are worked examples.

---

## 1. The failure class

A vendored app's config schema is owned by the upstream; the config artifacts
that feed it are owned by this repo. Nothing ties the two together. When the
upstream changes its schema on a vendor bump, the shipped artifacts keep
"working": the app boots, health checks pass, and the keys it no longer reads
are **silently ignored** — the app runs on built-in defaults you did not choose.

Two real incidents (observed on a private deployment of the same upstreams)
define the class:

1. **Honcho 3.0.7** replaced the flat per-specialist keys
   (`SUMMARY_PROVIDER`, `DERIVER_MODEL`, `DIALECTIC_LEVELS__<lvl>__PROVIDER`, …)
   with a nested `MODEL_CONFIG` sub-model. Every Honcho settings class uses
   pydantic-settings `extra="ignore"`, so an un-migrated config boots clean and
   **falls back to direct-OpenAI `gpt-5.4-mini`** for every specialist. No
   error. You find out from the OpenAI bill.
   (Documented in [`services/honcho/README.md`](../../services/honcho/README.md).)
2. **Hermes 0.18.1** shifted its config schema and silently misrouted provider
   credentials (symptom: `provider=openrouter, model=empty, 401`). The config
   file parsed fine; the keys just no longer meant what the artifact thought.

Both failures share one shape: **config the app no longer reads**. That is
exactly what a schema guard can catch mechanically, before deploy.

**And the guard found live instances on its first run** — see §7. The vendor
bump that migrated `docker-compose.yml` and `.env.example` to the Honcho 3.0.7+
schema missed `infrastructure/modules/container-apps/honcho.tf` (all three
resources) and `deploy/mac-site/docker-compose.yml`: both still shipped the
removed flat keys, meaning a Terraform or Mac-site deployment of the pinned
Honcho 3.0.11 image would run every specialist on direct-OpenAI
`gpt-5.4-mini` — incident (1), reproduced in-tree.

## 2. Inventory: vendored apps × shipped config artifacts

| Vendored app | Vendored how | Config surface | AAF-shipped artifacts that feed it |
|---|---|---|---|
| **Honcho** (`apps/honcho/src`) | git submodule, pinned `v3.0.11` | pydantic-settings classes in `src/config.py` (per-section `env_prefix`, `__` nested delimiter, `extra="ignore"`) | `docker-compose.yml` → `honcho.environment`; `deploy/mac-site/docker-compose.yml` → `honcho.environment`; `infrastructure/modules/container-apps/honcho.tf` → `env{}` blocks (3 resources: API app, deriver app, deriver job) |
| **Hermes** (`apps/hermes/src`) | git submodule, pinned `v2026.5.16` | (a) `config.yaml` parsed by `hermes_cli/config.py` (`DEFAULT_CONFIG` tree + `_KNOWN_ROOT_KEYS`); (b) env vars read via `os.getenv`/`os.environ` across the tree | (a) the `config.yaml` heredoc generators — **discovered**, not hardcoded, across `apps/paperclip/*.sh` and `services/paperclip/*.sh` (currently `apps/paperclip/write-hermes-config.sh` (A1's canonical template) plus the copy inside `services/paperclip/docker-entrypoint.sh`); (b) `infrastructure/modules/container-apps/hermes.tf` → `env{}` blocks, scoped to the `hermes` container (the model-router sidecar is AAF-authored) |
| **PaperClip** (not in-tree) | cloned at image build from the pinned release tag (`services/paperclip/Dockerfile`: `ARG PAPERCLIP_VERSION` + `PAPERCLIP_EXPECTED_SHA`) | env vars (upstream server + hermes-paperclip-adapter + AAF entrypoint/auth-proxy) | `docker-compose.yml` → `paperclip.environment`; `deploy/mac-site/docker-compose.yml` → `paperclip.environment`; `infrastructure/modules/container-apps/paperclip.tf` → `env{}` blocks |

Out of scope (documented, deliberate):

- **`.env.example` keys** are compose-interpolation *inputs* (`SUMMARY_TRANSPORT`
  feeds `SUMMARY_MODEL_CONFIG__TRANSPORT`), not app-visible env. The injection
  point — the compose/Terraform env key — is what the apps see, so that is what
  is validated.
- **`env_file: [.env]`** on the Mac-site services sprays the whole site `.env`
  into each container. Validating that would flood on shared keys
  (`POSTGRES_*`, router tiers). Only explicit `environment:` keys — the
  intentional per-service interface — are validated. (Known consequence worth
  knowing: a site `.env` `EMBEDDING_MODEL` also lands in Honcho, which has an
  `EMBEDDING_` settings section. The guard can't referee intent inside a shared
  env file.)
- **Model-router, memory-governor, watchdog, bridges** are AAF-authored — their
  config surface lives in this repo and drifts with its own code review; the
  guard is for surfaces whose schema is owned *upstream*.

## 3. Per-app validation strategy

One principle drives all three adapters: **derive the accepted-key universe
from the pinned vendored source wherever the source is in-tree and parseable;
curate a manifest only where it is not — and pin every curated artifact to the
vendor version so a bump forces re-validation.**

### 3.1 Honcho — load the vendored pydantic Settings (dynamic, exact)

`src/config.py` imports only `pydantic`, `pydantic-settings`, `python-dotenv`
and stdlib, and has no package-relative imports, so the guard loads it
standalone (`importlib.util.spec_from_file_location`, with
`PYTHON_DOTENV_DISABLED=1`) and introspects the real `AppSettings` model — the
same code the container executes. From it the guard derives every acceptable
env key:

- each settings class contributes `env_prefix + FIELD_NAME` for scalar fields;
- classes with `env_nested_delimiter="__"` contribute nested paths through
  `BaseModel`-typed fields (`SUMMARY_MODEL_CONFIG__TRANSPORT`,
  `…__OVERRIDES__BASE_URL`), honoring `validation_alias`;
- `dict[Literal[...], Model]` fields contribute one branch per literal key —
  `DIALECTIC_LEVELS__minimal__MODEL_CONFIG__MODEL` is accepted,
  `DIALECTIC_LEVELS__hgih__…` (typo'd level) is not;
- matching is case-insensitive, mirroring pydantic-settings' default.

Because the universe is *derived at validation time from the submodule commit
the repo pins*, a Honcho vendor bump needs no manifest maintenance: the moment
an upstream schema change invalidates a shipped key, the job fails — which is
the point.

A curated **removed-keys map** (from the 3.0.7 migration table in
`services/honcho/README.md`) upgrades the error message for the known-removed
flat keys from "unknown key" to "**removed in 3.0.7 — replace with
`SUMMARY_MODEL_CONFIG__TRANSPORT`**". The map only improves diagnostics; the
detection itself never depends on curation.

### 3.2 Hermes `config.yaml` — AST-parse the vendored parser (static) + pinned manifest for polymorphic sections

Importing `hermes_cli.config` would drag in the Hermes dependency tree, so the
guard **AST-parses** `apps/hermes/src/hermes_cli/config.py` (no imports, no
execution) and extracts:

- the `DEFAULT_CONFIG` key tree (root keys + nested dict keys), and
- `_KNOWN_ROOT_KEYS` (the upstream's own root-key set — incomplete upstream,
  so the union of both is used).

Every key path in the AAF-generated `config.yaml` (extracted from the
`<<HERMES_EOF` heredocs, `${…}` placeholders neutralized, then YAML-parsed)
must exist in that tree. The generator scripts themselves are **discovered**
by scanning `apps/paperclip/*.sh` + `services/paperclip/*.sh` for the heredoc
marker — A1 moved the generation from the entrypoint into
`write-hermes-config.sh` mid-flight, which is exactly why hardcoded paths
would rot; zero generators found is itself a finding (a Hermes booting on
pure defaults is its own silent-config incident). Two honest gaps require a
curated manifest ([`scripts/vendored-config/manifest-hermes.yaml`](../../scripts/vendored-config/manifest-hermes.yaml)):

- **Polymorphic sections.** `DEFAULT_CONFIG["model"]` is `""` (string), but the
  runtime also accepts a dict form whose keys (`provider`, `base_url`,
  `api_mode`, …) are only visible as scattered `.get()` calls. The manifest
  records those accepted subkeys with the source references used to curate them.
- **Manifest staleness.** The manifest carries the **pinned submodule commit**;
  the guard compares it against `git ls-tree HEAD apps/hermes/src` (the
  superproject gitlink — works even before submodule checkout). On a Hermes
  vendor bump the SHAs diverge and the job **fails loudly** until someone
  re-validates the polymorphic sections against the new source and updates the
  pin. That friction is deliberate: it converts "silent schema drift" into "a
  human re-checked the schema".

### 3.3 Hermes env vars — static consumption scan

The accepted env universe for `hermes.tf` is the union of: every string literal
passed to `os.getenv` / `os.environ.get` / `os.environ[...]` across the
vendored Hermes Python tree (regex scan, ~1,700 files, sub-second); the keys of
the parser's `REQUIRED_ENV_VARS` / `OPTIONAL_ENV_VARS` / `_EXTRA_ENV_KEYS`
tables (AST-extracted); and `$VAR` / `${VAR}` reads in the AAF-authored
override scripts baked into the same image (`apps/hermes/overrides/**/*.sh`).
This over-accepts (any env the source *ever* reads is accepted) — fine, because
this check's job is to catch keys the app **never** reads, which is the silent
class.

### 3.4 PaperClip — curated per-version manifest (source not in-tree)

PaperClip source is cloned at image build time, so there is nothing in-tree to
parse. The honest strategy is a curated manifest
([`scripts/vendored-config/manifest-paperclip.yaml`](../../scripts/vendored-config/manifest-paperclip.yaml)):
every accepted env key, annotated with its consumer (upstream server, adapter,
AAF entrypoint, auth-proxy, or Hermes-inside-the-container), pinned to
`PAPERCLIP_VERSION`. The guard cross-checks the manifest's pinned version
against **both** the Dockerfile `ARG PAPERCLIP_VERSION` default and the compose
`${PAPERCLIP_VERSION:-…}` default; any mismatch fails the job with
"vendor bump — re-validate the manifest". Same loud-on-bump behavior as 3.2,
enforced on the pin the build actually uses.

## 4. Failure policy: hard-fail everything, allowlist with mandatory reasons

Every finding is a **hard failure** — no warn tier. A warn tier for "probably
fine" keys is how the last two incidents shipped: a warning in a green build is
invisible. The pressure valve is an allowlist, not a severity downgrade:

- [`scripts/vendored-config/allowlist.yaml`](../../scripts/vendored-config/allowlist.yaml)
  holds `{app, key, reason}` entries; **an entry with a missing or empty
  `reason` is itself a validation failure.** Every suppression is a reviewed,
  explained decision in git history.
- Typical legitimate entries: platform env consumed by the runtime rather than
  the app's settings model (`AZURE_CLIENT_ID` → azure-identity,
  `APPLICATIONINSIGHTS_CONNECTION_STRING` → App Insights agent).

False-positive economics: the accepted universes are deliberately generous
(case-insensitive; env-scan over-acceptance in 3.3; upstream's own key tables
unioned in), so residual false positives are rare and the allowlist cost is a
one-line entry with a sentence of justification. False *negatives* (an
over-accepted key) cost nothing new — that is today's status quo for that key.

## 5. Behavior on a vendor bump (by design)

| App | What happens on bump | What un-breaks it |
|---|---|---|
| Honcho | Universe recomputed from new pinned source; job fails **iff** shipped keys became invalid | Migrate the flagged artifacts (that's the incident, caught) |
| Hermes | Manifest pin ≠ new gitlink → job fails **always** | Re-validate polymorphic sections against new source; update manifest pin |
| PaperClip | Manifest pin ≠ new Dockerfile/compose version → job fails **always** | Re-validate manifest env list against the new release; update pin |

## 6. Implementation

- **Validator**: [`scripts/validate_vendored_config.py`](../../scripts/validate_vendored_config.py)
  — single-file Python (3.11+; needs `tomllib`-era stdlib only for parity with
  Honcho's imports), deps `pydantic`, `pydantic-settings`, `python-dotenv`,
  `PyYAML` ([`scripts/vendored-config/requirements.txt`](../../scripts/vendored-config/requirements.txt)).
  Exit 0 = every shipped key accepted; exit 1 = findings (grouped per artifact,
  each with file, key, verdict, and fix hint). `--self-test` runs seeded
  known-bad fixtures (a removed Honcho flat key, an unknown env key, a typo'd
  Hermes root key, a PaperClip version mismatch) and exits nonzero unless every
  seeded fault is *detected* — the guard proves it can fail before it is trusted
  to pass.
- **Tests**: [`tests/vendored-config/`](../../tests/vendored-config/) (pytest)
  — per-app unit tests: unknown key detected, removed-key-still-shipped
  detected with migration hint, allowlisted key passes, allowlist-without-reason
  rejected, manifest pin drift detected, plus a whole-repo run asserting the
  current tree is clean.
- **CI**: `validate-vendored-config` job in
  [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) — checkout with
  `submodules: recursive` (same as `build-context`), Python 3.12, install
  requirements, run `--self-test`, run the guard, run the tests. Enforcing from
  day one (unlike `build-context`, nothing is pending port — the guard is green
  on the current tree after the §7 fixes).

## 7. Real drift found (and fixed) on first run

The guard's first honest run against the repo found incident class (1) alive
in two shipped artifacts — both missed by the vendor-bump PR that migrated
`docker-compose.yml` and `.env.example` (#111):

1. **`infrastructure/modules/container-apps/honcho.tf`** — all three resources
   (API app, always-on deriver, scheduled deriver job) shipped the pre-3.0.7
   flat keys: `SUMMARY_PROVIDER`, `SUMMARY_MODEL`, `DERIVER_PROVIDER`,
   `DERIVER_MODEL`, and `DIALECTIC_LEVELS__<lvl>__PROVIDER` / `__MODEL` /
   `__THINKING_BUDGET_TOKENS` for all five levels — 57 removed env keys in
   total. A Terraform deploy of the pinned Honcho 3.0.11 image would have run
   every specialist and all five dialectic levels on direct-OpenAI
   `gpt-5.4-mini`. Ironically the file's own comment warns that "the old
   DIALECTIC_MINIMAL_PROVIDER style is silently ignored" — while using the
   *other* silently-ignored style.
2. **`deploy/mac-site/docker-compose.yml`** — the Honcho service's
   `environment:` block shipped the same flat keys (19 removed keys), with a
   comment correctly stating "the flat form is ignored" three lines above the
   flat form being used.

Both are migrated in this change to the `MODEL_CONFIG` schema (preserving each
file's intended per-level `MAX_TOOL_ITERATIONS`, which survived 3.0.7 and stays
flat). `DERIVER_FLUSH_ENABLED`, `DERIVER_STALE_SESSION_TIMEOUT_MINUTES`,
`LOG_LEVEL`, `DB_CONNECTION_URI`, and `LLM_OPENAI_API_KEY` were verified
still-valid against the vendored model and left alone.

## 8. What this guard does NOT do

- It cannot judge **values** (a wrong model name in a valid key still ships) —
  it guards key routing, not semantics.
- It cannot see keys delivered through `env_file:` or Key Vault secret *values*.
- The PaperClip universe is curated, not derived; a manifest curator can be
  wrong. The version pin bounds the damage to one release window and forces a
  re-look at every bump.
- It does not validate AAF-authored services' own config (see §2 out-of-scope).
