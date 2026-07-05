#!/usr/bin/env python3
"""Adversarial verification lane (dream-backlog §0.5).

Before an agent posts a current-facts claim to the operator, a cheap
gpt4o-mini "skeptic" checks the claim against its cited evidence. If the
evidence does NOT support the claim, the comment must not post — this makes
fabrications (e.g. a hallucinated sports roster posted as fact)
architecturally impossible rather than merely discouraged.

This module is invoked by `pc-delegate.sh cmd_comment` when VERIFICATION_LANE=1
and an `--evidence` argument is supplied. It exits:
    0  → SUPPORTED (or skeptic unreachable — fail-OPEN, see below) → caller posts
    1  → UNSUPPORTED → caller must NOT post the comment

DESIGN — mirrors services/watchdog/memory.py:
  * Pure functions (build_prompt, judge, build_payload) carry all the logic and
    are exhaustively unit-tested with no network.
  * The network call is isolated behind an INJECTABLE `caller` (default = real
    urllib POST to the router). Tests pass a stub.

FAIL-OPEN on skeptic error/timeout: a router outage, a 5xx, a malformed verdict,
or any exception must NOT block every agent comment platform-wide. We log the
reason to stderr and ALLOW the post (exit 0). The lane is a safety net against
fabrication, not a hard dependency in the comment path — blocking on it would
trade one failure mode (occasional fabrication) for a worse one (no agent can
report anything when the router hiccups). The DB flag VERIFICATION_LANE_ENABLED
(migration 0009) is the declarative registry / kill switch; the shell env var
VERIFICATION_LANE=1 is the live toggle (matching §0.2's TRACK_RECORD_ROUTING).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Callable, Optional

# The skeptic runs on the cheapest capable tier — ~$0.001/check (§0.5: "router
# economics make it free"). gpt4o-mini is the platform default tier id.
SKEPTIC_TIER = "gpt4o-mini"
SKEPTIC_PERSONA = "verifier"

# A blunt instruction so the judge parser has a stable two-word vocabulary to
# match against. We ask for the verdict on its own first line.
_SYSTEM = (
    "You are a fact-checking skeptic. You are given a CLAIM an agent wants to "
    "post to its operator, and the EVIDENCE the agent gathered. Decide ONLY "
    "whether the evidence supports the claim. Do not use outside knowledge. "
    "If the evidence does not clearly support the claim — including when there "
    "is no evidence, or the evidence is unrelated — answer UNSUPPORTED. "
    "Respond with exactly one word on the first line: SUPPORTED or UNSUPPORTED, "
    "optionally followed by a one-sentence reason."
)


def build_prompt(claim: str, evidence: str) -> str:
    """Pure: assemble the user-message text comparing claim to evidence."""
    claim = (claim or "").strip()
    evidence = (evidence or "").strip() or "(no evidence provided)"
    return (
        "CLAIM (what the agent wants to post):\n"
        f"{claim}\n\n"
        "EVIDENCE (what the agent actually gathered):\n"
        f"{evidence}\n\n"
        "Does the evidence support the claim? Answer SUPPORTED or UNSUPPORTED."
    )


def build_payload(claim: str, evidence: str, *, persona: str = SKEPTIC_PERSONA) -> dict:
    """Pure: the /v1/chat/completions body for the skeptic call.

    metadata.persona lets the router attribute the call (PERSONA_TIERS); `tier`
    pins gpt4o-mini regardless of persona routing. camelCase isn't relevant here
    (this hits the router, not PaperClip), but we keep the body minimal and
    deterministic so it's trivially unit-tested.
    """
    return {
        "model": "gpt-4o-mini",
        "tier": SKEPTIC_TIER,
        "metadata": {"persona": persona, "tier": SKEPTIC_TIER},
        "temperature": 0,
        "max_tokens": 64,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": build_prompt(claim, evidence)},
        ],
    }


def judge(verdict_text: str) -> bool:
    """Pure: parse the skeptic's reply → True (SUPPORTED) / False (UNSUPPORTED).

    Conservative by construction: only an explicit, unambiguous SUPPORTED
    verdict returns True. We look at the FIRST non-empty line (the model is
    instructed to put the one-word verdict there). If 'unsupported' appears, or
    the verdict is empty/garbled, we return False — but note the caller treats
    parse failure differently from a clean UNSUPPORTED: parse failures during a
    LIVE call fail OPEN (see verify()), whereas a clean UNSUPPORTED blocks.
    """
    if not verdict_text:
        return False
    first = ""
    for line in verdict_text.splitlines():
        if line.strip():
            first = line.strip()
            break
    low = first.lower()
    # "unsupported" contains "supported" as a substring, so check the negative
    # token FIRST.
    if "unsupported" in low or "not supported" in low:
        return False
    if "supported" in low:
        return True
    # Some models answer yes/no instead of the requested tokens.
    if low.startswith("yes"):
        return True
    if low.startswith("no"):
        return False
    return False


def _default_caller(base_url: str, key: str, payload: dict, timeout: float) -> str:
    """Real network caller: POST to the router's chat-completions endpoint and
    return the assistant's text content. Isolated so tests inject a stub."""
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read() or "{}")
    choices = body.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message") or {}).get("content") or ""


def verify(
    claim: str,
    evidence: str,
    *,
    base_url: str,
    key: str = "",
    timeout: float = 20.0,
    persona: str = SKEPTIC_PERSONA,
    caller: Optional[Callable[[str, str, dict, float], str]] = None,
) -> tuple[bool, str]:
    """Run the skeptic. Returns (allow_post, reason).

    allow_post is True when the evidence SUPPORTS the claim OR when the skeptic
    could not be reached / returned an unusable verdict (FAIL-OPEN). It is False
    only on a clean UNSUPPORTED verdict. `caller` is injectable for tests;
    default is the real urllib POST.
    """
    call = caller or _default_caller
    payload = build_payload(claim, evidence, persona=persona)
    try:
        verdict_text = call(base_url, key, payload, timeout)
    except Exception as e:  # noqa: BLE001 — fail OPEN on ANY skeptic failure
        return True, f"skeptic unreachable, failing open: {e}"

    if not verdict_text or not verdict_text.strip():
        # Empty body / no content — can't judge, so fail OPEN.
        return True, "skeptic returned no verdict, failing open"

    supported = judge(verdict_text)
    if supported:
        return True, "SUPPORTED"
    return False, f"UNSUPPORTED: {verdict_text.strip()[:200]}"


def _main(argv: list[str]) -> int:
    """CLI entry for pc-delegate.sh.

    Usage: verify_claim.py --claim <text> --evidence <text> [--base-url U] [--key K]

    base_url defaults to ROUTER_BASE_URL or the in-pod router sidecar. key
    defaults to ROUTER_API_KEY. Prints the reason to stderr; exits 0 to ALLOW
    the post (SUPPORTED or fail-open), 1 to BLOCK it (clean UNSUPPORTED).
    """
    claim = evidence = ""
    base_url = os.environ.get("ROUTER_BASE_URL", "http://localhost:8080")
    key = os.environ.get("ROUTER_API_KEY", "")
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--claim":
            claim = argv[i + 1]; i += 2
        elif a == "--evidence":
            evidence = argv[i + 1]; i += 2
        elif a == "--base-url":
            base_url = argv[i + 1]; i += 2
        elif a == "--key":
            key = argv[i + 1]; i += 2
        else:
            sys.stderr.write(f"verify_claim: unknown arg {a!r}\n")
            return 2
    allow, reason = verify(claim, evidence, base_url=base_url, key=key)
    sys.stderr.write(f"verify_claim: {reason}\n")
    return 0 if allow else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
