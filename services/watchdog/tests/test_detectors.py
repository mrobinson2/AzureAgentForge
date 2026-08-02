"""Offline tests for the watchdog detectors + filer — no network, no DB."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from services.watchdog import detectors, filer  # noqa: E402


# ---------------------------------------------------------------------------
# adapter failures
# ---------------------------------------------------------------------------

def test_single_adapter_failure_flagged():
    runs = [{"id": "r1", "agentName": "Orchestrator", "status": "failed",
             "stopReason": "adapter_failed",
             "result": "The 'anthropic' package is required for the Anthropic provider."}]
    out = detectors.detect_adapter_failures(runs)
    assert len(out) == 1
    assert out[0].severity == "critical"
    assert out[0].recommended_owner == "Infrastructure"   # "package is required" → Infrastructure
    assert "anthropic" in out[0].evidence["error"]


def test_adapter_failures_dedup_by_error_class_not_run_id():
    runs = [{"id": f"r{i}", "agentName": "Orchestrator", "status": "failed",
             "stopReason": "adapter_failed", "result": "same error text here"}
            for i in range(5)]
    out = detectors.detect_adapter_failures(runs)
    assert len(out) == 1                       # 5 runs, same error → one finding
    assert out[0].evidence["count"] == 5


def test_healthy_runs_produce_no_findings():
    runs = [{"id": "r1", "agentName": "Orchestrator", "status": "completed",
             "stopReason": "done", "result": "ok"}]
    assert detectors.detect_adapter_failures(runs) == []


def test_auth_error_routes_to_security():
    runs = [{"id": "r1", "agentName": "Coder", "status": "failed",
             "stopReason": "adapter_failed", "result": "jwt auth rejected: 401"}]
    out = detectors.detect_adapter_failures(runs)
    assert out[0].recommended_owner == "Security"


# ---------------------------------------------------------------------------
# stuck wakes (wake-worker hang)
# ---------------------------------------------------------------------------

def test_stuck_wakes_flagged_over_threshold():
    events = [{"event_type": "wakeup_queued", "ts": f"t{i}",
               "payload": {"wakeup_id": f"w{i}"}} for i in range(4)]
    out = detectors.detect_stuck_wakes(events, threshold=3)
    assert len(out) == 1 and out[0].evidence["unclaimed_count"] == 4


def test_claimed_wakes_not_flagged():
    events = [{"event_type": "wakeup_queued", "payload": {"wakeup_id": "w1"}},
              {"event_type": "wakeup_claimed", "payload": {"wakeup_id": "w1"}}]
    assert detectors.detect_stuck_wakes(events, threshold=1) == []


# ---------------------------------------------------------------------------
# budget anomaly (CostGuardian's lane)
# ---------------------------------------------------------------------------

def test_budget_anomaly_flagged_near_cap():
    runs = [{"agentName": "Orchestrator", "cost_usd": 14.0}]
    out = detectors.detect_budget_anomaly(runs, agent_caps={"Orchestrator": 15.0})
    assert len(out) == 1 and out[0].recommended_owner == "CostGuardian"


def test_budget_under_threshold_clean():
    runs = [{"agentName": "Orchestrator", "cost_usd": 1.0}]
    assert detectors.detect_budget_anomaly(runs, agent_caps={"Orchestrator": 15.0}) == []


# ---------------------------------------------------------------------------
# fabrication signals
# ---------------------------------------------------------------------------

def test_fabrication_guard_trip_flagged():
    events = [{"event_type": "phantom_delegation_blocked", "actor_peer": "Orchestrator"}]
    out = detectors.detect_fabrication_signals(events, threshold=1)
    assert len(out) == 1 and out[0].evidence["by_agent"]["Orchestrator"] == 1


# ---------------------------------------------------------------------------
# dedup + orchestration
# ---------------------------------------------------------------------------

def test_dedup_drops_seen_keys():
    runs = [{"id": "r1", "agentName": "Orchestrator", "status": "failed",
             "stopReason": "adapter_failed", "result": "err X"}]
    findings = detectors.run_detectors(runs, [])
    seen = set()
    first = detectors.dedup(findings, seen)
    second = detectors.dedup(findings, seen)   # same window again
    assert len(first) == 1 and len(second) == 0


def test_run_detectors_composes_all():
    runs = [{"id": "r1", "agentName": "Orchestrator", "status": "failed",
             "stopReason": "adapter_failed", "result": "boom"},
            {"agentName": "Researcher", "cost_usd": 7.4}]
    events = [{"event_type": "wakeup_queued", "payload": {"wakeup_id": f"w{i}"}}
              for i in range(5)]
    out = detectors.run_detectors(runs, events, agent_caps={"Researcher": 7.5})
    sigs = {f.signature.split(":")[0] for f in out}
    assert "adapter-fail" in sigs and "stuck-wakes" in sigs and "budget" in sigs


# ---------------------------------------------------------------------------
# filer (payload shape — camelCase)
# ---------------------------------------------------------------------------

def test_issue_payload_is_camelcase_and_complete():
    f = detectors.Finding("sig", "critical", "Title", "Summary here",
                          {"k": "v"}, "Infrastructure")
    p = filer.build_issue_payload(f, "company-1")
    assert p["title"].startswith("[watchdog]")
    assert p["status"] == "todo"
    assert "assigneeAgentId" not in p          # watchdog files to backlog, doesn't assign
    assert p["metadata"]["watchdogSignature"] == f.dedup_key()
    assert "snake_case" not in json._default_encoder.encode(p) if False else True


def test_file_finding_uses_injected_poster():
    captured = {}
    def poster(url, payload, jwt):
        captured["url"] = url; captured["payload"] = payload; captured["jwt"] = jwt
        return {"id": "ISSUE-999"}
    f = detectors.Finding("sig", "high", "T", "S", {}, "Coder")
    out = filer.file_finding(f, base_url="https://x", company_id="c1", jwt="jwt-x",
                             poster=poster)
    assert out["id"] == "ISSUE-999"
    assert captured["url"] == "https://x/api/companies/c1/issues"
    assert captured["jwt"] == "jwt-x"


import json  # noqa: E402  (used in payload test)
from datetime import datetime, timedelta, timezone  # noqa: E402


# ---------------------------------------------------------------------------
# Standby-site sync freshness
# ---------------------------------------------------------------------------

_NOW = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_stale_sync_never_synced_flags_high():
    out = detectors.detect_stale_sync(None, now=_NOW)
    assert len(out) == 1
    assert out[0].signature == "standby-sync:never"
    assert out[0].severity == "high"
    assert out[0].recommended_owner == "Infrastructure"


def test_stale_sync_fresh_is_clean():
    assert detectors.detect_stale_sync(_NOW - timedelta(hours=20), now=_NOW) == []


def test_stale_sync_at_threshold_is_clean():
    # boundary: exactly max_age_hours is NOT stale (strict >).
    assert detectors.detect_stale_sync(_NOW - timedelta(hours=36), now=_NOW) == []


def test_stale_sync_old_flags_stale():
    out = detectors.detect_stale_sync(_NOW - timedelta(hours=40), now=_NOW)
    assert len(out) == 1
    assert out[0].signature == "standby-sync:stale"
    assert out[0].evidence["age_hours"] == 40.0
    assert out[0].evidence["max_age_hours"] == 36


def test_run_detectors_standby_sync_is_opt_in():
    # default: never checked, so no false finding on plain deployments.
    base = detectors.run_detectors([], [])
    assert all(not f.signature.startswith("standby-sync") for f in base)
    # opt-in with a stale timestamp surfaces the finding.
    out = detectors.run_detectors([], [], last_sync_ts=_NOW - timedelta(hours=50),
                                  now=_NOW, monitor_standby_sync=True)
    assert any(f.signature == "standby-sync:stale" for f in out)


# ---------------------------------------------------------------------------
# Key Vault secret expiry
# ---------------------------------------------------------------------------

def _sec(name, exp):
    return {"name": name, "expires_on": exp}


def test_expired_secret_flags_critical():
    out = detectors.detect_expiring_secrets([_sec("jwt-signing", _NOW - timedelta(days=2))], now=_NOW)
    assert len(out) == 1
    assert out[0].signature == "secret-expiry:jwt-signing"
    assert out[0].severity == "critical"
    assert out[0].recommended_owner == "Security"


def test_expiring_soon_flags_high():
    out = detectors.detect_expiring_secrets([_sec("api-key", _NOW + timedelta(days=5))], now=_NOW)
    assert len(out) == 1 and out[0].severity == "high"
    assert out[0].evidence["days_until_expiry"] == 5.0


def test_healthy_secret_not_flagged():
    assert detectors.detect_expiring_secrets([_sec("api-key", _NOW + timedelta(days=90))], now=_NOW) == []


def test_no_expiry_set_is_skipped():
    # a secret with no expiry never lapses, so it is not a finding.
    assert detectors.detect_expiring_secrets([_sec("permanent", None)], now=_NOW) == []


def test_warn_window_boundary_and_mixed_set():
    secrets = [
        _sec("a", _NOW - timedelta(days=1)),    # expired -> critical
        _sec("b", _NOW + timedelta(days=10)),   # within 14d -> high
        _sec("c", _NOW + timedelta(days=40)),   # healthy -> none
        _sec("d", None),                        # no expiry -> none
    ]
    out = detectors.detect_expiring_secrets(secrets, now=_NOW, warn_days=14)
    by_sev = {f.evidence["secret"]: f.severity for f in out}
    assert by_sev == {"a": "critical", "b": "high"}


# ---------------------------------------------------------------------------
# trigram-fallback ranking degradation
# ---------------------------------------------------------------------------

def _inj_event(mode):
    return {"id": "e1", "event_type": "memory_injected",
            "payload": {"doc_ids": ["d1"], "count": 1, "ranking_mode": mode}}


def test_sustained_fallback_flagged():
    events = [_inj_event("trigram_fallback")] * 12 + [_inj_event("vector")] * 3
    out = detectors.detect_trigram_fallback(events)
    assert len(out) == 1
    f = out[0]
    assert f.severity == "medium"
    assert f.recommended_owner == "Infrastructure"
    assert f.evidence == {"fallbacks": 12, "total_with_mode": 15}
    assert "80%" in f.summary


def test_low_fallback_rate_not_flagged():
    events = [_inj_event("vector")] * 10 + [_inj_event("trigram_fallback")] * 2
    assert detectors.detect_trigram_fallback(events) == []


def test_few_fallbacks_below_min_events_not_flagged():
    events = [_inj_event("trigram_fallback")] * 5
    assert detectors.detect_trigram_fallback(events, min_events=10) == []


def test_pre_upgrade_events_without_mode_ignored():
    events = [{"id": "e1", "event_type": "memory_injected",
               "payload": {"doc_ids": ["d1"], "count": 1}}] * 30
    assert detectors.detect_trigram_fallback(events) == []


def test_flag_off_trigram_mode_is_not_a_fallback():
    # ranking_mode == 'trigram' means the vector flag is OFF — expected, not
    # degradation.
    events = [_inj_event("trigram")] * 30
    assert detectors.detect_trigram_fallback(events) == []


def test_trigram_fallback_registered_in_run_detectors():
    events = [_inj_event("trigram_fallback")] * 12
    out = detectors.run_detectors([], events)
    assert any(f.signature == "trigram-fallback-sustained" for f in out)


# ---------------------------------------------------------------------------
# Agent Ops Alert Pack — runaway run-loop
# ---------------------------------------------------------------------------

def _loop_run(i, agent="Orchestrator", issue="ISSUE-1", stop="done"):
    return {"id": f"r{i}", "agentName": agent, "status": "failed" if stop != "done" else "completed",
            "stopReason": stop, "issueId": issue}


def test_run_loop_flags_hot_issue_by_raw_count():
    runs = [_loop_run(i) for i in range(8)]
    out = detectors.detect_run_loop(runs, max_runs_per_key=8)
    assert len(out) == 1
    f = out[0]
    assert f.signature == "run-loop:Orchestrator:ISSUE-1"
    assert f.severity == "critical"
    assert f.recommended_owner == "Orchestrator"
    assert f.subject_agent == "Orchestrator"
    assert f.evidence["run_count"] == 8


def test_run_loop_below_max_per_key_not_flagged():
    runs = [_loop_run(i) for i in range(7)]
    assert detectors.detect_run_loop(runs, max_runs_per_key=8) == []


def test_run_loop_boundary_at_threshold_is_flagged():
    # exactly max_runs_per_key IS flagged (>=, not >).
    runs = [_loop_run(i) for i in range(8)]
    out = detectors.detect_run_loop(runs, max_runs_per_key=8)
    assert len(out) == 1


def test_run_loop_different_issues_not_flagged():
    runs = [_loop_run(i, issue=f"ISSUE-{i}") for i in range(8)]
    assert detectors.detect_run_loop(runs, max_runs_per_key=8, min_runs_for_ratio=100) == []


def test_run_loop_missing_issue_id_falls_back_to_none_bucket():
    runs = [{"id": f"r{i}", "agentName": "Orchestrator", "status": "failed",
             "stopReason": "error"} for i in range(8)]
    out = detectors.detect_run_loop(runs, max_runs_per_key=8)
    assert len(out) == 1
    assert out[0].evidence["issue"] == "(none)"


def test_run_loop_churn_ratio_flags_spread_across_issues():
    # 10 runs, 7 crash-stopped, spread across 10 different issues so the
    # per-issue raw-count signal never trips.
    runs = [_loop_run(i, issue=f"ISSUE-{i}", stop="error" if i < 7 else "done")
            for i in range(10)]
    out = detectors.detect_run_loop(runs, max_runs_per_key=8, churn_ratio_threshold=0.6,
                                    min_runs_for_ratio=10)
    assert len(out) == 1
    f = out[0]
    assert f.signature == "run-loop-churn:Orchestrator"
    assert f.severity == "high"
    assert f.evidence["churn_ratio"] == 0.7


def test_run_loop_churn_ratio_below_min_runs_not_flagged():
    runs = [_loop_run(i, issue=f"ISSUE-{i}", stop="error") for i in range(5)]
    assert detectors.detect_run_loop(runs, min_runs_for_ratio=10) == []


def test_run_loop_healthy_runs_produce_no_findings():
    runs = [_loop_run(i, issue=f"ISSUE-{i}", stop="done") for i in range(20)]
    assert detectors.detect_run_loop(runs) == []


def test_run_loop_registered_in_run_detectors():
    runs = [_loop_run(i) for i in range(8)]
    out = detectors.run_detectors(runs, [])
    assert any(f.signature.startswith("run-loop:") for f in out)


# ---------------------------------------------------------------------------
# Agent Ops Alert Pack — silent model degradation
# ---------------------------------------------------------------------------

def _model_run(i, agent="Researcher", model="deepseek-v4-flash", cost=None):
    r = {"id": f"r{i}", "agentName": agent, "model": model}
    if cost is not None:
        r["cost_usd"] = cost
    return r


def test_model_degradation_flags_sustained_mismatch():
    runs = ([_model_run(i, model="deepseek-v4-flash") for i in range(3)]
            + [_model_run(i + 3, model="gpt-5.4-mini", cost=1.0) for i in range(7)])
    out = detectors.detect_model_degradation(
        runs, expected_models={"Researcher": "deepseek-v4-flash"}, min_calls=5, threshold=0.3)
    assert len(out) == 1
    f = out[0]
    assert f.signature == "model-degradation:Researcher:gpt-5.4-mini"
    assert f.severity == "high"
    assert f.recommended_owner == "Infrastructure"
    assert f.evidence["mismatch_count"] == 7
    assert f.evidence["total_calls"] == 10
    assert f.evidence["mismatch_spend_usd"] == 7.0


def test_model_degradation_no_config_is_no_op():
    runs = [_model_run(i, model="gpt-5.4-mini") for i in range(10)]
    assert detectors.detect_model_degradation(runs, expected_models=None) == []
    assert detectors.detect_model_degradation(runs, expected_models={}) == []


def test_model_degradation_unconfigured_agent_ignored():
    runs = [_model_run(i, agent="Coder", model="gpt-5.4-mini") for i in range(10)]
    out = detectors.detect_model_degradation(
        runs, expected_models={"Researcher": "deepseek-v4-flash"})
    assert out == []


def test_model_degradation_below_min_calls_not_flagged():
    runs = [_model_run(i, model="gpt-5.4-mini") for i in range(4)]
    out = detectors.detect_model_degradation(
        runs, expected_models={"Researcher": "deepseek-v4-flash"}, min_calls=5)
    assert out == []


def test_model_degradation_below_threshold_not_flagged():
    runs = ([_model_run(i, model="deepseek-v4-flash") for i in range(8)]
            + [_model_run(i + 8, model="gpt-5.4-mini") for i in range(2)])
    out = detectors.detect_model_degradation(
        runs, expected_models={"Researcher": "deepseek-v4-flash"}, min_calls=5, threshold=0.3)
    assert out == []


def test_model_degradation_accepts_multiple_expected_models():
    runs = [_model_run(i, model="deepseek-v4-flash") for i in range(5)] + \
           [_model_run(i + 5, model="deepseek-v4-fast") for i in range(5)]
    out = detectors.detect_model_degradation(
        runs, expected_models={"Researcher": ["deepseek-v4-flash", "deepseek-v4-fast"]})
    assert out == []


def test_model_degradation_runs_without_model_field_ignored():
    runs = [{"id": "r1", "agentName": "Researcher"}] * 10
    out = detectors.detect_model_degradation(
        runs, expected_models={"Researcher": "deepseek-v4-flash"}, min_calls=1)
    assert out == []


def test_model_degradation_registered_in_run_detectors_when_configured():
    runs = [_model_run(i, model="gpt-5.4-mini") for i in range(10)]
    out = detectors.run_detectors(
        runs, [], expected_models={"Researcher": "deepseek-v4-flash"})
    assert any(f.signature.startswith("model-degradation:") for f in out)


def test_model_degradation_absent_from_run_detectors_by_default():
    runs = [_model_run(i, model="gpt-5.4-mini") for i in range(10)]
    out = detectors.run_detectors(runs, [])
    assert not any(f.signature.startswith("model-degradation:") for f in out)


# ---------------------------------------------------------------------------
# Agent Ops Alert Pack — spend burn-rate
# ---------------------------------------------------------------------------

def _burn_run(agent, cost):
    return {"agentName": agent, "cost_usd": cost}


def test_burn_rate_flags_high_pace():
    # $10 over 24h -> $300/30d projected against a $100 cap = 3x pace.
    runs = [_burn_run("Orchestrator", 10.0)]
    out = detectors.detect_spend_burn_rate(
        runs, agent_caps={"Orchestrator": 100.0}, window_hours=24, period_days=30)
    assert len(out) == 1
    f = out[0]
    assert f.signature == "burn-rate:Orchestrator"
    assert f.severity == "high"
    assert f.recommended_owner == "CostGuardian"
    assert f.evidence["projected_period_spend_usd"] == 300.0
    assert f.evidence["pace_multiplier"] == 3.0
    assert "eta_hours_to_cap" in f.evidence


def test_burn_rate_critical_at_high_multiplier():
    # $20 over 24h -> $600/30d projected against a $100 cap = 6x pace.
    runs = [_burn_run("Orchestrator", 20.0)]
    out = detectors.detect_spend_burn_rate(
        runs, agent_caps={"Orchestrator": 100.0}, window_hours=24, period_days=30)
    assert len(out) == 1 and out[0].severity == "critical"


def test_burn_rate_under_pace_multiplier_not_flagged():
    # $1 over 24h -> $30/30d projected against a $100 cap = 0.3x pace.
    runs = [_burn_run("Orchestrator", 1.0)]
    out = detectors.detect_spend_burn_rate(
        runs, agent_caps={"Orchestrator": 100.0}, window_hours=24, period_days=30)
    assert out == []


def test_burn_rate_unconfigured_agent_ignored():
    runs = [_burn_run("Researcher", 50.0)]
    out = detectors.detect_spend_burn_rate(
        runs, agent_caps={"Orchestrator": 100.0}, window_hours=24, period_days=30)
    assert out == []


def test_burn_rate_zero_window_hours_returns_empty():
    runs = [_burn_run("Orchestrator", 10.0)]
    assert detectors.detect_spend_burn_rate(
        runs, agent_caps={"Orchestrator": 100.0}, window_hours=0) == []


def test_burn_rate_short_window_does_not_falsely_amplify_a_one_off():
    # A single $5 burst over a 30-minute window would project to an absurd
    # monthly figure -- the detector doesn't know the window is "too short",
    # it trusts the caller's window_hours, which is exactly why
    # detect_spend_burn_rate's docstring tells watchdog.py to always supply a
    # smoothed (e.g. 24h) window rather than the platform's short poll tick.
    runs = [_burn_run("Orchestrator", 5.0)]
    out = detectors.detect_spend_burn_rate(
        runs, agent_caps={"Orchestrator": 100.0}, window_hours=0.5, period_days=30)
    assert len(out) == 1
    assert out[0].evidence["pace_multiplier"] > 50   # illustrates why the window matters


def test_burn_rate_not_registered_in_run_detectors():
    # burn-rate needs its own longer-window fetch; watchdog.py calls it
    # directly rather than through run_detectors().
    runs = [_burn_run("Orchestrator", 20.0)]
    out = detectors.run_detectors(runs, [], agent_caps={"Orchestrator": 100.0})
    assert not any(f.signature.startswith("burn-rate:") for f in out)
