"""Voice ops — latency aggregation + cost attribution, offline."""

import pathlib
import sys

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from voicecore.ops import CostLedger, LatencyAggregator, TurnLatency  # noqa: E402


def test_turn_total_is_sum_of_stages():
    assert TurnLatency(stt_ms=120, agent_ms=800, tts_ms=90).total_ms == 1010


def test_empty_aggregator_reports_zero_count():
    assert LatencyAggregator().stats() == {"count": 0}


def test_latency_stats_over_samples():
    agg = LatencyAggregator()
    # totals: 100, 200, 300, 400, 1000 (5 samples)
    for stt, agent, tts in [
        (10, 80, 10), (20, 160, 20), (30, 240, 30), (40, 320, 40), (100, 800, 100)
    ]:
        agg.add(TurnLatency(stt, agent, tts))
    s = agg.stats()
    assert s["count"] == 5
    assert s["max_total_ms"] == 1000
    assert s["p50_total_ms"] == 300      # nearest-rank median of the 5 totals
    assert s["p95_total_ms"] == 1000     # top sample
    assert s["mean_total_ms"] == pytest.approx((100 + 200 + 300 + 400 + 1000) / 5)
    assert s["mean_agent_ms"] == pytest.approx((80 + 160 + 240 + 320 + 800) / 5)


def test_cost_attribution_rolls_up_per_caller():
    led = CostLedger()
    led.attribute("alfred", 0.01)
    led.attribute("alfred", 0.02)
    led.attribute("gandalf", 0.05)
    assert led.total("alfred") == pytest.approx(0.03)
    assert led.total("gandalf") == pytest.approx(0.05)
    assert led.grand_total() == pytest.approx(0.08)
    assert led.breakdown() == {"alfred": pytest.approx(0.03), "gandalf": pytest.approx(0.05)}


def test_unknown_caller_total_is_zero():
    assert CostLedger().total("nobody") == 0.0


def test_negative_cost_rejected():
    with pytest.raises(ValueError):
        CostLedger().attribute("x", -0.01)
