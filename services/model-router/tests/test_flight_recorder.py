"""Unit tests for flight_recorder.py — the bounded, in-process call trace.

Covers: prompt fingerprinting, ring-buffer bound (never grows unbounded),
redaction (on by default, enforced regardless of what a caller passes),
JSONL persistence + size-capped rotation, the read API used by
waste_breakers and the /debug/flight-recorder endpoint, and the
"never raises" write-failure contract."""

import json
import os

import pytest

import flight_recorder as fr


# ── prompt_fingerprint ───────────────────────────────────────────────────────

class TestPromptFingerprint:
    def test_identical_messages_same_fingerprint(self):
        msgs = [{"role": "user", "content": "hello there"}]
        assert fr.prompt_fingerprint(msgs) == fr.prompt_fingerprint(msgs)

    def test_different_content_different_fingerprint(self):
        a = [{"role": "user", "content": "hello"}]
        b = [{"role": "user", "content": "goodbye"}]
        assert fr.prompt_fingerprint(a) != fr.prompt_fingerprint(b)

    def test_tool_schema_affects_fingerprint(self):
        msgs = [{"role": "user", "content": "hi"}]
        tools_a = [{"function": {"name": "search"}}]
        tools_b = [{"function": {"name": "compute"}}]
        assert fr.prompt_fingerprint(msgs, tools_a) != fr.prompt_fingerprint(msgs, tools_b)

    def test_empty_input_does_not_raise(self):
        assert isinstance(fr.prompt_fingerprint(None), str)
        assert isinstance(fr.prompt_fingerprint([]), str)

    def test_fingerprint_is_short_and_hex(self):
        fp = fr.prompt_fingerprint([{"role": "user", "content": "x"}])
        assert len(fp) == 16
        int(fp, 16)  # raises ValueError if not hex


# ── Ring buffer bound ─────────────────────────────────────────────────────────

class TestRingBufferBound:
    def test_never_exceeds_max_events(self):
        rec = fr.FlightRecorder(max_events=5)
        for i in range(50):
            rec.record(caller="c", outcome=fr.OUTCOME_SUCCESS, prompt_fingerprint=str(i))
        assert rec.stats()["buffer_size"] == 5
        # Only the most recent 5 survive.
        recent = rec.recent(limit=10)
        assert [e["prompt_fingerprint"] for e in recent] == ["49", "48", "47", "46", "45"]

    def test_rejects_non_positive_max_events(self):
        with pytest.raises(ValueError):
            fr.FlightRecorder(max_events=0)


# ── Redaction ──────────────────────────────────────────────────────────────────

class TestRedaction:
    def test_redact_true_strips_excerpts_even_if_passed(self):
        rec = fr.FlightRecorder(max_events=10, redact=True)
        rec.record(
            outcome=fr.OUTCOME_SUCCESS,
            prompt_excerpt="the secret prompt",
            response_excerpt="the secret response",
        )
        event = rec.recent(limit=1)[0]
        assert "prompt_excerpt" not in event
        assert "response_excerpt" not in event
        assert event["redacted"] is True

    def test_redact_false_keeps_capped_excerpt(self):
        rec = fr.FlightRecorder(max_events=10, redact=False)
        rec.record(
            outcome=fr.OUTCOME_SUCCESS,
            prompt_excerpt="short prompt",
        )
        event = rec.recent(limit=1)[0]
        assert event["prompt_excerpt"] == "short prompt"
        assert event["redacted"] is False

    def test_redact_false_still_caps_length(self):
        rec = fr.FlightRecorder(max_events=10, redact=False)
        rec.record(outcome=fr.OUTCOME_SUCCESS, prompt_excerpt="x" * 5000)
        event = rec.recent(limit=1)[0]
        assert len(event["prompt_excerpt"]) <= fr._MAX_EXCERPT_CHARS + 1  # + ellipsis char

    def test_default_is_redact_on(self):
        rec = fr.FlightRecorder(max_events=10)
        assert rec.redact is True


# ── Write path / never raises ──────────────────────────────────────────────────

class TestWritePath:
    def test_invalid_outcome_counted_not_raised(self):
        rec = fr.FlightRecorder(max_events=10)
        event_id = rec.record(outcome="not-a-real-outcome")
        assert event_id is None
        assert rec.write_failures == 1
        assert rec.last_error is not None

    def test_successful_write_increments_counter(self):
        rec = fr.FlightRecorder(max_events=10)
        rec.record(outcome=fr.OUTCOME_SUCCESS)
        assert rec.writes == 1
        assert rec.write_failures == 0

    def test_total_tokens_defaulted(self):
        rec = fr.FlightRecorder(max_events=10)
        rec.record(outcome=fr.OUTCOME_SUCCESS, input_tokens=10, output_tokens=5)
        event = rec.recent(limit=1)[0]
        assert event["total_tokens"] == 15

    def test_event_id_is_returned_and_stable(self):
        rec = fr.FlightRecorder(max_events=10)
        event_id = rec.record(outcome=fr.OUTCOME_SUCCESS)
        assert event_id is not None
        assert rec.get(event_id)["event_id"] == event_id


# ── Read API used by waste_breakers ──────────────────────────────────────────

class TestReadAPI:
    def test_recent_filters_by_caller(self):
        rec = fr.FlightRecorder(max_events=10)
        rec.record(caller="a", outcome=fr.OUTCOME_SUCCESS)
        rec.record(caller="b", outcome=fr.OUTCOME_SUCCESS)
        rec.record(caller="a", outcome=fr.OUTCOME_SUCCESS)
        assert len(rec.recent(caller="a", limit=10)) == 2
        assert len(rec.recent(caller="b", limit=10)) == 1

    def test_get_missing_event_returns_none(self):
        rec = fr.FlightRecorder(max_events=10)
        assert rec.get("does-not-exist") is None

    def test_count_recent_respects_window(self, monkeypatch):
        rec = fr.FlightRecorder(max_events=10)
        # Freeze time.time() so we can place one event outside the window.
        clock = {"t": 1000.0}
        monkeypatch.setattr(fr.time, "time", lambda: clock["t"])
        rec.record(caller="a", outcome=fr.OUTCOME_SUCCESS)
        clock["t"] = 1100.0  # 100s later
        rec.record(caller="a", outcome=fr.OUTCOME_SUCCESS)
        assert rec.count_recent(caller="a", seconds=60) == 1
        assert rec.count_recent(caller="a", seconds=200) == 2

    def test_count_recent_filters_by_fingerprint(self):
        rec = fr.FlightRecorder(max_events=10)
        rec.record(caller="a", outcome=fr.OUTCOME_SUCCESS, prompt_fingerprint="fp1")
        rec.record(caller="a", outcome=fr.OUTCOME_SUCCESS, prompt_fingerprint="fp2")
        rec.record(caller="a", outcome=fr.OUTCOME_SUCCESS, prompt_fingerprint="fp1")
        assert rec.count_recent(caller="a", fingerprint="fp1", seconds=3600) == 2

    def test_consecutive_failures_stops_at_first_success(self):
        rec = fr.FlightRecorder(max_events=10)
        rec.record(caller="a", outcome=fr.OUTCOME_SUCCESS)
        rec.record(caller="a", outcome=fr.OUTCOME_ERROR)
        rec.record(caller="a", outcome=fr.OUTCOME_ERROR)
        rec.record(caller="a", outcome=fr.OUTCOME_ERROR)
        assert rec.consecutive_failures(caller="a") == 3

    def test_consecutive_failures_zero_for_unknown_caller(self):
        rec = fr.FlightRecorder(max_events=10)
        assert rec.consecutive_failures(caller="ghost") == 0


# ── JSONL persistence + rotation ─────────────────────────────────────────────

class TestJsonlPersistence:
    def test_disabled_when_no_path(self):
        rec = fr.FlightRecorder(max_events=10, jsonl_path=None)
        rec.record(outcome=fr.OUTCOME_SUCCESS)
        assert rec.stats()["jsonl_path"] is None

    def test_writes_one_line_per_event(self, tmp_path):
        path = str(tmp_path / "trace.jsonl")
        rec = fr.FlightRecorder(max_events=10, jsonl_path=path)
        rec.record(outcome=fr.OUTCOME_SUCCESS, caller="a")
        rec.record(outcome=fr.OUTCOME_SUCCESS, caller="b")
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["caller"] == "a"

    def test_rotation_bounds_size(self, tmp_path):
        path = str(tmp_path / "trace.jsonl")
        # Tiny cap so a handful of events forces a rotation.
        rec = fr.FlightRecorder(max_events=1000, jsonl_path=path, jsonl_max_bytes=200)
        for i in range(30):
            rec.record(outcome=fr.OUTCOME_SUCCESS, caller=f"caller-{i}")
        assert os.path.exists(path)
        assert os.path.exists(path + ".1")
        # Current file must never exceed the cap by more than one line.
        assert os.path.getsize(path) < 500

    def test_disk_failure_does_not_raise(self, tmp_path):
        # Point the jsonl path at a directory (not a file) so the append
        # write fails with an OSError — record() must still return an id.
        bad_dir = tmp_path / "not_a_file"
        bad_dir.mkdir()
        rec = fr.FlightRecorder(max_events=10, jsonl_path=str(bad_dir))
        event_id = rec.record(outcome=fr.OUTCOME_SUCCESS)
        assert event_id is not None  # in-memory ring buffer still got it
        assert rec.get(event_id) is not None


class TestStats:
    def test_stats_shape(self):
        rec = fr.FlightRecorder(max_events=3, redact=True)
        rec.record(outcome=fr.OUTCOME_SUCCESS)
        s = rec.stats()
        assert s["max_events"] == 3
        assert s["buffer_size"] == 1
        assert s["redact"] is True
        assert s["writes"] == 1
        assert s["write_failures"] == 0
