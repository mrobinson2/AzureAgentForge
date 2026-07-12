"""A3 — vendored-config schema guard unit tests.

Prove the guard DETECTS the incident class (removed/unknown keys shipped to a
vendored app), that legitimate suppression paths work (allowlist with a
mandatory reason), that vendor bumps fail loudly (manifest pin drift), and
that the current repo state is clean.

Requires the vendored submodules (apps/honcho/src, apps/hermes/src) checked
out plus pydantic-settings — exactly what the validate-vendored-config CI job
provides; tests that need more skip gracefully elsewhere (e.g. the generic
`python` CI job never collects this directory).
See docs/design/vendored-config-schema-guard.md.
"""

import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")

import validate_vendored_config as guard  # noqa: E402

HONCHO_AVAILABLE = (REPO / "apps/honcho/src/src/config.py").exists()
HERMES_AVAILABLE = (REPO / "apps/hermes/src/hermes_cli/config.py").exists()

needs_honcho = pytest.mark.skipif(not HONCHO_AVAILABLE, reason="honcho submodule not checked out")
needs_hermes = pytest.mark.skipif(not HERMES_AVAILABLE, reason="hermes submodule not checked out")


# ── Honcho: dynamic pydantic universe ────────────────────────────────────────

@pytest.fixture(scope="module")
def honcho_universe():
    if not HONCHO_AVAILABLE:
        pytest.skip("honcho submodule not checked out")
    mod = guard.load_honcho_settings_module(REPO)
    return guard.honcho_accepted_env(mod)


@needs_honcho
def test_honcho_removed_flat_key_detected_with_migration_hint(honcho_universe):
    """The exact Honcho 3.0.7 incident: a removed flat key must be flagged,
    and the message must carry the nested MODEL_CONFIG replacement."""
    exact, patterns = honcho_universe
    findings = guard.check_env_keys("honcho", "<fixture>", ["SUMMARY_PROVIDER"],
                                    exact, patterns, set(), guard._HONCHO_REMOVED)
    assert len(findings) == 1
    assert "REMOVED" in findings[0].message
    assert "SUMMARY_MODEL_CONFIG__TRANSPORT" in findings[0].message


@needs_honcho
def test_honcho_removed_dialectic_level_keys_detected(honcho_universe):
    exact, patterns = honcho_universe
    shipped = ["DIALECTIC_LEVELS__minimal__PROVIDER",
               "DIALECTIC_LEVELS__max__MODEL",
               "DIALECTIC_LEVELS__high__THINKING_BUDGET_TOKENS"]
    findings = guard.check_env_keys("honcho", "<fixture>", shipped,
                                    exact, patterns, set(), guard._HONCHO_REMOVED)
    assert {f.key for f in findings} == set(shipped)
    assert all("MODEL_CONFIG" in f.message for f in findings)


@needs_honcho
def test_honcho_unknown_key_detected(honcho_universe):
    exact, patterns = honcho_universe
    findings = guard.check_env_keys("honcho", "<fixture>", ["HONCHO_TYPO_KEY"],
                                    exact, patterns, set())
    assert [f.key for f in findings] == ["HONCHO_TYPO_KEY"]
    assert "unknown key" in findings[0].message


@needs_honcho
def test_honcho_typoed_dialectic_level_name_detected(honcho_universe):
    """dict[Literal[...]] closes the level-name set: a typo'd level would
    silently CREATE a new level upstream — must be flagged."""
    exact, patterns = honcho_universe
    findings = guard.check_env_keys(
        "honcho", "<fixture>", ["DIALECTIC_LEVELS__hgih__MODEL_CONFIG__MODEL"],
        exact, patterns, set())
    assert len(findings) == 1


@needs_honcho
def test_honcho_valid_keys_pass(honcho_universe):
    """Every key shape the repo actually ships (nested MODEL_CONFIG paths,
    per-section prefixes, OVERRIDES sub-model, case-insensitive levels)."""
    exact, patterns = honcho_universe
    good = [
        "DB_CONNECTION_URI",
        "LLM_OPENAI_API_KEY", "LLM_ANTHROPIC_API_KEY", "LLM_GEMINI_API_KEY",
        "LOG_LEVEL",
        "SUMMARY_MODEL_CONFIG__TRANSPORT", "SUMMARY_MODEL_CONFIG__MODEL",
        "DERIVER_MODEL_CONFIG__TRANSPORT", "DERIVER_MODEL_CONFIG__MODEL",
        "DERIVER_FLUSH_ENABLED", "DERIVER_STALE_SESSION_TIMEOUT_MINUTES",
        "DERIVER_MODEL_CONFIG__OVERRIDES__BASE_URL",
        "DIALECTIC_LEVELS__minimal__MODEL_CONFIG__TRANSPORT",
        "DIALECTIC_LEVELS__max__MODEL_CONFIG__THINKING_BUDGET_TOKENS",
        "DIALECTIC_LEVELS__low__MAX_TOOL_ITERATIONS",
    ]
    findings = guard.check_env_keys("honcho", "<fixture>", good, exact, patterns, set())
    assert findings == [], [f.key for f in findings]


@needs_honcho
def test_honcho_allowlisted_key_passes(honcho_universe):
    exact, patterns = honcho_universe
    findings = guard.check_env_keys("honcho", "<fixture>", ["AZURE_CLIENT_ID"],
                                    exact, patterns, {("honcho", "AZURE_CLIENT_ID")})
    assert findings == []


# ── Allowlist hygiene ────────────────────────────────────────────────────────

def test_allowlist_entry_without_reason_is_a_finding(tmp_path, monkeypatch):
    bad = {"allow": [{"app": "honcho", "key": "WHATEVER"}]}
    (tmp_path / "scripts" / "vendored-config").mkdir(parents=True)
    (tmp_path / "scripts" / "vendored-config" / "allowlist.yaml").write_text(yaml.dump(bad))
    entries, findings = guard.load_allowlist(tmp_path)
    assert entries == set()
    assert len(findings) == 1
    assert "reason" in findings[0].message


def test_repo_allowlist_all_entries_have_reasons():
    entries, findings = guard.load_allowlist(REPO)
    assert findings == [], [str(f) for f in findings]
    assert entries  # the platform-env entries exist


# ── Hermes config.yaml: AST universe + manifest ──────────────────────────────

@pytest.fixture(scope="module")
def hermes_parsed():
    if not HERMES_AVAILABLE:
        pytest.skip("hermes submodule not checked out")
    return guard.parse_hermes_config_source(REPO)


@pytest.fixture(scope="module")
def hermes_poly():
    manifest = guard.load_manifest(REPO, "manifest-hermes.yaml")
    return {s: set(k or []) for s, k in (manifest.get("polymorphic_sections") or {}).items()}


@needs_hermes
def test_hermes_unknown_root_key_detected(hermes_parsed, hermes_poly):
    cfg = {"prompt_cachng": {"cache_ttl": "1h"}}  # seeded typo of prompt_caching
    findings = guard._check_yaml_paths("hermes", "<fixture>", cfg,
                                       hermes_parsed["default_config"], hermes_poly,
                                       hermes_parsed["known_root_keys"], set())
    assert [f.key for f in findings] == ["prompt_cachng"]


@needs_hermes
def test_hermes_unknown_nested_key_detected(hermes_parsed, hermes_poly):
    cfg = {"prompt_caching": {"cache_ttll": "1h"},        # typo'd leaf
           "model": {"provider": "custom", "baseurl": "x"}}  # typo'd polymorphic subkey
    findings = guard._check_yaml_paths("hermes", "<fixture>", cfg,
                                       hermes_parsed["default_config"], hermes_poly,
                                       hermes_parsed["known_root_keys"], set())
    assert {f.key for f in findings} == {"prompt_caching.cache_ttll", "model.baseurl"}


@needs_hermes
def test_hermes_generated_configs_valid(hermes_parsed, hermes_poly):
    """Every shipped config.yaml generator's output must be accepted."""
    generators = guard.hermes_config_generators(REPO)
    assert generators, "no Hermes config.yaml generator found in the repo"
    # A1 keeps the canonical generator in write-hermes-config.sh plus a copy
    # in the services/ entrypoint — discovery must see both.
    names = {g.name for g in generators}
    assert "write-hermes-config.sh" in names
    for script in generators:
        cfg = guard.extract_heredoc_yaml(script)
        assert cfg, f"no heredoc config extracted from {script}"
        findings = guard._check_yaml_paths("hermes", str(script), cfg,
                                           hermes_parsed["default_config"], hermes_poly,
                                           hermes_parsed["known_root_keys"], set())
        assert findings == [], [str(f) for f in findings]


@needs_hermes
def test_hermes_manifest_pin_matches_submodule_gitlink():
    """Vendor-bump tripwire: manifest pin == superproject gitlink."""
    manifest = guard.load_manifest(REPO, "manifest-hermes.yaml")
    actual = guard.gitlink_sha(REPO, "apps/hermes/src")
    if actual is None:
        pytest.skip("not a git checkout")
    assert manifest["pinned_commit"] == actual, (
        "hermes vendor bump detected — re-validate manifest-hermes.yaml "
        "polymorphic sections against the new source, then update pinned_commit")


@needs_hermes
def test_hermes_env_universe_detects_never_read_key():
    universe = guard.hermes_env_universe(REPO)
    findings = guard.check_env_keys("hermes", "<fixture>",
                                    ["HERMES_DB_PATH"], universe, [], set())
    # the pinned hermes has no HERMES_DB_PATH override — the guard must say so
    assert [f.key for f in findings] == ["HERMES_DB_PATH"]
    assert guard.check_env_keys("hermes", "<fixture>",
                                ["HERMES_KANBAN_DB", "HERMES_HOME"],
                                universe, [], set()) == []


# ── PaperClip: curated manifest + version-pin tripwire ───────────────────────

def test_paperclip_manifest_pin_matches_dockerfile_and_compose():
    manifest = guard.load_manifest(REPO, "manifest-paperclip.yaml")
    versions = guard.paperclip_pinned_versions(REPO)
    assert versions, "could not extract PAPERCLIP_VERSION pins"
    for source, version in versions.items():
        assert version == manifest["pinned_version"], (
            f"{source} pins {version}, manifest pins {manifest['pinned_version']} — "
            "vendor bump: re-validate manifest-paperclip.yaml")


def test_paperclip_unknown_key_detected():
    manifest = guard.load_manifest(REPO, "manifest-paperclip.yaml")
    accepted = {str(e["key"]).upper() for e in manifest["accepted_env"]}
    findings = guard.check_env_keys("paperclip", "<fixture>",
                                    ["PAPERCLIP_TYPO_KEY"], accepted, [], set())
    assert [f.key for f in findings] == ["PAPERCLIP_TYPO_KEY"]


# ── Terraform / compose artifact parsing ─────────────────────────────────────

def test_tf_container_scoping_excludes_sidecar_env():
    """hermes.tf carries a model-router sidecar; its env must not be attributed
    to the hermes container."""
    tf = REPO / "infrastructure/modules/container-apps/hermes.tf"
    hermes_keys = set(guard.terraform_env_keys(tf, containers=["hermes"]))
    all_keys = set(guard.terraform_env_keys(tf))
    assert hermes_keys < all_keys
    assert "OPENAI_BASE_URL" in hermes_keys
    # router-tier env lives only in the sidecar
    assert "PHI_BASE_URL" not in hermes_keys
    assert "PHI_BASE_URL" in all_keys


def test_compose_env_keys_reads_dict_form():
    keys = guard.compose_env_keys(REPO / "docker-compose.yml", "honcho")
    assert "DB_CONNECTION_URI" in keys
    assert "SUMMARY_MODEL_CONFIG__TRANSPORT" in keys


# ── The bottom line ──────────────────────────────────────────────────────────

@needs_honcho
@needs_hermes
def test_repo_is_clean():
    """The whole guard, against the real repo: no shipped key may be unread by
    its pinned vendored consumer. This is the check CI enforces."""
    findings = guard.validate_all(REPO)
    assert findings == [], "\n".join(str(f) for f in findings)


@needs_honcho
@needs_hermes
def test_self_test_passes():
    assert guard.self_test(REPO) == 0
