"""Peer identity resolution at the write choke point.

A5 (v1.8) gave the human principal one canonical peer id. This is the other
half of docs/design/memory-system.md §18: the *agent* side of the expected peer
set, plus the alias map that rewrites known strays as they arrive instead of
letting them accumulate.

The failure mode is the same one §18 documents for the user peer, one level
over: writers drift, each locally reasonable, and the reader queries exactly one
peer. Facts land under peers nobody asks about and recall goes quiet with zero
errors. A canonical input fixes the drift that already happened; it cannot stop
a *new* writer — the next gateway, an imported history, a vendored tool with its
own naming scheme — from minting peers before anyone threads the variable. This
module is the backstop for that.

Two deploy-time inputs, both optional, both defaulting to permissive:

  HONCHO_AGENT_PEER_IDS   comma-separated agent slugs that are legitimate peers
                          alongside the canonical user peer. Unset means "every
                          peer is expected" — the check reports nothing rather
                          than flooding a deployment that has not declared its
                          roster.
  HONCHO_PEER_ALIASES     comma-separated alias=canonical pairs, applied to
                          `observer` and `observed` at admission.

Deliberately NOT a rejection path. An unexpected peer is a config smell, not an
attack, and refusing the write loses the memory while the misconfiguration is
still in place — strictly worse than storing it and telling the operator. The
classification is reported; the write proceeds.
"""

from __future__ import annotations

import os

from . import config

# Classifications for a resolved peer id.
CANONICAL_USER = "canonical_user"
KNOWN_AGENT = "known_agent"
UNEXPECTED = "unexpected"
# Returned when no roster is declared: we cannot call any peer unexpected
# without knowing what was expected.
UNDECLARED = "undeclared_roster"


def agent_peer_ids() -> set[str]:
    """Declared agent slugs. Empty set = roster not declared (permissive)."""
    raw = os.environ.get("HONCHO_AGENT_PEER_IDS", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def peer_aliases() -> dict[str, str]:
    """alias -> canonical peer id.

    Parsed leniently: a malformed pair is skipped rather than crashing the write
    path. A misconfigured alias map must not take admission down — the whole
    point is to be a safety net, and a safety net that fails closed on its own
    config is a liability at the choke point.
    """
    raw = os.environ.get("HONCHO_PEER_ALIASES", "")
    out: dict[str, str] = {}
    for pair in raw.split(","):
        alias, sep, canonical = pair.partition("=")
        alias, canonical = alias.strip(), canonical.strip()
        if not sep or not alias or not canonical:
            continue
        # Self-mapping is a no-op; an alias chain is not resolved transitively
        # (one hop only) so a cycle in the config cannot hang the write path.
        if alias == canonical:
            continue
        out[alias] = canonical
    return out


def resolve_peer(peer: str) -> str:
    """Rewrite a known alias to its canonical peer. One hop, never recursive."""
    if not peer:
        return peer
    return peer_aliases().get(peer, peer)


def classify_peer(peer: str) -> str:
    """Classify an already-resolved peer against the expected set."""
    if peer == config.user_peer_id():
        return CANONICAL_USER
    roster = agent_peer_ids()
    if not roster:
        return UNDECLARED
    if peer in roster:
        return KNOWN_AGENT
    return UNEXPECTED


def resolve_and_classify(peer: str) -> tuple[str, str, bool]:
    """Return (resolved_peer, classification, was_aliased)."""
    resolved = resolve_peer(peer)
    return resolved, classify_peer(resolved), resolved != peer
