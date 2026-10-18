"""Per-subject export + two-step erase (offline).

Handlers are called directly against a FakePool (same pattern as
test_memory_inspector.py): the tests assert the generated SQL is scoped by
workspace AND subject hash, that the dry run performs no write, that a wrong
request_id is refused, and that a confirmed erase blanks content, tombstones,
and emits a counts-only event.
"""

import asyncio

import pytest
from fastapi import HTTPException

from governor import main as governor_main
from governor import subject


class _Rec(dict):
    """asyncpg.Record look-alike: item access by column name."""


class _FakePool:
    def __init__(self, fetch=None, fetchrow=None, fetchval=None):
        self._fetch = list(fetch or [])
        self._fetchrow = list(fetchrow or [])
        self._fetchval = list(fetchval or [])
        self.fetch_calls, self.fetchrow_calls, self.fetchval_calls, self.execute_calls = [], [], [], []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self._fetch.pop(0) if self._fetch else []

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        return self._fetchrow.pop(0) if self._fetchrow else None

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        return self._fetchval.pop(0) if self._fetchval else None

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))


def _patch(monkeypatch, pool):
    async def _pool():
        return pool
    monkeypatch.setattr(governor_main.db, "pool", _pool)
    events = []

    async def _emit(event_type, actor_peer, payload, **kw):
        events.append((event_type, actor_peer, payload))
    monkeypatch.setattr(governor_main.db, "emit_event", _emit)
    return events


H = "a" * 64
DOC = _Rec(id="d1", content="likes tea", memory_class="user_preference", memory_scope_kind="user",
           memory_scope_id="u", source_type="observed", verification_state="unverified",
           created_by_peer="coder", created_at="2026-10-01T00:00:00+00:00", expires_at=None)
SESS = _Rec(id=7, session_id="s1", content="asked about tea", created_by_peer="coder", expires_at=None)


# ── pure subject module ──────────────────────────────────────────────────────

def test_hash_subject_is_peppered_deterministic_and_case_insensitive(monkeypatch):
    monkeypatch.setenv("SUBJECT_HASH_PEPPER", "pepper-1")
    a = subject.hash_subject("teams:ABC")
    assert subject.is_subject_hash(a)
    assert a == subject.hash_subject(" teams:abc ")
    monkeypatch.setenv("SUBJECT_HASH_PEPPER", "pepper-2")
    assert a != subject.hash_subject("teams:abc")
    with pytest.raises(ValueError):
        subject.hash_subject("   ")


def test_raw_subject_never_appears_in_hash():
    h = subject.hash_subject("slack:T1/U42", pepper_value="p")
    assert "U42" not in h and len(h) == 64


def test_erase_request_id_binds_subject_workspace_and_day(monkeypatch):
    monkeypatch.setenv("SUBJECT_HASH_PEPPER", "p")
    a = subject.erase_request_id(H, "ws", "2026-10-18")
    assert a == subject.erase_request_id(H, "ws", "2026-10-18")
    assert a != subject.erase_request_id(H, "ws", "2026-10-19")
    assert a != subject.erase_request_id(H, "other", "2026-10-18")


def test_build_export_bundle_excludes_internal_metadata():
    b = subject.build_export_bundle(H, "ws", [DOC], [SESS])
    assert b["counts"] == {"documents": 1, "session_memories": 1}
    assert b["documents"][0]["content"] == "likes tea"
    assert "internal_metadata" not in b["documents"][0]
    assert b["session_memories"][0]["session_id"] == "s1"


# ── routes ───────────────────────────────────────────────────────────────────

def test_export_is_scoped_by_workspace_and_subject(monkeypatch):
    pool = _FakePool(fetch=[[DOC], [SESS]])
    _patch(monkeypatch, pool)
    out = asyncio.run(governor_main.subject_export(H, workspace_name="ws", limit=10))
    assert out["counts"] == {"documents": 1, "session_memories": 1}
    doc_sql, doc_args = pool.fetch_calls[0]
    assert "internal_metadata->>'subject_hash' = $2" in doc_sql and "workspace_name = $1" in doc_sql
    assert "deleted_at IS NULL" in doc_sql
    assert doc_args == ("ws", H, 10)
    sess_sql, sess_args = pool.fetch_calls[1]
    assert "session_memory" in sess_sql and "peer_id = $2" in sess_sql
    assert sess_args == ("ws", H, 10)
    assert pool.execute_calls == [] and pool.fetchval_calls == []


def test_export_rejects_malformed_hash(monkeypatch):
    pool = _FakePool()
    _patch(monkeypatch, pool)
    with pytest.raises(HTTPException) as e:
        asyncio.run(governor_main.subject_export("not-a-hash", workspace_name="ws"))
    assert e.value.status_code == 422
    assert pool.fetch_calls == []


def test_erase_dry_run_writes_nothing_and_returns_request_id(monkeypatch):
    monkeypatch.setenv("SUBJECT_HASH_PEPPER", "p")
    pool = _FakePool(fetchrow=[_Rec(documents=3, session_memories=1)])
    events = _patch(monkeypatch, pool)
    body = governor_main.SubjectEraseBody(workspace_name="ws")
    out = asyncio.run(governor_main.subject_erase(H, body))
    assert out["status"] == "dry_run" and out["would_erase"] == {"documents": 3, "session_memories": 1}
    assert len(out["request_id"]) == 32
    assert pool.fetchval_calls == [] and pool.execute_calls == [] and events == []
    sql, args = pool.fetchrow_calls[0]
    assert "internal_metadata->>'subject_hash' = $2" in sql and args == ("ws", H)


def test_erase_with_wrong_request_id_is_refused(monkeypatch):
    monkeypatch.setenv("SUBJECT_HASH_PEPPER", "p")
    pool = _FakePool(fetchrow=[_Rec(documents=1, session_memories=0)])
    events = _patch(monkeypatch, pool)
    body = governor_main.SubjectEraseBody(workspace_name="ws", confirm=True, request_id="nope")
    with pytest.raises(HTTPException) as e:
        asyncio.run(governor_main.subject_erase(H, body))
    assert e.value.status_code == 409
    assert pool.fetchval_calls == [] and events == []


def test_confirmed_erase_blanks_tombstones_and_emits_counts_only(monkeypatch):
    monkeypatch.setenv("SUBJECT_HASH_PEPPER", "p")
    pool = _FakePool(fetchrow=[_Rec(documents=2, session_memories=1)], fetchval=[2, 1])
    events = _patch(monkeypatch, pool)
    dry = asyncio.run(governor_main.subject_erase(H, governor_main.SubjectEraseBody(workspace_name="ws")))
    pool._fetchrow = [_Rec(documents=2, session_memories=1)]
    body = governor_main.SubjectEraseBody(workspace_name="ws", confirm=True, request_id=dry["request_id"], actor="op")
    out = asyncio.run(governor_main.subject_erase(H, body))
    assert out == {"status": "erased", "request_id": dry["request_id"], "erased": {"documents": 2, "session_memories": 1}}
    doc_sql, doc_args = pool.fetchval_calls[0]
    assert "UPDATE documents" in doc_sql and "content = ''" in doc_sql and "deleted_at = now()" in doc_sql
    assert "internal_metadata->>'subject_hash' = $2" in doc_sql and doc_args == ("ws", H)
    sess_sql, sess_args = pool.fetchval_calls[1]
    assert "DELETE FROM session_memory" in sess_sql and "peer_id = $2" in sess_sql and sess_args == ("ws", H)
    assert events == [("subject_erased", "op", {"subject_hash": H, "workspace": "ws",
                                                "request_id": dry["request_id"],
                                                "erased": {"documents": 2, "session_memories": 1}})]


def test_two_subjects_in_one_workspace_are_isolated_by_the_hash_filter(monkeypatch):
    """The isolation property: every subject query carries the hash as a
    positional arg, so subject B's rows can never match subject A's call."""
    other = "b" * 64
    pool = _FakePool(fetch=[[DOC], []], fetchrow=[_Rec(documents=0, session_memories=0)])
    _patch(monkeypatch, pool)
    asyncio.run(governor_main.subject_export(H, workspace_name="ws"))
    asyncio.run(governor_main.subject_erase(other, governor_main.SubjectEraseBody(workspace_name="ws")))
    assert pool.fetch_calls[0][1][1] == H
    assert pool.fetchrow_calls[0][1][1] == other


# ── admit tagging ────────────────────────────────────────────────────────────

def test_admit_hashes_subject_server_side_and_never_forwards_raw(monkeypatch):
    monkeypatch.setenv("SUBJECT_HASH_PEPPER", "p")
    captured = {}

    async def fake_admit(req):
        captured["req"] = req

        class R:
            __dict__ = {"ok": True}
        return R()

    monkeypatch.setattr(governor_main.admission, "admit", fake_admit)
    body = governor_main.AdmitBody(content="likes tea", workspace_name="ws", observer="coder",
                                   created_by_peer="coder", subject="teams:USER-1")
    asyncio.run(governor_main.admit(body))
    req = captured["req"]
    assert req.subject_hash == subject.hash_subject("teams:USER-1")
    assert not hasattr(req, "subject")
    body2 = governor_main.AdmitBody(content="x", workspace_name="ws", observer="coder", created_by_peer="coder")
    asyncio.run(governor_main.admit(body2))
    assert captured["req"].subject_hash is None
