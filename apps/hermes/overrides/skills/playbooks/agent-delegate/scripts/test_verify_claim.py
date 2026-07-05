"""Offline tests for the adversarial verification lane (dream-backlog §0.5).

Pure judge + prompt + payload builders, plus verify() with a STUBBED caller —
no network, no LLM, no DB. Mirrors the injectable-caller pattern in
services/watchdog/tests/test_memory.py.

Run:  python3 -m pytest -q apps/hermes/overrides/skills/playbooks/agent-delegate/scripts/test_verify_claim.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_claim as vc  # noqa: E402


# ── judge: pure verdict parser ───────────────────────────────────────────────

class TestJudge:
    def test_supported_one_word(self):
        assert vc.judge("SUPPORTED") is True

    def test_unsupported_one_word(self):
        assert vc.judge("UNSUPPORTED") is False

    def test_unsupported_beats_supported_substring(self):
        # "unsupported" contains "supported"; the negative token must win.
        assert vc.judge("UNSUPPORTED — the evidence is unrelated") is False

    def test_supported_with_trailing_reason(self):
        assert vc.judge("SUPPORTED. The article confirms the score.") is True

    def test_case_insensitive(self):
        assert vc.judge("supported") is True
        assert vc.judge("Unsupported") is False

    def test_verdict_on_first_nonempty_line(self):
        assert vc.judge("\n\n  SUPPORTED\nsome detail below") is True

    def test_not_supported_phrase(self):
        assert vc.judge("The evidence is not supported by the claim") is False

    def test_yes_no_fallback(self):
        assert vc.judge("yes") is True
        assert vc.judge("no") is False

    def test_empty_is_unsupported(self):
        assert vc.judge("") is False

    def test_garbled_defaults_unsupported(self):
        # Conservative: anything we can't classify is treated as not-supported
        # (verify() upgrades unreachable/empty to fail-open separately).
        assert vc.judge("maybe? unclear") is False


# ── build_prompt / build_payload: pure builders ──────────────────────────────

class TestPromptAndPayload:
    def test_prompt_contains_claim_and_evidence(self):
        p = vc.build_prompt("Sky is blue", "Photo shows a blue sky")
        assert "Sky is blue" in p
        assert "Photo shows a blue sky" in p

    def test_prompt_handles_empty_evidence(self):
        p = vc.build_prompt("Sky is blue", "")
        assert "(no evidence provided)" in p

    def test_payload_pins_cheap_tier(self):
        body = vc.build_payload("c", "e")
        assert body["tier"] == "gpt4o-mini"
        assert body["metadata"]["tier"] == "gpt4o-mini"

    def test_payload_sets_persona_metadata(self):
        body = vc.build_payload("c", "e", persona="verifier")
        assert body["metadata"]["persona"] == "verifier"

    def test_payload_messages_shape(self):
        body = vc.build_payload("c", "e")
        roles = [m["role"] for m in body["messages"]]
        assert roles == ["system", "user"]

    def test_payload_deterministic_temperature(self):
        assert vc.build_payload("c", "e")["temperature"] == 0


# ── verify: stubbed caller, both verdicts + fail-open ────────────────────────

class TestVerify:
    def test_supported_verdict_allows(self):
        def stub(base_url, key, payload, timeout):
            return "SUPPORTED"

        allow, reason = vc.verify("claim", "evidence", base_url="http://r", caller=stub)
        assert allow is True
        assert reason == "SUPPORTED"

    def test_unsupported_verdict_blocks(self):
        def stub(base_url, key, payload, timeout):
            return "UNSUPPORTED — evidence does not mention the claim"

        allow, reason = vc.verify("claim", "evidence", base_url="http://r", caller=stub)
        assert allow is False
        assert "UNSUPPORTED" in reason

    def test_caller_receives_built_payload(self):
        seen = {}

        def stub(base_url, key, payload, timeout):
            seen["base_url"] = base_url
            seen["key"] = key
            seen["payload"] = payload
            return "SUPPORTED"

        vc.verify("the claim", "the evidence", base_url="http://router", key="k", caller=stub)
        assert seen["base_url"] == "http://router"
        assert seen["key"] == "k"
        assert seen["payload"]["tier"] == "gpt4o-mini"
        assert "the claim" in seen["payload"]["messages"][1]["content"]

    def test_caller_exception_fails_open(self):
        def boom(base_url, key, payload, timeout):
            raise RuntimeError("router 502")

        allow, reason = vc.verify("claim", "evidence", base_url="http://r", caller=boom)
        assert allow is True  # FAIL-OPEN — outage must not block all comments
        assert "failing open" in reason

    def test_empty_verdict_fails_open(self):
        def stub(base_url, key, payload, timeout):
            return ""

        allow, reason = vc.verify("claim", "evidence", base_url="http://r", caller=stub)
        assert allow is True
        assert "failing open" in reason

    def test_timeout_exception_fails_open(self):
        import socket

        def slow(base_url, key, payload, timeout):
            raise socket.timeout("timed out")

        allow, reason = vc.verify("claim", "evidence", base_url="http://r", caller=slow)
        assert allow is True


# ── _main: CLI exit-code contract ────────────────────────────────────────────

class TestMainExitCodes:
    def test_main_supported_exits_zero(self, monkeypatch):
        monkeypatch.setattr(vc, "_default_caller",
                            lambda b, k, p, t: "SUPPORTED")
        rc = vc._main(["--claim", "c", "--evidence", "e", "--base-url", "http://r"])
        assert rc == 0

    def test_main_unsupported_exits_one(self, monkeypatch):
        monkeypatch.setattr(vc, "_default_caller",
                            lambda b, k, p, t: "UNSUPPORTED")
        rc = vc._main(["--claim", "c", "--evidence", "e", "--base-url", "http://r"])
        assert rc == 1

    def test_main_network_error_exits_zero_fail_open(self, monkeypatch):
        def boom(b, k, p, t):
            raise RuntimeError("down")

        monkeypatch.setattr(vc, "_default_caller", boom)
        rc = vc._main(["--claim", "c", "--evidence", "e", "--base-url", "http://r"])
        assert rc == 0

    def test_main_unknown_arg_exits_two(self):
        rc = vc._main(["--bogus", "x"])
        assert rc == 2
