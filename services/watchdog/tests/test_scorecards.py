"""Offline tests for track-record routing — pure scorecard aggregation + the
durable_fact upsert payload/write path. No network (poster/getter injected)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from services.watchdog import scorecards, memory  # noqa: E402


def _run(agent, status, stop=None, start="2024-01-01T10:00:00Z",
         finish="2024-01-01T10:04:00Z", cost=0.03):
    return {"agentName": agent, "status": status, "stopReason": stop,
            "startedAt": start, "finishedAt": finish, "costUsd": cost}


class TestComputeScorecards:
    def test_basic_aggregation_and_completion_rate(self):
        runs = [
            _run("Researcher", "completed", cost=0.03),
            _run("Researcher", "completed", cost=0.02),
            _run("Researcher", "failed", stop="error", cost=0.01),
        ]
        cards = scorecards.compute_scorecards(runs)
        assert len(cards) == 1
        g = cards[0]
        assert g["slug"] == "researcher"
        assert g["total"] == 3 and g["ok"] == 2
        assert g["completion_rate"] == round(2 / 3, 3)
        assert g["median_cost_usd"] == 0.02         # median of 0.03/0.02/0.01
        assert g["median_duration_s"] == 240.0      # 4 minutes each
        assert "[track-record:researcher]" in g["summary"]

    def test_below_min_runs_skipped(self):
        runs = [_run("Coder", "completed"), _run("Coder", "completed")]  # only 2
        assert scorecards.compute_scorecards(runs) == []
        assert scorecards.compute_scorecards(runs, min_runs=2)[0]["slug"] == "coder"

    def test_unknown_agent_names_dropped(self):
        runs = [_run("NotAnAgent", "completed") for _ in range(5)]
        assert scorecards.compute_scorecards(runs) == []

    def test_sorted_best_first(self):
        runs = (
            [_run("Researcher", "completed") for _ in range(3)]                    # 100%
            + [_run("Coder", "failed", stop="error") for _ in range(3)]          # 0%
        )
        cards = scorecards.compute_scorecards(runs)
        assert [c["slug"] for c in cards] == ["researcher", "coder"]
        assert cards[0]["completion_rate"] == 1.0 and cards[1]["completion_rate"] == 0.0

    def test_missing_cost_and_duration_tolerated(self):
        runs = [{"agentName": "Infrastructure", "status": "completed"} for _ in range(3)]
        c = scorecards.compute_scorecards(runs)[0]
        assert c["median_cost_usd"] is None and c["median_duration_s"] is None
        assert "n/a" in c["summary"]


class TestBuildScorecardPayload:
    def test_scoped_to_orchestrator_as_track_record(self):
        card = {"slug": "researcher", "summary": "score [track-record:researcher]"}
        p = memory.build_scorecard_payload(card, workspace="ws")
        assert p["memory_class"] == "durable_fact"
        assert p["scope_kind"] == "peer"
        assert p["scope_id"] == "orchestrator"      # injected for the orchestrator
        assert p["planner_hint"] == "track_record"
        assert p["created_by_peer"] == "watchdog"
        assert p["source_type"] == "agent_observed"
        assert p["content"] == card["summary"]


class TestWriteScorecards:
    def test_retires_prior_then_admits_fresh(self):
        calls = []
        def poster(url, payload, key):
            calls.append((url, payload.get("action") or "admit"))
            return {"status": "admitted"}
        def getter(url, key):
            return [{"id": "old1", "snippet": "old score [track-record:researcher]"},
                    {"id": "other", "snippet": "a failure lesson [signature: x]"}]
        card = {"slug": "researcher", "summary": "fresh [track-record:researcher]"}
        n = memory.write_scorecards([card], base_url="http://gov", key="k",
                                    workspace="ws", getter=getter, poster=poster)
        assert n == 1
        # rm the matching prior (old1) — NOT the unrelated failure lesson — then admit
        assert ("http://gov/memory/old1/action", "rm") in calls
        assert all("other" not in url for url, _ in calls)
        assert ("http://gov/admit", "admit") in calls

    def test_no_prior_just_admits(self):
        calls = []
        memory.write_scorecards(
            [{"slug": "infrastructure", "summary": "s [track-record:infrastructure]"}],
            base_url="http://gov", key="k", workspace="ws",
            getter=lambda u, k: [], poster=lambda u, p, k: calls.append(u) or {},
        )
        assert calls == ["http://gov/admit"]
