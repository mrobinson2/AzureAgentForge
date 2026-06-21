"""Tests for `_register_foundry_tier` — the per-deployment env-driven tier
registration used for Claude/Kimi/Grok Foundry deployments.

The function reads `<PREFIX>_BASE_URL`/`_API_KEY`/`_MODEL` at *call* time and
writes the deployment into the module-global MODELS, so it can be exercised
directly with monkeypatched env (no module re-import needed). The autouse
isolation fixture restores MODELS after each test."""


class TestRegisterFoundryTier:
    def test_noop_when_env_absent(self, router, monkeypatch):
        for suffix in ("BASE_URL", "API_KEY", "MODEL"):
            monkeypatch.delenv(f"ABSENTPREFIX_{suffix}", raising=False)
        before = set(router.MODELS.keys())
        router._register_foundry_tier("ABSENTPREFIX", default_budget=0.25)
        assert set(router.MODELS.keys()) == before

    def test_noop_when_partial_env(self, router, monkeypatch):
        # BASE_URL + API_KEY present but MODEL missing → still a no-op.
        monkeypatch.setenv("PARTIALPREFIX_BASE_URL", "https://host/openai/v1/")
        monkeypatch.setenv("PARTIALPREFIX_API_KEY", "k")
        monkeypatch.delenv("PARTIALPREFIX_MODEL", raising=False)
        before = set(router.MODELS.keys())
        router._register_foundry_tier("PARTIALPREFIX", default_budget=0.25)
        assert set(router.MODELS.keys()) == before

    def test_registers_openai_prefix_tier(self, router, monkeypatch):
        monkeypatch.setenv("KIMITEST_BASE_URL", "https://kimi.example/openai/v1/")
        monkeypatch.setenv("KIMITEST_API_KEY", "kimi-key")
        monkeypatch.setenv("KIMITEST_MODEL", "Kimi-Test")
        router._register_foundry_tier("KIMITEST", default_budget=0.25)

        cfg = router.MODELS["Kimi-Test"]
        assert cfg["litellm_model"] == "openai/Kimi-Test"
        # openai prefix keeps the base URL verbatim (no /anthropic rewrite).
        assert cfg["api_base"] == "https://kimi.example/openai/v1/"
        assert cfg["api_key"] == "kimi-key"
        assert cfg["daily_budget"] == 0.25
        assert cfg["supports_tools"] is True

    def test_anthropic_prefix_rewrites_openai_v1_suffix(self, router, monkeypatch):
        monkeypatch.setenv("CLAUDETEST_BASE_URL", "https://foundry.example/openai/v1/")
        monkeypatch.setenv("CLAUDETEST_API_KEY", "claude-key")
        monkeypatch.setenv("CLAUDETEST_MODEL", "claude-sonnet-test")
        router._register_foundry_tier(
            "CLAUDETEST", default_budget=0.25, litellm_prefix="anthropic"
        )

        cfg = router.MODELS["claude-sonnet-test"]
        assert cfg["litellm_model"] == "anthropic/claude-sonnet-test"
        # /openai/v1/ suffix is rewritten to /anthropic for the SDK base URL.
        assert cfg["api_base"] == "https://foundry.example/anthropic"
        assert router._is_anthropic_tier("claude-sonnet-test") is True

    def test_anthropic_prefix_without_suffix_left_alone(self, router, monkeypatch):
        monkeypatch.setenv("CLAUDEBARE_BASE_URL", "https://foundry.example/anthropic")
        monkeypatch.setenv("CLAUDEBARE_API_KEY", "k")
        monkeypatch.setenv("CLAUDEBARE_MODEL", "claude-bare")
        router._register_foundry_tier(
            "CLAUDEBARE", default_budget=0.25, litellm_prefix="anthropic"
        )
        assert router.MODELS["claude-bare"]["api_base"] == "https://foundry.example/anthropic"

    def test_daily_budget_env_override(self, router, monkeypatch):
        monkeypatch.setenv("GROKTEST_BASE_URL", "https://grok.example/openai/v1/")
        monkeypatch.setenv("GROKTEST_API_KEY", "k")
        monkeypatch.setenv("GROKTEST_MODEL", "grok-test")
        monkeypatch.setenv("GROKTEST_DAILY_BUDGET_USD", "9.50")
        router._register_foundry_tier("GROKTEST", default_budget=2.00)
        assert router.MODELS["grok-test"]["daily_budget"] == 9.50

    def test_default_pending_fallback_is_gpt4o_mini(self, router, monkeypatch):
        monkeypatch.setenv("FBTEST_BASE_URL", "https://fb.example/openai/v1/")
        monkeypatch.setenv("FBTEST_API_KEY", "k")
        monkeypatch.setenv("FBTEST_MODEL", "fb-test")
        router._register_foundry_tier("FBTEST", default_budget=0.25)
        assert router.MODELS["fb-test"]["_pending_fallback"] == ["gpt4o-mini"]

    def test_custom_fallback_list_preserved(self, router, monkeypatch):
        monkeypatch.setenv("FB2TEST_BASE_URL", "https://fb2.example/openai/v1/")
        monkeypatch.setenv("FB2TEST_API_KEY", "k")
        monkeypatch.setenv("FB2TEST_MODEL", "fb2-test")
        router._register_foundry_tier(
            "FB2TEST", default_budget=0.25, fallback=["phi4", "gpt4o-mini"]
        )
        assert router.MODELS["fb2-test"]["_pending_fallback"] == ["phi4", "gpt4o-mini"]

    def test_supports_tools_flag_passthrough(self, router, monkeypatch):
        monkeypatch.setenv("NOTOOLS_BASE_URL", "https://nt.example/openai/v1/")
        monkeypatch.setenv("NOTOOLS_API_KEY", "k")
        monkeypatch.setenv("NOTOOLS_MODEL", "notools-test")
        router._register_foundry_tier(
            "NOTOOLS", default_budget=0.25, supports_tools=False
        )
        assert router.MODELS["notools-test"]["supports_tools"] is False

    def test_noop_when_base_url_is_kv_placeholder(self, router, monkeypatch):
        # seed-keyvault.sh seeds "__unset__" for unprovided externals; an
        # unconfigured tier must be skipped, not registered against the
        # placeholder (which would 5xx at request time).
        monkeypatch.setenv("PHURL_BASE_URL", "__unset__")
        monkeypatch.setenv("PHURL_API_KEY", "real-key")
        monkeypatch.setenv("PHURL_MODEL", "phurl-test")
        before = set(router.MODELS.keys())
        router._register_foundry_tier("PHURL", default_budget=0.25)
        assert set(router.MODELS.keys()) == before

    def test_noop_when_api_key_is_kv_placeholder(self, router, monkeypatch):
        monkeypatch.setenv("PHKEY_BASE_URL", "https://host/openai/v1/")
        monkeypatch.setenv("PHKEY_API_KEY", "__unset__")
        monkeypatch.setenv("PHKEY_MODEL", "phkey-test")
        before = set(router.MODELS.keys())
        router._register_foundry_tier("PHKEY", default_budget=0.25)
        assert set(router.MODELS.keys()) == before

    def test_noop_when_placeholder_has_surrounding_whitespace(self, router, monkeypatch):
        # The sentinel matches after strip(), so "  __unset__  " also skips.
        monkeypatch.setenv("PHWS_BASE_URL", "  __unset__  ")
        monkeypatch.setenv("PHWS_API_KEY", "k")
        monkeypatch.setenv("PHWS_MODEL", "phws-test")
        before = set(router.MODELS.keys())
        router._register_foundry_tier("PHWS", default_budget=0.25)
        assert set(router.MODELS.keys()) == before


class TestTierEnv:
    """`_tier_env` normalizes the Key Vault unset-placeholder and blank/
    whitespace values to "" so the truthy tier-gating skips an unconfigured
    tier instead of registering it against a bogus endpoint."""

    def test_real_value_passes_through(self, router, monkeypatch):
        monkeypatch.setenv("TE_BASE_URL", "https://real.example/v1")
        assert router._tier_env("TE_BASE_URL") == "https://real.example/v1"

    def test_real_value_is_stripped(self, router, monkeypatch):
        monkeypatch.setenv("TE_KEY", "  sk-abc  ")
        assert router._tier_env("TE_KEY") == "sk-abc"

    def test_kv_placeholder_normalizes_to_empty(self, router, monkeypatch):
        monkeypatch.setenv("TE_KEY", "__unset__")
        assert router._tier_env("TE_KEY") == ""

    def test_whitespace_padded_placeholder_normalizes_to_empty(self, router, monkeypatch):
        monkeypatch.setenv("TE_KEY", "  __unset__ ")
        assert router._tier_env("TE_KEY") == ""

    def test_blank_normalizes_to_empty(self, router, monkeypatch):
        monkeypatch.setenv("TE_KEY", "   ")
        assert router._tier_env("TE_KEY") == ""

    def test_absent_returns_empty_default(self, router, monkeypatch):
        monkeypatch.delenv("TE_MISSING", raising=False)
        assert router._tier_env("TE_MISSING") == ""
