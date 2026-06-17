"""Tests for the OpenAI <-> Anthropic translation layer used by the
direct-call bypass for Claude-on-Foundry tiers. These functions are pure
(no I/O): tool-shape conversion, tool_choice mapping, message-stream
conversion, response conversion, and Anthropic-tier selection for the
native /v1/messages route."""

import pytest
from fastapi import HTTPException


# ── _oai_tools_to_anthropic ──────────────────────────────────────────────────

class TestToolsToAnthropic:
    def test_none_and_empty(self, router):
        assert router._oai_tools_to_anthropic(None) == []
        assert router._oai_tools_to_anthropic([]) == []

    def test_basic_function_tool(self, router):
        tools = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Look up weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }]
        out = router._oai_tools_to_anthropic(tools)
        assert len(out) == 1
        assert out[0]["name"] == "get_weather"
        assert out[0]["description"] == "Look up weather"
        assert out[0]["input_schema"]["properties"]["city"]["type"] == "string"

    def test_empty_parameters_are_padded(self, router):
        tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
        out = router._oai_tools_to_anthropic(tools)
        assert out[0]["input_schema"] == {"type": "object", "properties": {}}

    def test_schema_without_type_is_padded(self, router):
        tools = [{"type": "function", "function": {"name": "x", "parameters": {"properties": {}}}}]
        out = router._oai_tools_to_anthropic(tools)
        assert out[0]["input_schema"] == {"type": "object", "properties": {}}

    def test_non_function_tool_skipped(self, router):
        tools = [{"type": "retrieval"}, {"type": "function", "function": {"name": "keep"}}]
        out = router._oai_tools_to_anthropic(tools)
        assert [t["name"] for t in out] == ["keep"]

    def test_tool_without_name_skipped(self, router):
        tools = [{"type": "function", "function": {"description": "no name"}}]
        assert router._oai_tools_to_anthropic(tools) == []

    def test_cache_control_preserved_from_envelope(self, router):
        tools = [{
            "type": "function",
            "cache_control": {"type": "ephemeral"},
            "function": {"name": "cached"},
        }]
        out = router._oai_tools_to_anthropic(tools)
        assert out[0]["cache_control"] == {"type": "ephemeral"}

    def test_cache_control_preserved_from_function_block(self, router):
        tools = [{
            "type": "function",
            "function": {"name": "cached", "cache_control": {"type": "ephemeral"}},
        }]
        out = router._oai_tools_to_anthropic(tools)
        assert out[0]["cache_control"] == {"type": "ephemeral"}


# ── _oai_tool_choice_to_anthropic ────────────────────────────────────────────

class TestToolChoiceToAnthropic:
    def test_none_is_auto(self, router):
        assert router._oai_tool_choice_to_anthropic(None) == {"type": "auto"}

    def test_auto(self, router):
        assert router._oai_tool_choice_to_anthropic("auto") == {"type": "auto"}

    def test_required_is_any(self, router):
        assert router._oai_tool_choice_to_anthropic("required") == {"type": "any"}

    def test_none_string_returns_none(self, router):
        assert router._oai_tool_choice_to_anthropic("none") is None

    def test_named_function(self, router):
        tc = {"type": "function", "function": {"name": "search"}}
        assert router._oai_tool_choice_to_anthropic(tc) == {"type": "tool", "name": "search"}

    def test_named_function_without_name_falls_back_to_auto(self, router):
        tc = {"type": "function", "function": {}}
        assert router._oai_tool_choice_to_anthropic(tc) == {"type": "auto"}

    def test_unknown_value_falls_back_to_auto(self, router):
        assert router._oai_tool_choice_to_anthropic("garbage") == {"type": "auto"}


# ── _openai_to_anthropic_messages ────────────────────────────────────────────

class TestMessagesToAnthropic:
    def test_system_string_extracted_and_joined(self, router):
        msgs = [
            {"role": "system", "content": "be terse"},
            {"role": "system", "content": "be kind"},
            {"role": "user", "content": "hi"},
        ]
        system, out = router._openai_to_anthropic_messages(msgs)
        assert system == "be terse\n\nbe kind"
        assert out == [{"role": "user", "content": "hi"}]

    def test_system_block_shape_promotes_to_list(self, router):
        msgs = [
            {"role": "system", "content": [
                {"type": "text", "text": "cached preamble", "cache_control": {"type": "ephemeral"}},
            ]},
            {"role": "user", "content": "hi"},
        ]
        system, out = router._openai_to_anthropic_messages(msgs)
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_user_string_passthrough(self, router):
        system, out = router._openai_to_anthropic_messages([{"role": "user", "content": "hello"}])
        assert system is None
        assert out == [{"role": "user", "content": "hello"}]

    def test_tool_results_grouped_into_one_user_message(self, router):
        msgs = [
            {"role": "tool", "tool_call_id": "a", "content": "result-a"},
            {"role": "tool", "tool_call_id": "b", "content": "result-b"},
        ]
        _, out = router._openai_to_anthropic_messages(msgs)
        assert len(out) == 1
        assert out[0]["role"] == "user"
        blocks = out[0]["content"]
        assert [b["tool_use_id"] for b in blocks] == ["a", "b"]
        assert all(b["type"] == "tool_result" for b in blocks)

    def test_assistant_tool_calls_become_tool_use(self, router):
        msgs = [{
            "role": "assistant",
            "content": "let me check",
            "tool_calls": [{
                "id": "call_1",
                "function": {"name": "lookup", "arguments": '{"q": "x"}'},
            }],
        }]
        _, out = router._openai_to_anthropic_messages(msgs)
        blocks = out[0]["content"]
        text_block = next(b for b in blocks if b["type"] == "text")
        tool_block = next(b for b in blocks if b["type"] == "tool_use")
        assert text_block["text"] == "let me check"
        assert tool_block["name"] == "lookup"
        assert tool_block["input"] == {"q": "x"}

    def test_assistant_bad_json_arguments_wrapped(self, router):
        msgs = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c", "function": {"name": "f", "arguments": "not-json"}}],
        }]
        _, out = router._openai_to_anthropic_messages(msgs)
        tool_block = next(b for b in out[0]["content"] if b["type"] == "tool_use")
        assert tool_block["input"] == {"__raw__": "not-json"}

    def test_empty_assistant_gets_benign_text_block(self, router):
        msgs = [{"role": "assistant", "content": ""}]
        _, out = router._openai_to_anthropic_messages(msgs)
        assert out[0]["content"] == [{"type": "text", "text": ""}]

    def test_unknown_role_skipped(self, router):
        msgs = [{"role": "function", "content": "ignored"}, {"role": "user", "content": "hi"}]
        _, out = router._openai_to_anthropic_messages(msgs)
        assert out == [{"role": "user", "content": "hi"}]


# ── _anthropic_to_openai_response ─────────────────────────────────────────────

class _Block:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Usage:
    def __init__(self, input_tokens, output_tokens, cache_creation_input_tokens=0,
                 cache_read_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class _Resp:
    def __init__(self, content, stop_reason="end_turn", usage=None, id="msg_test"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or _Usage(10, 5)
        self.id = id


class TestResponseToOpenAI:
    def test_text_only_response(self, router):
        resp = _Resp([_Block(type="text", text="hello world")])
        out = router._anthropic_to_openai_response(resp, "claude-x")
        choice = out["choices"][0]
        assert choice["message"]["content"] == "hello world"
        assert "tool_calls" not in choice["message"]
        assert choice["finish_reason"] == "stop"
        assert out["model"] == "claude-x"
        assert out["usage"]["total_tokens"] == 15

    def test_tool_use_response(self, router):
        resp = _Resp(
            [_Block(type="tool_use", id="t1", name="search", input={"q": "x"})],
            stop_reason="tool_use",
        )
        out = router._anthropic_to_openai_response(resp, "claude-x")
        msg = out["choices"][0]["message"]
        # content is None when only tool calls are present.
        assert msg["content"] is None
        assert msg["tool_calls"][0]["function"]["name"] == "search"
        assert msg["tool_calls"][0]["function"]["arguments"] == '{"q": "x"}'
        assert out["choices"][0]["finish_reason"] == "tool_calls"

    def test_finish_reason_mapping(self, router):
        resp = _Resp([_Block(type="text", text="x")], stop_reason="max_tokens")
        out = router._anthropic_to_openai_response(resp, "claude-x")
        assert out["choices"][0]["finish_reason"] == "length"

    def test_cache_token_fields_surface_when_present(self, router):
        resp = _Resp(
            [_Block(type="text", text="x")],
            usage=_Usage(100, 10, cache_creation_input_tokens=20, cache_read_input_tokens=80),
        )
        out = router._anthropic_to_openai_response(resp, "claude-x")
        usage = out["usage"]
        assert usage["cache_read_input_tokens"] == 80
        assert usage["cache_creation_input_tokens"] == 20
        assert usage["prompt_tokens_details"]["cached_tokens"] == 80

    def test_no_cache_fields_when_absent(self, router):
        resp = _Resp([_Block(type="text", text="x")], usage=_Usage(5, 5))
        out = router._anthropic_to_openai_response(resp, "claude-x")
        assert "prompt_tokens_details" not in out["usage"]


# ── _select_anthropic_tier_for_model / _is_anthropic_tier ────────────────────

@pytest.fixture
def with_claude_tier(router):
    """Inject an Anthropic-backed 'claude' tier; the autouse isolation fixture
    removes it afterward."""
    router.MODELS["claude"] = {
        "litellm_model": "anthropic/claude-sonnet-4-6",
        "api_base": "https://foundry.example/anthropic",
        "api_key": "k",
        "daily_budget": 0.25,
        "max_tokens": 4096,
        "context_limit": 128000,
        "timeout_seconds": 60,
        "supports_tools": True,
    }
    return router


class TestSelectAnthropicTier:
    def test_explicit_tier_field(self, with_claude_tier):
        r = with_claude_tier
        assert r._select_anthropic_tier_for_model({"tier": "claude", "model": "x"}) == "claude"

    def test_exact_model_match(self, with_claude_tier):
        r = with_claude_tier
        assert r._select_anthropic_tier_for_model({"model": "claude"}) == "claude"

    def test_substring_claude_match(self, with_claude_tier):
        r = with_claude_tier
        assert r._select_anthropic_tier_for_model({"model": "claude-3-opus-something"}) == "claude"

    def test_non_anthropic_model_400(self, with_claude_tier):
        r = with_claude_tier
        with pytest.raises(HTTPException) as exc:
            r._select_anthropic_tier_for_model({"model": "gpt-4o-mini"})
        assert exc.value.status_code == 400

    def test_no_model_400(self, with_claude_tier):
        r = with_claude_tier
        with pytest.raises(HTTPException) as exc:
            r._select_anthropic_tier_for_model({})
        assert exc.value.status_code == 400

    def test_is_anthropic_tier_flag(self, with_claude_tier):
        r = with_claude_tier
        assert r._is_anthropic_tier("claude") is True
        assert r._is_anthropic_tier("gpt4o-mini") is False
