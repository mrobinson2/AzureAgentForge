"""Tests for scripts/replay-gate/prompt_contract_check.py.

Focus: fixture pass/fail evaluation, the base-vs-candidate diff/regression
logic (the actual behavioural gate), and the real fixtures shipped in
scripts/replay-gate/fixtures/ against the real agents/profiles/ roster.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import prompt_contract_check as pcc
from conftest import GOOD_BODY, write_profile

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = Path(__file__).resolve().parent.parent / "prompt_contract_check.py"
REAL_PROFILES_DIR = REPO_ROOT / "agents" / "profiles"
REAL_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# must_contain / must_not_contain / token budget
# ---------------------------------------------------------------------------

def test_must_contain_pass(profiles_dir):
    write_profile(profiles_dir, "widget")
    fx = {"fixture_id": "f1", "prompt_contract": {"agents": ["widget"], "must_contain": ["Identity"]}}
    failures = pcc.check_prompt_contract(fx, profiles_dir, {})
    assert failures == []


def test_must_contain_fail(profiles_dir):
    write_profile(profiles_dir, "widget")
    fx = {"fixture_id": "f1", "prompt_contract": {"agents": ["widget"], "must_contain": ["NOT PRESENT ANYWHERE"]}}
    failures = pcc.check_prompt_contract(fx, profiles_dir, {})
    assert len(failures) == 1
    assert "missing required pattern" in failures[0]


def test_must_not_contain_fail(profiles_dir):
    write_profile(profiles_dir, "widget", body=GOOD_BODY + "\nhttps://internal.example-corp-real.io/secret\n")
    fx = {"fixture_id": "f1", "prompt_contract": {"agents": ["widget"], "must_not_contain": [r"https://internal\."]}}
    failures = pcc.check_prompt_contract(fx, profiles_dir, {})
    assert len(failures) == 1
    assert "forbidden pattern" in failures[0]


def test_must_not_contain_pass(profiles_dir):
    write_profile(profiles_dir, "widget")
    fx = {"fixture_id": "f1", "prompt_contract": {"agents": ["widget"], "must_not_contain": [r"https://internal\."]}}
    failures = pcc.check_prompt_contract(fx, profiles_dir, {})
    assert failures == []


def test_token_budget_ceiling_triggers(profiles_dir):
    write_profile(profiles_dir, "widget")
    fx = {"fixture_id": "f1", "prompt_contract": {"agents": ["widget"], "max_chars_per_4_estimate": 1}}
    failures = pcc.check_prompt_contract(fx, profiles_dir, {})
    assert len(failures) == 1
    assert "exceeds budget ceiling" in failures[0]


def test_token_budget_ceiling_passes_with_headroom(profiles_dir):
    write_profile(profiles_dir, "widget")
    fx = {"fixture_id": "f1", "prompt_contract": {"agents": ["widget"], "max_chars_per_4_estimate": 100_000}}
    failures = pcc.check_prompt_contract(fx, profiles_dir, {})
    assert failures == []


def test_agents_all_resolves_via_roster(profiles_dir):
    write_profile(profiles_dir, "widget")
    write_profile(profiles_dir, "gadget")
    fx = {"fixture_id": "f1", "prompt_contract": {"agents": "all", "must_contain": ["Identity"]}}
    failures = pcc.check_prompt_contract(fx, profiles_dir, {})
    assert failures == []


def test_missing_composition_surfaces_as_failure_not_exception(profiles_dir):
    (profiles_dir / "gadget.yaml").write_text(
        "name: Gadget\nrole: gadget\ndescription: x\nmodel_tier: economy\ntoolsets: [file]\nreports_to: null\n"
    )
    fx = {"fixture_id": "f1", "prompt_contract": {"agents": ["gadget"], "must_contain": ["x"]}}
    failures = pcc.check_prompt_contract(fx, profiles_dir, {})
    assert len(failures) == 1
    assert "composition failed" in failures[0]


# ---------------------------------------------------------------------------
# Diff / regression logic — the actual gate behavior
# ---------------------------------------------------------------------------

def _result(fixture_id: str, passed: bool) -> pcc.ContractResult:
    return pcc.ContractResult(fixture_id=fixture_id, passed=passed)


def test_delta_regression_blocks():
    base = [_result("f1", True)]
    candidate = [_result("f1", False)]
    ok = pcc.print_delta(base, candidate)
    assert ok is False


def test_delta_improvement_does_not_block():
    base = [_result("f1", False)]
    candidate = [_result("f1", True)]
    ok = pcc.print_delta(base, candidate)
    assert ok is True


def test_delta_stable_pass_ok():
    base = [_result("f1", True)]
    candidate = [_result("f1", True)]
    assert pcc.print_delta(base, candidate) is True


def test_delta_pre_existing_failure_blocks_as_failing_not_regression(capsys):
    base = [_result("f1", False)]
    candidate = [_result("f1", False)]
    ok = pcc.print_delta(base, candidate)
    out = capsys.readouterr().out
    assert ok is False
    assert "FAILING" in out
    assert "REGRESSION" not in out


def test_delta_new_fixture_only_in_candidate_counts_as_failing_if_it_fails():
    base = []
    candidate = [_result("f1", False)]
    assert pcc.print_delta(base, candidate) is False


def test_delta_new_fixture_only_in_candidate_ok_if_it_passes():
    base = []
    candidate = [_result("f1", True)]
    assert pcc.print_delta(base, candidate) is True


# ---------------------------------------------------------------------------
# End-to-end regression detection through run() + print_delta()
# ---------------------------------------------------------------------------

def test_end_to_end_regression_detected(tmp_path):
    base_dir = tmp_path / "base"
    candidate_dir = tmp_path / "candidate"
    base_dir.mkdir()
    candidate_dir.mkdir()
    write_profile(base_dir, "widget")
    # Candidate: disposition-protocol section silently dropped.
    broken_body = GOOD_BODY.replace(
        "# Completing an issue (disposition protocol)\n\nNo silent terminal states. plan_only. missing_disposition.",
        "",
    )
    write_profile(candidate_dir, "widget", body=broken_body)

    fixtures = [{
        "fixture_id": "disposition-intact",
        "prompt_contract": {"agents": ["widget"], "must_contain": [r"disposition protocol"]},
    }]
    base_results = pcc.run(fixtures, base_dir)
    candidate_results = pcc.run(fixtures, candidate_dir)
    assert base_results[0].passed is True
    assert candidate_results[0].passed is False
    assert pcc.print_delta(base_results, candidate_results) is False


# ---------------------------------------------------------------------------
# JUnit output
# ---------------------------------------------------------------------------

def test_junit_xml_written(tmp_path):
    results = [pcc.ContractResult("f1", True), pcc.ContractResult("f2", False, ["boom"])]
    out = tmp_path / "results.xml"
    pcc.write_junit_xml(results, str(out))
    text = out.read_text()
    assert 'tests="2"' in text
    assert 'failures="1"' in text
    assert "boom" in text


# ---------------------------------------------------------------------------
# --self-test
# ---------------------------------------------------------------------------

def test_self_test_function_passes():
    assert pcc.self_test() == 0


def test_cli_self_test():
    r = subprocess.run([sys.executable, str(SCRIPT), "--self-test"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELF-TEST PASSED" in r.stdout


# ---------------------------------------------------------------------------
# Real fixtures against the real roster
# ---------------------------------------------------------------------------

def test_load_real_fixtures():
    fixtures = pcc.load_contract_fixtures(REAL_FIXTURES_DIR)
    assert len(fixtures) == 8
    ids = {fx["fixture_id"] for fx in fixtures}
    assert len(ids) == 8, "fixture_id values must be unique"


def test_real_fixtures_pass_against_real_roster():
    fixtures = pcc.load_contract_fixtures(REAL_FIXTURES_DIR)
    results = pcc.run(fixtures, REAL_PROFILES_DIR)
    failed = [r for r in results if not r.passed]
    assert failed == [], "\n".join(f"{r.fixture_id}: {r.failures}" for r in failed)


def test_cli_end_to_end_against_real_repo():
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--profiles-dir", str(REAL_PROFILES_DIR), "--label", "candidate"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "8/8 passed" in r.stdout


def test_cli_diff_mode_no_regression_when_base_equals_candidate():
    r = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--profiles-dir", str(REAL_PROFILES_DIR), "--label", "candidate",
         "--compare-profiles-dir", str(REAL_PROFILES_DIR), "--compare-label", "base"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "REGRESSION" not in r.stdout


def test_cli_diff_mode_detects_seeded_regression(tmp_path):
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    for f in REAL_PROFILES_DIR.glob("*"):
        (candidate_dir / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    # Seed a regression: drop security's critical-escalation carve-out text.
    sec = candidate_dir / "security.AGENTS.md"
    sec.write_text(sec.read_text().replace("courtesy CC, not a gate", "just a normal CC"), encoding="utf-8")

    r = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--profiles-dir", str(candidate_dir), "--label", "candidate",
         "--compare-profiles-dir", str(REAL_PROFILES_DIR), "--compare-label", "base"],
        capture_output=True, text=True,
    )
    assert r.returncode == 1
    assert "REGRESSION" in r.stdout
    assert "04-security-critical-escalation-contract" in r.stdout
