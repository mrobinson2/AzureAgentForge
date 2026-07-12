"""Tests for the /v1/embeddings passthrough (governor Plane C vector retrieval).

litellm.aembedding is monkeypatched — these never leave the process.

The provider-prefix pin tests guard the load-bearing lesson ported from the
upstream private deployment: a bare model name plus an azure.com api_base makes
LiteLLM pick its AZURE provider (api-key header auth), which Azure AI Foundry's
OpenAI-compatible endpoint rejects with 400 unknown_model. The router must pin
the `openai/` prefix so provider detection stays on Bearer auth.
"""


def _ok_response(**_kwargs):
    # Shape mirrors what litellm.aembedding returns (OpenAI-compatible). A plain
    # dict here exercises the handler's dict fallback (no .model_dump()).
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
        "model": "text-embedding-3-small",
        "usage": {"prompt_tokens": 3, "total_tokens": 3},
    }


class TestEmbeddingsEndpoint:
    def test_503_when_unconfigured(self, client, router, monkeypatch):
        # default test env sets no embedding key — fail LOUD, not silent
        monkeypatch.setattr(router, "_EMBED_API_KEY", None)
        r = client.post("/v1/embeddings", json={"input": "hello world"})
        assert r.status_code == 503
        assert "not configured" in r.json()["detail"]

    def test_happy_path_returns_vector(self, client, router, monkeypatch):
        monkeypatch.setattr(router, "_EMBED_API_KEY", "test-embed-key")

        async def fake(**kwargs):
            # The pinned LiteLLM model string is what goes upstream.
            assert kwargs["model"] == router._EMBED_LITELLM_MODEL
            assert kwargs["input"] == "hello world"
            return _ok_response()

        monkeypatch.setattr(router.litellm, "aembedding", fake)
        r = client.post("/v1/embeddings", json={"input": "hello world"})
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "list"
        assert body["data"][0]["embedding"] == [0.1, 0.2, 0.3]
        assert body["model"] == "text-embedding-3-small"

    def test_missing_input_400(self, client, router, monkeypatch):
        monkeypatch.setattr(router, "_EMBED_API_KEY", "test-embed-key")
        r = client.post("/v1/embeddings", json={})
        assert r.status_code == 400

    def test_too_many_inputs_400(self, client, router, monkeypatch):
        monkeypatch.setattr(router, "_EMBED_API_KEY", "test-embed-key")
        monkeypatch.setattr(router, "_EMBED_MAX_INPUTS", 2)
        r = client.post("/v1/embeddings", json={"input": ["a", "b", "c"]})
        assert r.status_code == 400

    def test_provider_error_502(self, client, router, monkeypatch):
        monkeypatch.setattr(router, "_EMBED_API_KEY", "test-embed-key")

        async def boom(**kwargs):
            raise RuntimeError("upstream down")

        monkeypatch.setattr(router.litellm, "aembedding", boom)
        r = client.post("/v1/embeddings", json={"input": "hi"})
        assert r.status_code == 502


class TestProviderPrefixPin:
    """The openai/ prefix pin — see _pin_embedding_provider in main.py."""

    def test_bare_model_gets_openai_prefix(self, router):
        assert (
            router._pin_embedding_provider("text-embedding-3-small")
            == "openai/text-embedding-3-small"
        )

    def test_explicit_provider_prefix_honored(self, router):
        # An operator who really wants api-key-header auth (classic Azure
        # OpenAI resource) can say so explicitly; the pin must not stack.
        assert router._pin_embedding_provider("azure/my-deployment") == "azure/my-deployment"
        assert (
            router._pin_embedding_provider("openai/text-embedding-3-small")
            == "openai/text-embedding-3-small"
        )

    def test_module_default_is_pinned(self, router):
        # Import-time wiring: the string handed to LiteLLM carries a provider
        # prefix even though EMBEDDING_MODEL defaults to a bare model name.
        assert router._EMBED_LITELLM_MODEL == router._pin_embedding_provider(router._EMBED_MODEL)
        assert "/" in router._EMBED_LITELLM_MODEL

    def test_pinned_model_and_base_reach_litellm(self, client, router, monkeypatch):
        # The Foundry scenario end-to-end (mocked upstream): azure.com api_base
        # + bare model must reach litellm.aembedding with the openai/ prefix,
        # or Foundry answers 400 unknown_model (api-key-header auth).
        monkeypatch.setattr(router, "_EMBED_API_KEY", "test-foundry-key")
        monkeypatch.setattr(
            router, "_EMBED_API_BASE", "https://example-foundry.services.azure.com/openai/v1"
        )
        monkeypatch.setattr(router, "_EMBED_MODEL", "text-embedding-3-small")
        monkeypatch.setattr(
            router,
            "_EMBED_LITELLM_MODEL",
            router._pin_embedding_provider("text-embedding-3-small"),
        )
        seen = {}

        async def fake(**kwargs):
            seen.update(kwargs)
            return _ok_response()

        monkeypatch.setattr(router.litellm, "aembedding", fake)
        r = client.post("/v1/embeddings", json={"input": "hello world"})
        assert r.status_code == 200
        assert seen["model"] == "openai/text-embedding-3-small"
        assert seen["api_base"] == "https://example-foundry.services.azure.com/openai/v1"
        assert seen["api_key"] == "test-foundry-key"

    def test_response_model_field_stays_unprefixed(self, client, router, monkeypatch):
        # When the upstream omits `model`, the fallback is the operator-facing
        # name — callers must never see the internal openai/ routing prefix.
        monkeypatch.setattr(router, "_EMBED_API_KEY", "test-embed-key")

        async def fake(**kwargs):
            resp = _ok_response()
            del resp["model"]
            return resp

        monkeypatch.setattr(router.litellm, "aembedding", fake)
        r = client.post("/v1/embeddings", json={"input": "hi"})
        assert r.status_code == 200
        assert r.json()["model"] == router._EMBED_MODEL
        assert not r.json()["model"].startswith("openai/")
