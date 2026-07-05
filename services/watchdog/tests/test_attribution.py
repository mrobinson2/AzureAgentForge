"""Offline tests for use-based earned-trust attribution.

No network, no DB — the governor poster is stubbed.
"""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from services.watchdog import attribution, memory  # noqa: E402

T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def run(*, rid="run-1", agent="Researcher", status="completed", started=T0, finished=None, stop=None):
    d = {"id": rid, "agentName": agent, "status": status, "startedAt": started.isoformat()}
    if finished is not None:
        d["finishedAt"] = finished.isoformat()
    if stop is not None:
        d["stopReason"] = stop
    return d


def inj(*, slug="researcher", ts=T0, doc_ids=("d1",)):
    return {"event_type": "memory_injected", "actor_peer": slug, "ts": ts,
            "payload": {"doc_ids": list(doc_ids)}}


class TestAttributeSuccesses:
    def test_credits_injected_docs_of_a_success_run(self):
        out = attribution.attribute_successes([run()], [inj(doc_ids=("d1", "d2"))])
        assert {c["doc_id"] for c in out} == {"d1", "d2"}
        assert all(c["agent_slug"] == "researcher" and c["run_id"] == "run-1" for c in out)

    def test_non_success_status_credits_nothing(self):
        assert attribution.attribute_successes([run(status="failed")], [inj()]) == []
        assert attribution.attribute_successes([run(status="running")], [inj()]) == []

    def test_crash_stop_reason_excluded_even_if_status_success(self):
        out = attribution.attribute_successes([run(status="completed", stop="timeout")], [inj()])
        assert out == []

    def test_different_agent_not_credited(self):
        out = attribution.attribute_successes([run(agent="Orchestrator")], [inj(slug="researcher")])
        assert out == []

    def test_injection_outside_time_window_excluded(self):
        late = inj(ts=T0 + timedelta(hours=2))   # run had no finishedAt → 30-min cap
        assert attribution.attribute_successes([run()], [late]) == []
        early = inj(ts=T0 - timedelta(minutes=10))  # before started - buffer
        assert attribution.attribute_successes([run()], [early]) == []

    def test_within_window_with_finish(self):
        r = run(finished=T0 + timedelta(minutes=5))
        e = inj(ts=T0 + timedelta(minutes=4))
        assert len(attribution.attribute_successes([r], [e])) == 1

    def test_dedup_same_run_doc_pair(self):
        out = attribution.attribute_successes([run()], [inj(doc_ids=("d1",)), inj(doc_ids=("d1",))])
        assert len(out) == 1

    def test_unknown_agent_uuid_skipped(self):
        out = attribution.attribute_successes(
            [run(agent="00000000-0000-0000-0000-000000000000")], [inj()]
        )
        assert out == []

    def test_run_without_started_skipped(self):
        r = {"id": "r", "agentName": "Researcher", "status": "completed"}  # no startedAt
        assert attribution.attribute_successes([r], [inj()]) == []


class TestReconfirmMemory:
    def test_posts_reconfirm_action(self):
        captured = {}

        def poster(url, payload, key):
            captured["url"] = url
            captured["payload"] = payload
            captured["key"] = key
            return {"status": "ok"}

        out = memory.reconfirm_memory(
            "doc-9", "run-7", base_url="http://gov", key="k", poster=poster
        )
        assert out == {"status": "ok"}
        assert captured["url"] == "http://gov/memory/doc-9/action"
        assert captured["payload"]["action"] == "reconfirm"
        assert captured["payload"]["actor"] == "watchdog"
        assert "run-7" in captured["payload"]["note"]
