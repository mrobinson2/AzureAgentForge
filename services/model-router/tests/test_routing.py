"""
Unit tests for model-router pure routing functions.

The module requires GPT4O_API_KEY, PHI_BASE_URL, and PHI_API_KEY at import
time (os.environ["..."] — hard KeyError if absent). These are set below to
sentinel values before the import so no real network call is made; all tested
functions are pure (no I/O).
"""
import importlib
import os
import pathlib
import sys

# Set required env vars BEFORE importing main.
# With the tolerant registration, the module imports cleanly with no env set,
# but the tests assert tier presence/routing, so we prime both tiers here.
os.environ.setdefault("GPT4O_BASE_URL", "http://localhost:8888")
os.environ.setdefault("GPT4O_API_KEY", "test-key")
os.environ.setdefault("PHI_BASE_URL", "http://localhost:9999")
os.environ.setdefault("PHI_API_KEY", "test-phi-key")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
main = importlib.import_module("main")


# ── select_tier ──────────────────────────────────────────────────────────────

def test_select_tier_returns_string():
    """select_tier always returns a non-empty string."""
    tier = main.select_tier({"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
    assert isinstance(tier, str) and tier


def test_select_tier_gpt4o_mini_hint():
    """'gpt-4o-mini' in model field maps to the 'gpt4o-mini' registered tier."""
    tier = main.select_tier({"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
    assert tier == "gpt4o-mini"


def test_select_tier_4o_mini_substring():
    """Any model value containing '4o-mini' maps to 'gpt4o-mini'."""
    tier = main.select_tier({"model": "azure/4o-mini", "messages": [{"role": "user", "content": "hi"}]})
    assert tier == "gpt4o-mini"


def test_select_tier_phi4_hint():
    """'phi4' in model field maps to the 'phi4' registered tier."""
    tier = main.select_tier({"model": "phi4", "messages": [{"role": "user", "content": "hi"}]})
    assert tier == "phi4"


def test_select_tier_phi4_hyphen_hint():
    """'phi-4' in model field also maps to 'phi4'."""
    tier = main.select_tier({"model": "phi-4", "messages": [{"role": "user", "content": "hi"}]})
    assert tier == "phi4"


def test_select_tier_explicit_tier_field():
    """An explicit 'tier' field in the body overrides the model hint."""
    tier = main.select_tier({"tier": "phi4", "model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
    assert tier == "phi4"


def test_select_tier_metadata_tier_field():
    """tier nested in metadata.tier is honored."""
    tier = main.select_tier({
        "metadata": {"tier": "phi4"},
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert tier == "phi4"


def test_select_tier_registered_model_name_passthrough():
    """A model value that exactly matches a registered tier key returns that tier."""
    # 'gpt4o-mini' is always registered; asking for it by its MODELS key should
    # resolve directly via the exact-match branch (not the substring hint branch).
    tier = main.select_tier({"model": "gpt4o-mini", "messages": [{"role": "user", "content": "hi"}]})
    assert tier == "gpt4o-mini"
    assert tier in main.MODELS


def test_select_tier_unknown_model_returns_string_and_registered():
    """An unknown model name is registered as a passthrough tier and returned."""
    tier = main.select_tier({"model": "some-unknown-deployment", "messages": [{"role": "user", "content": "hi"}]})
    assert isinstance(tier, str) and tier
    # The passthrough path registers the ephemeral tier into MODELS
    assert tier in main.MODELS


def test_select_tier_no_model_defaults_to_gpt4o_mini():
    """With no model field and no persona, select_tier falls back to gpt4o-mini."""
    tier = main.select_tier({"messages": [{"role": "user", "content": "hi"}]})
    assert tier == "gpt4o-mini"


def test_select_tier_persona_lookup():
    """A known persona key in PERSONA_TIERS resolves to its mapped tier."""
    # Temporarily inject a persona mapping so the test is self-contained.
    original = dict(main.PERSONA_TIERS)
    main.PERSONA_TIERS["test-persona"] = "phi4"
    try:
        tier = main.select_tier({
            "persona": "test-persona",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert tier == "phi4"
    finally:
        main.PERSONA_TIERS.clear()
        main.PERSONA_TIERS.update(original)


def test_select_tier_unknown_persona_defaults_to_gpt4o_mini():
    """An unmapped persona falls back to the default tier instead of erroring."""
    original = dict(main.PERSONA_TIERS)
    main.PERSONA_TIERS.clear()
    main.PERSONA_TIERS["known-persona"] = "phi4"
    try:
        tier = main.select_tier({
            "persona": "unknown-persona",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert tier == "gpt4o-mini"
    finally:
        main.PERSONA_TIERS.clear()
        main.PERSONA_TIERS.update(original)


# ── _build_fallback_chain ────────────────────────────────────────────────────

def test_fallback_chain_is_list_of_registered_tiers():
    """_build_fallback_chain returns a list whose entries are all in MODELS."""
    chain = main._build_fallback_chain("gpt4o-mini", 10, 100)
    assert isinstance(chain, list)
    assert all(t in main.MODELS for t in chain)


def test_fallback_chain_gpt4o_mini_includes_phi4():
    """gpt4o-mini's fallback preference includes phi4."""
    chain = main._build_fallback_chain("gpt4o-mini", 10, 100)
    assert "phi4" in chain


def test_fallback_chain_phi4_is_empty():
    """phi4 has no further fallback — it is the last resort tier."""
    chain = main._build_fallback_chain("phi4", 10, 100)
    assert chain == []


def test_fallback_chain_unknown_tier_is_list():
    """A tier with no preference entry returns an empty list, not an error."""
    chain = main._build_fallback_chain("nonexistent-tier", 10, 100)
    assert isinstance(chain, list)


# ── MODELS dict ──────────────────────────────────────────────────────────────

def test_models_contains_baseline_tiers():
    """gpt4o-mini and phi4 are always registered at startup."""
    assert "gpt4o-mini" in main.MODELS
    assert "phi4" in main.MODELS


def test_models_gpt4o_mini_supports_tools():
    """gpt4o-mini tier has supports_tools=True."""
    assert main.MODELS["gpt4o-mini"]["supports_tools"] is True


def test_models_phi4_does_not_support_tools():
    """phi4 tier has supports_tools=False (per its Foundry capability)."""
    assert main.MODELS["phi4"]["supports_tools"] is False


def test_models_entries_have_required_keys():
    """Every registered tier has the keys the router depends on."""
    required = {"litellm_model", "api_base", "api_key", "daily_budget", "max_tokens",
                "context_limit", "timeout_seconds", "supports_tools"}
    for tier, cfg in main.MODELS.items():
        missing = required - cfg.keys()
        assert not missing, f"Tier '{tier}' is missing keys: {missing}"
