"""Agent-side peer identity — the other half of docs/design/memory-system.md §18.

A5 gave the human principal one canonical peer. This covers the expected *agent*
peer set and the alias map applied at the write choke point: known aliases are
rewritten as they arrive, and a peer that is neither the canonical user nor a
declared agent is reported rather than silently accumulating.

The write always proceeds. An unexpected peer is a config smell, not an attack,
and rejecting it would lose the memory while the misconfiguration is still in
place. Offline: no DB, no HTTP.
"""

import asyncio

import pytest

from governor import identity
from governor.memory import admission


# ── roster declaration ──────────────────────────────────────────────────────

def test_roster_unset_is_permissive(monkeypatch):
    # A deployment that has not declared its agents must not have every write
    # flagged — we cannot call a peer unexpected without knowing what was
    # expected.
    monkeypatch.delenv("HONCHO_AGENT_PEER_IDS", raising=False)
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "user")
    assert identity.agent_peer_ids() == set()
    assert identity.classify_peer("whoever") == identity.UNDECLARED


def test_roster_parsed_and_whitespace_tolerated(monkeypatch):
    monkeypatch.setenv("HONCHO_AGENT_PEER_IDS", " researcher , engineer ,, qa ")
    assert identity.agent_peer_ids() == {"researcher", "engineer", "qa"}


def test_classification_of_each_peer_kind(monkeypatch):
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "principal-42")
    monkeypatch.setenv("HONCHO_AGENT_PEER_IDS", "researcher,engineer")
    assert identity.classify_peer("principal-42") == identity.CANONICAL_USER
    assert identity.classify_peer("researcher") == identity.KNOWN_AGENT
    assert identity.classify_peer("user-bot-telegram-dm-99") == identity.UNEXPECTED


def test_canonical_user_wins_over_roster(monkeypatch):
    # If someone lists the user peer in the agent roster too, the human
    # classification takes precedence — it is the more specific claim.
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "user")
    monkeypatch.setenv("HONCHO_AGENT_PEER_IDS", "user,researcher")
    assert identity.classify_peer("user") == identity.CANONICAL_USER


# ── alias map ───────────────────────────────────────────────────────────────

def test_alias_rewrites_to_canonical(monkeypatch):
    monkeypatch.setenv("HONCHO_PEER_ALIASES", "operator=user,legacy-admin=user")
    assert identity.resolve_peer("operator") == "user"
    assert identity.resolve_peer("legacy-admin") == "user"


def test_unaliased_peer_passes_through(monkeypatch):
    monkeypatch.setenv("HONCHO_PEER_ALIASES", "operator=user")
    assert identity.resolve_peer("researcher") == "researcher"


def test_malformed_alias_config_is_skipped_not_fatal(monkeypatch):
    # A broken alias map must never take the write path down: this runs at the
    # admission choke point, and failing closed on our own config would lose
    # every write for a typo.
    monkeypatch.setenv(
        "HONCHO_PEER_ALIASES", "operator=user,garbage,=user,alias=,  ,x=y"
    )
    assert identity.peer_aliases() == {"operator": "user", "x": "y"}


def test_alias_chain_resolves_one_hop_only(monkeypatch):
    # a->b->c stops at b. Deliberate: one hop cannot loop, so a cyclic config
    # (a=b,b=a) cannot hang admission.
    monkeypatch.setenv("HONCHO_PEER_ALIASES", "a=b,b=c")
    assert identity.resolve_peer("a") == "b"


def test_cyclic_alias_config_terminates(monkeypatch):
    monkeypatch.setenv("HONCHO_PEER_ALIASES", "a=b,b=a")
    assert identity.resolve_peer("a") == "b"
    assert identity.resolve_peer("b") == "a"


def test_self_mapping_is_dropped(monkeypatch):
    monkeypatch.setenv("HONCHO_PEER_ALIASES", "user=user,operator=user")
    assert identity.peer_aliases() == {"operator": "user"}


def test_resolve_and_classify_reports_aliasing(monkeypatch):
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "user")
    monkeypatch.setenv("HONCHO_AGENT_PEER_IDS", "researcher")
    monkeypatch.setenv("HONCHO_PEER_ALIASES", "operator=user")
    assert identity.resolve_and_classify("operator") == (
        "user",
        identity.CANONICAL_USER,
        True,
    )
    assert identity.resolve_and_classify("researcher") == (
        "researcher",
        identity.KNOWN_AGENT,
        False,
    )


# ── admission wiring ────────────────────────────────────────────────────────

@pytest.fixture
def captured_events(monkeypatch):
    events: list[tuple[str, str, dict]] = []

    async def _emit(event_type, actor_peer, payload, **kwargs):
        events.append((event_type, actor_peer, payload))

    monkeypatch.setattr(admission.db, "emit_event", _emit)
    return events


def _request(**overrides):
    fields = {
        "content": "the user's dog is named Biscuit",
        "workspace_name": "ws",
        "observer": "researcher",
        "observed": "user",
        "created_by_peer": "researcher",
    }
    fields.update(overrides)
    return admission.AdmitRequest(**fields)


def test_admission_rewrites_aliases_in_place(monkeypatch, captured_events):
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "user")
    monkeypatch.setenv("HONCHO_AGENT_PEER_IDS", "researcher")
    monkeypatch.setenv("HONCHO_PEER_ALIASES", "operator=user,legacy-researcher=researcher")
    req = _request(observed="operator", observer="legacy-researcher")

    asyncio.run(admission._resolve_identity(req))

    # The alias must never reach storage, dedup, or a downstream event payload.
    assert (req.observed, req.observer) == ("user", "researcher")


def test_admission_reports_unexpected_peer_without_rejecting(
    monkeypatch, captured_events
):
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "user")
    monkeypatch.setenv("HONCHO_AGENT_PEER_IDS", "researcher")
    req = _request(observed="user-bot-telegram-dm-99")

    asyncio.run(admission._resolve_identity(req))

    # Reported...
    identity_events = [e for e in captured_events if e[0] == "memory_identity"]
    assert len(identity_events) == 1
    payload = identity_events[0][2]
    assert payload["peer"] == "user-bot-telegram-dm-99"
    assert payload["classification"] == identity.UNEXPECTED
    assert payload["field"] == "observed"
    # ...and left alone. Losing the memory is worse than storing it on a peer
    # the operator now knows about.
    assert req.observed == "user-bot-telegram-dm-99"


def test_expected_peers_emit_nothing(monkeypatch, captured_events):
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "user")
    monkeypatch.setenv("HONCHO_AGENT_PEER_IDS", "researcher")
    asyncio.run(admission._resolve_identity(_request()))
    assert [e for e in captured_events if e[0] == "memory_identity"] == []


def test_undeclared_roster_emits_nothing(monkeypatch, captured_events):
    # The permissive path: no roster declared, so no peer can be called a stray.
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "user")
    monkeypatch.delenv("HONCHO_AGENT_PEER_IDS", raising=False)
    asyncio.run(admission._resolve_identity(_request(observed="anything-at-all")))
    assert [e for e in captured_events if e[0] == "memory_identity"] == []


def test_agent_self_lesson_is_not_a_stray(monkeypatch, captured_events):
    # §18: the watchdog's self-lessons deliberately observe the AGENT slug. A
    # declared agent writing about itself is expected, not fragmentation.
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "user")
    monkeypatch.setenv("HONCHO_AGENT_PEER_IDS", "watchdog")
    req = _request(observer="watchdog", observed="watchdog", created_by_peer="watchdog")
    asyncio.run(admission._resolve_identity(req))
    assert [e for e in captured_events if e[0] == "memory_identity"] == []
