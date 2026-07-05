"""Offline tests for automatic repetition detection -> skill autogen (0008).

Pure units for the parser/slug/signature/allowlist, plus asyncio.run-driven
tests (no pytest-asyncio in this suite) for the flag-gating and the one-
candidate-per-cluster dedup, using a fake asyncpg pool. No real DB/HTTP."""

import asyncio

from governor import db, llm, skill_miner


class TestParseSkillSynthesis:
    def test_clean_json(self):
        out = llm.parse_skill_synthesis('{"name": "Deploy Hotfix", "body": "When to use: x\\n1. do"}')
        assert out == {"name": "deploy-hotfix", "body": "When to use: x\n1. do"}

    def test_fenced_json(self):
        raw = '```json\n{"name": "rotate-secret", "body": "When to use: rotate"}\n```'
        assert llm.parse_skill_synthesis(raw)["name"] == "rotate-secret"

    def test_json_embedded_in_prose(self):
        raw = 'Sure! Here:\n{"name": "x y", "body": "When to use: z"}\nhope that helps'
        assert llm.parse_skill_synthesis(raw)["name"] == "x-y"

    def test_empty_or_noise_returns_none(self):
        assert llm.parse_skill_synthesis("") is None
        assert llm.parse_skill_synthesis("   ") is None
        assert llm.parse_skill_synthesis("no json here at all") is None
        assert llm.parse_skill_synthesis("not even { valid") is None
        assert llm.parse_skill_synthesis('{"name": "", "body": ""}') is None
        assert llm.parse_skill_synthesis('{"name": "x", "body": ""}') is None
        assert llm.parse_skill_synthesis('{"name": "", "body": "y"}') is None


class TestSlugify:
    def test_kebab(self):
        assert llm._slugify_skill_name("Deploy The Hotfix") == "deploy-the-hotfix"
        assert llm._slugify_skill_name("  Rotate/Secret!! ") == "rotate-secret"
        assert llm._slugify_skill_name("ALL CAPS") == "all-caps"

    def test_bounded_length(self):
        assert len(llm._slugify_skill_name("word " * 40)) <= 60


class TestClusterSignature:
    def test_stable_and_distinct(self):
        assert skill_miner.cluster_signature("doc-1") == skill_miner.cluster_signature("doc-1")
        assert skill_miner.cluster_signature("doc-1") != skill_miner.cluster_signature("doc-2")

    def test_short_hex(self):
        assert len(skill_miner.cluster_signature("doc-1")) == 16


class TestAgentAllowlist:
    def test_default_includes_skill_enabled_agents(self, monkeypatch):
        monkeypatch.delenv("SKILL_AUTOGEN_AGENT_ALLOWLIST", raising=False)
        al = skill_miner.agent_allowlist()
        assert {"orchestrator", "coder", "infrastructure"} <= set(al)

    def test_env_override_trims(self, monkeypatch):
        monkeypatch.setenv("SKILL_AUTOGEN_AGENT_ALLOWLIST", " x, y ,z ")
        assert skill_miner.agent_allowlist() == ["x", "y", "z"]


class _FakePool:
    """Minimal async stand-in for an asyncpg pool."""

    def __init__(self, fetch_rows=None, insert_ok=True):
        self._fetch_rows = fetch_rows or []
        self._insert_ok = insert_ok
        self.inserts = []

    async def fetch(self, *args):
        return self._fetch_rows

    async def fetchrow(self, _sql, *args):
        self.inserts.append(args)
        return {"id": f"cand-{len(self.inserts)}"} if self._insert_ok else None

    async def execute(self, *args):
        return "OK"


def _patch_governor(monkeypatch, *, flag, pool, skill):
    async def _flag(_name):
        return flag

    async def _pool():
        return pool

    async def _emit(*a, **k):
        return None

    async def _synth(_agent, _contents):
        return skill

    monkeypatch.setattr(db, "flag_enabled", _flag)
    monkeypatch.setattr(db, "pool", _pool)
    monkeypatch.setattr(db, "emit_event", _emit)
    monkeypatch.setattr(llm, "synthesize_skill", _synth)


class TestMineGating:
    def test_flag_off_noops(self, monkeypatch):
        _patch_governor(monkeypatch, flag=False, pool=_FakePool(), skill={"name": "x", "body": "y"})
        monkeypatch.setenv("GOVERNOR_WORKSPACE", "ws")
        assert asyncio.run(skill_miner.mine_once()) == 0

    def test_no_workspace_noops(self, monkeypatch):
        _patch_governor(monkeypatch, flag=True, pool=_FakePool(), skill={"name": "x", "body": "y"})
        monkeypatch.delenv("GOVERNOR_WORKSPACE", raising=False)
        monkeypatch.delenv("HONCHO_APP_ID", raising=False)
        assert asyncio.run(skill_miner.mine_once()) == 0


class TestMineDedup:
    def _row(self, seed, sibs, rec):
        return {
            "seed_id": seed,
            "seed_content": f"procedure {seed}",
            "recurrence": rec,
            "sibling_ids": sibs,
            "sibling_contents": [f"procedure {s}" for s in sibs],
        }

    def test_overlapping_clusters_make_one_candidate(self, monkeypatch):
        # Two seed rows describe the SAME cluster (A<->B<->C). The covered-set
        # guard must collapse them to a single candidate.
        rows = [self._row("A", ["B", "C"], 2), self._row("B", ["A", "C"], 2)]
        pool = _FakePool(fetch_rows=rows)
        _patch_governor(monkeypatch, flag=True, pool=pool, skill={"name": "proc", "body": "When to use: x"})
        created = asyncio.run(skill_miner.mine_agent("coder", "ws"))
        assert created == 1
        assert len(pool.inserts) == 1

    def test_disjoint_clusters_make_two_candidates(self, monkeypatch):
        rows = [self._row("A", ["B"], 1), self._row("X", ["Y"], 1)]
        pool = _FakePool(fetch_rows=rows)
        _patch_governor(monkeypatch, flag=True, pool=pool, skill={"name": "proc", "body": "When to use: x"})
        assert asyncio.run(skill_miner.mine_agent("infrastructure", "ws")) == 2
        assert len(pool.inserts) == 2

    def test_synth_none_skips_persist(self, monkeypatch):
        rows = [self._row("A", ["B", "C"], 2)]
        pool = _FakePool(fetch_rows=rows)
        _patch_governor(monkeypatch, flag=True, pool=pool, skill=None)
        assert asyncio.run(skill_miner.mine_agent("coder", "ws")) == 0
        assert len(pool.inserts) == 0

    def test_insert_conflict_not_counted(self, monkeypatch):
        # ON CONFLICT DO NOTHING -> fetchrow returns None -> not counted.
        rows = [self._row("A", ["B"], 1)]
        pool = _FakePool(fetch_rows=rows, insert_ok=False)
        _patch_governor(monkeypatch, flag=True, pool=pool, skill={"name": "proc", "body": "When to use: x"})
        assert asyncio.run(skill_miner.mine_agent("coder", "ws")) == 0
