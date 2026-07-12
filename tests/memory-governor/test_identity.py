"""A5 — canonical user peer. One deploy-time input (HONCHO_USER_PEER_ID) names
the peer that represents the human principal; the governor's /admit must
default `observed` to it when a writer omits the field, and the fallback when
the input is unset must be "user" — the SAME fallback every other component
(compose, Terraform, pc-memory/pc-honcho) uses. Divergent per-component
defaults are the identity-fragmentation failure mode documented in
docs/design/memory-system.md §18. Offline: no DB, no HTTP."""

from governor import config
from governor import main as governor_main


# ── config.user_peer_id(): the single resolution point ──────────────────────

def test_default_is_user_when_env_unset(monkeypatch):
    monkeypatch.delenv("HONCHO_USER_PEER_ID", raising=False)
    assert config.user_peer_id() == "user"
    assert config.DEFAULT_USER_PEER_ID == "user"


def test_env_value_wins(monkeypatch):
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "principal-42")
    assert config.user_peer_id() == "principal-42"


def test_blank_env_falls_back_to_default(monkeypatch):
    # An empty/whitespace value must NOT become a real (empty) peer id — that
    # would be a third accidental identity. Fall through to the canonical default.
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "   ")
    assert config.user_peer_id() == "user"


def test_read_at_call_time_not_import_time(monkeypatch):
    # Long-lived processes and tests must see env changes without a module
    # reload — the default is a function, not a frozen module constant.
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "first")
    assert config.user_peer_id() == "first"
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "second")
    assert config.user_peer_id() == "second"


# ── AdmitBody: a writer that omits `observed` lands on the canonical peer ───

def _admit_body(**overrides):
    fields = {
        "content": "the user's dog is named Biscuit",
        "workspace_name": "ws",
        "observer": "researcher",
        "created_by_peer": "researcher",
    }
    fields.update(overrides)
    return governor_main.AdmitBody(**fields)


def test_admit_observed_defaults_to_canonical_peer(monkeypatch):
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "principal-42")
    assert _admit_body().observed == "principal-42"


def test_admit_observed_defaults_to_user_when_unset(monkeypatch):
    monkeypatch.delenv("HONCHO_USER_PEER_ID", raising=False)
    assert _admit_body().observed == "user"


def test_admit_default_tracks_env_per_request(monkeypatch):
    # default_factory, not a literal frozen at import: two requests under
    # different env values resolve independently.
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "alpha")
    first = _admit_body()
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "beta")
    second = _admit_body()
    assert (first.observed, second.observed) == ("alpha", "beta")


def test_admit_explicit_observed_still_wins(monkeypatch):
    # Agent self-lessons (watchdog) deliberately set observed to the agent slug;
    # the canonical default must never override an explicit value.
    monkeypatch.setenv("HONCHO_USER_PEER_ID", "principal-42")
    assert _admit_body(observed="researcher").observed == "researcher"
