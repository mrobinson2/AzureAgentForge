"""Offline contract checks on the governed-memory migration SQL. No DB — these
pin that the overlay covers exactly the columns/tables the governor code reads,
so enabling MEMORY_PLANNER_ENABLED can't 500 on a missing column, and that every
statement stays idempotent (IF NOT EXISTS)."""

import pathlib
import re

_MIG = pathlib.Path(__file__).resolve().parents[2] / "services" / "memory-governor" / "migrations"
_SQL_0001 = (_MIG / "0001_governed_memory_overlay.sql").read_text()
_SQL_0002 = (_MIG / "0002_governed_memory_full_overlay.sql").read_text()
_BOTH = _SQL_0001 + "\n" + _SQL_0002

# Columns the retrieval planner / admission / scoring read off `documents`.
_PLANNER_DOC_COLUMNS = [
    "memory_class", "verification_state", "memory_scope_kind", "memory_scope_id",
    "source_type", "expires_at", "half_life_days", "last_confirmed_at",
    "usage_success_count", "contradiction_count", "is_always_on_candidate",
    "superseded_at", "confidence_score", "review_note", "planner_hint",
    "last_accessed_at", "reviewed_at",
]

_FLAGS = [
    "AGENT_EVENTS_ENABLED", "MEMORY_CLASSES_ENABLED", "MEMORY_PLANNER_ENABLED",
    "MEMORY_SESSION_SEPARATION_ENABLED", "MEMORY_TTL_SWEEPER_ENABLED",
    "MEMORY_VECTOR_RETRIEVAL_ENABLED", "MEMORY_CONTRADICTION_SWEEP_ENABLED",
    "SKILL_AUTOGEN_ENABLED",
]


class TestOverlayCompleteness:
    def test_every_planner_doc_column_is_added(self):
        for col in _PLANNER_DOC_COLUMNS:
            assert re.search(rf"ADD COLUMN IF NOT EXISTS {col}\b", _BOTH), \
                f"documents.{col} is read by the governor but never added"

    def test_superseded_at_is_timestamptz(self):
        # The planner filters `superseded_at IS NULL`; it must be a timestamp,
        # not the 0001 `superseded_by` text pointer.
        assert re.search(r"ADD COLUMN IF NOT EXISTS superseded_at\s+timestamptz", _SQL_0002)

    def test_owned_tables_created(self):
        assert "CREATE TABLE IF NOT EXISTS session_memory" in _SQL_0002
        assert "CREATE TABLE IF NOT EXISTS skill_candidates" in _SQL_0002


class TestSkillCandidatesShape:
    def test_has_columns_the_miner_writes(self):
        for col in ("agent_slug", "skill_name", "skill_body", "source_doc_ids",
                    "cluster_signature", "recurrence", "status"):
            assert col in _SQL_0002, f"skill_candidates.{col} missing"

    def test_unique_cluster_signature_index(self):
        assert "uq_skill_candidates_sig" in _SQL_0002
        assert re.search(r"agent_slug,\s*cluster_signature", _SQL_0002)


class TestFlagSpineSeededOff:
    def test_all_flags_seeded_false(self):
        for flag in _FLAGS:
            assert f"'{flag}'" in _SQL_0002, f"flag {flag} not seeded"
        # Seeds must be OFF and non-clobbering.
        assert "ON CONFLICT (name) DO NOTHING" in _SQL_0002
        assert "true" not in _SQL_0002.split("INSERT INTO feature_flags", 1)[1].lower()


class TestIdempotent:
    def test_no_bare_create_or_alter_add(self):
        # Every table/column add must be guarded so re-apply is a no-op.
        assert "CREATE TABLE " not in _SQL_0002.replace("CREATE TABLE IF NOT EXISTS", "")
        for m in re.finditer(r"ADD COLUMN (?!IF NOT EXISTS)", _SQL_0002):
            raise AssertionError("unguarded ADD COLUMN in 0002")

    def test_constraints_added_guardedly(self):
        # Named constraints are added inside an IF NOT EXISTS pg_constraint guard.
        for conname in ("documents_memory_class_chk", "session_memory_scope_chk",
                        "skill_candidates_status_chk"):
            assert conname in _SQL_0002
            assert f"conname = '{conname}'" in _SQL_0002
