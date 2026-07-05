"""Tests for the /v1/embeddings passthrough (governor Plane C vector retrieval).

litellm.aembedding is monkeypatched — these never leave the process.
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
        # default test env sets no embedding key
        monkeypatch.setattr(router, "_EMBED_API_KEY", None)
        r = client.post("/v1/embeddings", json={"input": "hello world"})
        assert r.status_code == 503
        assert "not configured" in r.json()["detail"]

    def test_happy_path_returns_vector(self, client, router, monkeypatch):
        monkeypatch.setattr(router, "_EMBED_API_KEY", "test-embed-key")

        async def fake(**kwargs):
            assert kwargs["model"] == router._EMBED_MODEL
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
