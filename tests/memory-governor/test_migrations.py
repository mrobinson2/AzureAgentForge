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


class TestMigrationsShipWithTheImage:
    """The governor applies this overlay on startup, so the SQL has to BE in the
    image. It was not: the Dockerfile copied only src/governor, migrate.py
    resolved MIGRATIONS_DIR to a path that did not exist, and apply() logged
    "schema up to date (0 known)" — a success message for having done nothing.
    The governor then failed every feature-flag lookup on a missing
    feature_flags table. These pin both halves of that fix."""

    def test_dockerfile_copies_the_migrations_directory(self):
        dockerfile = (
            pathlib.Path(__file__).resolve().parents[2]
            / "services" / "memory-governor" / "Dockerfile"
        ).read_text()
        assert re.search(r"^COPY\s+.*\bmigrations\s+/migrations\s*$", dockerfile, re.M), (
            "the image must carry the migrations at /migrations — the path "
            "governor/migrate.py resolves from its own location"
        )

    def test_migrate_refuses_to_report_success_on_an_empty_dir(self, tmp_path, monkeypatch):
        # Nothing to apply is only good news when there was something to check.
        import asyncio

        from governor import migrate

        monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path / "absent")
        try:
            asyncio.run(migrate.apply())
        except migrate.MigrationsMissing as exc:
            assert "no .sql migrations found" in str(exc)
        else:
            raise AssertionError("apply() must raise when the directory is missing")

    def test_migrate_refuses_a_present_but_empty_dir(self, tmp_path, monkeypatch):
        import asyncio

        from governor import migrate

        empty = tmp_path / "migrations"
        empty.mkdir()
        monkeypatch.setattr(migrate, "MIGRATIONS_DIR", empty)
        try:
            asyncio.run(migrate.apply())
        except migrate.MigrationsMissing:
            pass
        else:
            raise AssertionError("apply() must raise when the directory holds no .sql")


class TestChainIsSelfConsistent:
    """The governor applies THIS chain on its own at startup. Anything a later
    migration references must therefore be created by an earlier one in the same
    chain — not by the separate infrastructure/migrations chain that happens to
    have run first on long-lived databases.

    The regression: 0003/0004 seed flags with `updated_by`, but 0001's CREATE
    TABLE never had that column. It worked wherever the infra chain had already
    run, and broke on the first fresh database:

        applying migration 0003_memory_digest_flag.sql
        UndefinedColumnError: column "updated_by" of relation "feature_flags"
        does not exist
    """

    def _chain(self):
        return sorted(_MIG.glob("*.sql"))

    def test_every_feature_flags_insert_column_exists_earlier_in_the_chain(self):
        provided: set[str] = set()
        for path in self._chain():
            sql = path.read_text()
            create = re.search(
                r"CREATE TABLE IF NOT EXISTS feature_flags\s*\((.*?)\);", sql, re.S | re.I
            )
            if create:
                for line in create.group(1).splitlines():
                    m = re.match(r"\s*([a-z_]+)\s+[a-z]", line, re.I)
                    if m:
                        provided.add(m.group(1).lower())
            for m in re.finditer(
                r"ALTER TABLE feature_flags\s+ADD COLUMN IF NOT EXISTS\s+([a-z_]+)",
                sql, re.I,
            ):
                provided.add(m.group(1).lower())
            for m in re.finditer(r"INSERT INTO feature_flags\s*\(([^)]*)\)", sql, re.I):
                used = {c.strip().lower() for c in m.group(1).split(",") if c.strip()}
                missing = used - provided
                assert not missing, (
                    f"{path.name} inserts into feature_flags column(s) {sorted(missing)} "
                    "that no earlier migration in this chain creates — fine only if "
                    "infrastructure/migrations ran first, which a fresh deploy does not do"
                )

    def test_the_two_chains_agree_on_the_feature_flags_columns(self):
        def columns(sql: str) -> set[str]:
            m = re.search(
                r"CREATE TABLE IF NOT EXISTS feature_flags\s*\((.*?)\);", sql, re.S | re.I
            )
            assert m, "feature_flags CREATE TABLE not found"
            return {
                mm.group(1).lower()
                for line in m.group(1).splitlines()
                if (mm := re.match(r"\s*([a-z_]+)\s+[a-z]", line, re.I))
            }

        infra = (
            pathlib.Path(__file__).resolve().parents[2]
            / "infrastructure" / "migrations" / "0001_agent_events_and_feature_flags.sql"
        ).read_text()
        local = (_MIG / "0001_governed_memory_overlay.sql").read_text()
        assert columns(local) == columns(infra), (
            "the two feature_flags definitions have drifted; a database built by "
            "one chain then migrated by the other is how the updated_by break happened"
        )
