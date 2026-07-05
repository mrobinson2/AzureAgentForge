"""Tests for `_validate_request` — the request-body bounds/shape guard that
runs before any routing or upstream call. Each rejection raises HTTPException
with a specific status; a well-formed body returns None (no raise)."""

import pytest
from fastapi import HTTPException


def _ok_body(**overrides):
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return body


class TestValidateRequest:
    def test_accepts_minimal_valid_body(self, router):
        # Should not raise.
        assert router._validate_request(_ok_body()) is None

    def test_rejects_missing_messages(self, router):
        with pytest.raises(HTTPException) as exc:
            router._validate_request({"model": "gpt-4o-mini"})
        assert exc.value.status_code == 400
        assert "messages" in exc.value.detail

    def test_rejects_empty_messages_list(self, router):
        with pytest.raises(HTTPException) as exc:
            router._validate_request({"model": "x", "messages": []})
        assert exc.value.status_code == 400

    def test_rejects_messages_not_a_list(self, router):
        with pytest.raises(HTTPException) as exc:
            router._validate_request({"messages": "not-a-list"})
        assert exc.value.status_code == 400

    def test_rejects_too_many_messages(self, router, monkeypatch):
        monkeypatch.setattr(router, "_MAX_MESSAGES", 3)
        body = {"messages": [{"role": "user", "content": str(i)} for i in range(4)]}
        with pytest.raises(HTTPException) as exc:
            router._validate_request(body)
        assert exc.value.status_code == 400
        assert "Too many messages" in exc.value.detail

    def test_accepts_exactly_max_messages(self, router, monkeypatch):
        monkeypatch.setattr(router, "_MAX_MESSAGES", 3)
        body = {"messages": [{"role": "user", "content": str(i)} for i in range(3)]}
        assert router._validate_request(body) is None

    def test_rejects_message_without_role(self, router):
        with pytest.raises(HTTPException) as exc:
            router._validate_request({"messages": [{"content": "no role here"}]})
        assert exc.value.status_code == 400
        assert "role" in exc.value.detail

    def test_rejects_message_not_a_dict(self, router):
        with pytest.raises(HTTPException) as exc:
            router._validate_request({"messages": ["just a string"]})
        assert exc.value.status_code == 400

    def test_rejects_overlong_model_name(self, router):
        long_name = "m" * (router._MAX_MODEL_NAME_LEN + 1)
        with pytest.raises(HTTPException) as exc:
            router._validate_request(_ok_body(model=long_name))
        assert exc.value.status_code == 400
        assert "Model name too long" in exc.value.detail

    def test_accepts_model_name_at_limit(self, router):
        name = "m" * router._MAX_MODEL_NAME_LEN
        assert router._validate_request(_ok_body(model=name)) is None

    @pytest.mark.parametrize("temp", [-0.1, 2.1, "warm", [1]])
    def test_rejects_out_of_range_or_nonnumeric_temperature(self, router, temp):
        with pytest.raises(HTTPException) as exc:
            router._validate_request(_ok_body(temperature=temp))
        assert exc.value.status_code == 400
        assert "Temperature" in exc.value.detail

    @pytest.mark.parametrize("temp", [0, 0.7, 1, 2])
    def test_accepts_in_range_temperature(self, router, temp):
        assert router._validate_request(_ok_body(temperature=temp)) is None

    def test_temperature_none_is_allowed(self, router):
        assert router._validate_request(_ok_body(temperature=None)) is None

    @pytest.mark.parametrize("mt", [0, -5, 1.5, "lots"])
    def test_rejects_invalid_max_tokens(self, router, mt):
        with pytest.raises(HTTPException) as exc:
            router._validate_request(_ok_body(max_tokens=mt))
        assert exc.value.status_code == 400
        assert "max_tokens" in exc.value.detail

    def test_accepts_positive_int_max_tokens(self, router):
        assert router._validate_request(_ok_body(max_tokens=1024)) is None

    def test_max_tokens_none_is_allowed(self, router):
        assert router._validate_request(_ok_body(max_tokens=None)) is None
