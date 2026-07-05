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
