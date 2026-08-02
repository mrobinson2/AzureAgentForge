"""Tests for scripts/roster-cost-gate.py -- pytest, matches repo convention.

Run:
    pytest -q tests/roster-cost-gate

Pure-function tests exercise the parsing/arithmetic directly. CLI tests run
the script as a subprocess against the static fixtures in fixtures/ (pass,
fail, malformed, missing-field) so a real profile/compose edit never has to
touch these fixtures. The last test runs the CLI against the REAL
agents/profiles/*.yaml + docker-compose.yml, documenting that the shipped
roster currently fits the committed cap.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import importlib.util

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "roster_cost_gate",
    Path(__file__).resolve().parent.parent.parent / "scripts" / "roster-cost-gate.py",
)
roster_cost_gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(roster_cost_gate)

GateConfigError = roster_cost_gate.GateConfigError
DAYS_PER_MONTH = roster_cost_gate.DAYS_PER_MONTH
CAP_VAR = roster_cost_gate.CAP_VAR

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "roster-cost-gate.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REAL_PROFILES_DIR = REPO_ROOT / "agents" / "profiles"
REAL_COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"


def run_cli(profiles_dir: Path, compose_file: Path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--profiles-dir", str(profiles_dir),
         "--compose-file", str(compose_file)],
        capture_output=True, text=True,
    )
    return result.returncode, result.stdout, result.stderr


# ── compute_monthly_sum: projection math (daily -> monthly) ─────────────────

class TestComputeMonthlySum:
    def test_multiplies_each_ceiling_by_30_and_sums(self):
        agents = [{"daily_budget_usd": 1.0}, {"daily_budget_usd": 0.5}]
        assert roster_cost_gate.compute_monthly_sum(agents) == pytest.approx(1.5 * DAYS_PER_MONTH)

    def test_days_per_month_is_30(self):
        # Locks the projection constant the design doc and gate output both
        # advertise -- a silent change here would desync the docs from the math.
        assert DAYS_PER_MONTH == 30

    def test_zero_ceiling_contributes_nothing(self):
        agents = [{"daily_budget_usd": 0.0}, {"daily_budget_usd": 2.0}]
        assert roster_cost_gate.compute_monthly_sum(agents) == pytest.approx(60.0)

    def test_empty_roster_sums_to_zero(self):
        assert roster_cost_gate.compute_monthly_sum([]) == 0.0


# ── parse_platform_monthly_cap ───────────────────────────────────────────────

class TestParsePlatformMonthlyCap:
    def test_extracts_default_from_shell_interpolation_form(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "x-roster-cost-gate:\n"
            "  PLATFORM_MONTHLY_BUDGET_USD: ${PLATFORM_MONTHLY_BUDGET_USD:-42.50}\n"
        )
        assert roster_cost_gate.parse_platform_monthly_cap(compose) == pytest.approx(42.50)

    def test_accepts_bare_numeric_override(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("x-roster-cost-gate:\n  PLATFORM_MONTHLY_BUDGET_USD: 99\n")
        assert roster_cost_gate.parse_platform_monthly_cap(compose) == pytest.approx(99.0)

    def test_missing_anchor_is_fatal(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("services:\n  router:\n    image: foo\n")
        with pytest.raises(GateConfigError, match=CAP_VAR):
            roster_cost_gate.parse_platform_monthly_cap(compose)

    def test_missing_file_is_fatal(self, tmp_path):
        with pytest.raises(GateConfigError, match="not found"):
            roster_cost_gate.parse_platform_monthly_cap(tmp_path / "nope.yml")

    def test_unparseable_value_is_fatal(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("x-roster-cost-gate:\n  PLATFORM_MONTHLY_BUDGET_USD: not-a-number\n")
        with pytest.raises(GateConfigError):
            roster_cost_gate.parse_platform_monthly_cap(compose)


# ── load_agent_ceilings ───────────────────────────────────────────────────────

class TestLoadAgentCeilings:
    def test_reads_declared_ceilings(self, tmp_path):
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "a.yaml").write_text(
            "name: A\nrole: a\ndescription: x\nmodel_tier: economy\n"
            "toolsets: [file]\nreports_to: null\ndaily_budget_usd: 0.30\n"
        )
        agents = roster_cost_gate.load_agent_ceilings(profiles)
        assert len(agents) == 1
        assert agents[0]["daily_budget_usd"] == pytest.approx(0.30)
        assert agents[0]["slug"] == "a"

    def test_missing_field_is_fatal_not_zero(self, tmp_path):
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "a.yaml").write_text(
            "name: A\nrole: a\ndescription: x\nmodel_tier: economy\n"
            "toolsets: [file]\nreports_to: null\n"
        )
        with pytest.raises(GateConfigError, match="daily_budget_usd"):
            roster_cost_gate.load_agent_ceilings(profiles)

    def test_negative_ceiling_is_fatal(self, tmp_path):
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "a.yaml").write_text(
            "name: A\nrole: a\ndescription: x\nmodel_tier: economy\n"
            "toolsets: [file]\nreports_to: null\ndaily_budget_usd: -1\n"
        )
        with pytest.raises(GateConfigError):
            roster_cost_gate.load_agent_ceilings(profiles)

    def test_missing_directory_is_fatal(self, tmp_path):
        with pytest.raises(GateConfigError, match="not found"):
            roster_cost_gate.load_agent_ceilings(tmp_path / "nope")

    def test_empty_directory_is_fatal(self, tmp_path):
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        with pytest.raises(GateConfigError, match="no agent profiles"):
            roster_cost_gate.load_agent_ceilings(profiles)


# ── format_fail: delta + top contributors ────────────────────────────────────

class TestFormatFail:
    def test_states_sum_cap_and_delta(self):
        agents = [
            {"slug": "a", "name": "A", "daily_budget_usd": 1.0},
            {"slug": "b", "name": "B", "daily_budget_usd": 0.5},
        ]
        msg = roster_cost_gate.format_fail(agents, 45.0, 40.0)
        assert "$45.00" in msg
        assert "$40.00" in msg
        assert "$5.00" in msg

    def test_ranks_contributors_by_ceiling_descending(self):
        agents = [
            {"slug": "small", "name": "Small", "daily_budget_usd": 0.10},
            {"slug": "big", "name": "Big", "daily_budget_usd": 2.00},
        ]
        msg = roster_cost_gate.format_fail(agents, 63.0, 50.0)
        assert msg.index("big (Big)") < msg.index("small (Small)")

    def test_zero_ceiling_agents_excluded_from_top_contributors(self):
        agents = [
            {"slug": "idle", "name": "Idle", "daily_budget_usd": 0.0},
            {"slug": "spender", "name": "Spender", "daily_budget_usd": 5.0},
        ]
        msg = roster_cost_gate.format_fail(agents, 150.0, 100.0)
        assert "idle" not in msg
        assert "spender" in msg

    def test_states_ceilings_not_forecast(self):
        agents = [{"slug": "a", "name": "A", "daily_budget_usd": 1.0}]
        msg = roster_cost_gate.format_fail(agents, 30.0, 10.0)
        assert "ceilings, not a spend forecast" in msg


# ── CLI: pass / fail / malformed / missing-field fixtures ───────────────────

class TestCli:
    def test_pass_fixture_exits_0(self):
        rc, stdout, stderr = run_cli(FIXTURES / "pass" / "profiles", FIXTURES / "pass" / "docker-compose.yml")
        assert rc == 0, stderr
        assert "OK" in stdout
        assert "$9.00" in stdout
        assert "$20.00" in stdout

    def test_fail_fixture_exits_1_with_delta_and_top_contributors(self):
        rc, stdout, stderr = run_cli(FIXTURES / "fail" / "profiles", FIXTURES / "fail" / "docker-compose.yml")
        assert rc == 1, stdout
        assert "FAIL" in stderr
        assert "$69.00" in stderr  # sum
        assert "$50.00" in stderr  # cap
        assert "$19.00" in stderr  # delta
        # top contributor (agent-a, $1.00/day -> $30.00/mo) ranked first
        assert stderr.index("agent-a") < stderr.index("agent-b") < stderr.index("agent-c")
        assert "$30.00/mo" in stderr

    def test_malformed_yaml_exits_2(self):
        rc, stdout, stderr = run_cli(FIXTURES / "malformed" / "profiles", FIXTURES / "malformed" / "docker-compose.yml")
        assert rc == 2, stdout
        assert "FATAL" in stderr

    def test_missing_daily_budget_field_exits_2(self):
        rc, stdout, stderr = run_cli(FIXTURES / "missing-field" / "profiles", FIXTURES / "missing-field" / "docker-compose.yml")
        assert rc == 2, stdout
        assert "FATAL" in stderr
        assert "daily_budget_usd" in stderr

    def test_missing_profiles_directory_exits_2(self, tmp_path):
        rc, stdout, stderr = run_cli(tmp_path / "does-not-exist", FIXTURES / "pass" / "docker-compose.yml")
        assert rc == 2, stdout
        assert "FATAL" in stderr

    def test_missing_compose_file_exits_2(self, tmp_path):
        rc, stdout, stderr = run_cli(FIXTURES / "pass" / "profiles", tmp_path / "does-not-exist.yml")
        assert rc == 2, stdout
        assert "FATAL" in stderr

    def test_machine_parsable_summary_line_on_pass(self):
        rc, stdout, _ = run_cli(FIXTURES / "pass" / "profiles", FIXTURES / "pass" / "docker-compose.yml")
        first_line = stdout.splitlines()[0]
        assert "sum=$9.00" in first_line
        assert "cap=$20.00" in first_line
        assert "agents=2" in first_line

    def test_machine_parsable_summary_line_on_fail(self):
        _, _, stderr = run_cli(FIXTURES / "fail" / "profiles", FIXTURES / "fail" / "docker-compose.yml")
        first_line = stderr.splitlines()[0]
        assert "sum=$69.00" in first_line
        assert "cap=$50.00" in first_line
        assert "delta=$19.00" in first_line
        assert "agents=3" in first_line

    # ── integration: the real shipped roster + compose file ─────────────────

    def test_real_repo_roster_passes_the_gate(self):
        """Documents the current, real state of the repo: the shipped
        agents/profiles/*.yaml ceilings fit inside the committed
        PLATFORM_MONTHLY_BUDGET_USD in docker-compose.yml. If this ever
        flips to a failure, it means a real profile or compose edit pushed
        the roster over its committed cap -- exactly what this gate exists
        to catch."""
        rc, stdout, stderr = run_cli(REAL_PROFILES_DIR, REAL_COMPOSE_FILE)
        assert rc == 0, f"real roster is over its committed cap:\n{stderr}"
        assert "agents=14" in stdout


# ── run_gate / main return codes (direct, no subprocess) ────────────────────

class TestRunGate:
    def test_run_gate_returns_0_on_pass(self):
        assert roster_cost_gate.run_gate(
            FIXTURES / "pass" / "profiles", FIXTURES / "pass" / "docker-compose.yml"
        ) == 0

    def test_run_gate_returns_1_on_fail(self):
        assert roster_cost_gate.run_gate(
            FIXTURES / "fail" / "profiles", FIXTURES / "fail" / "docker-compose.yml"
        ) == 1

    def test_run_gate_returns_2_on_fatal(self):
        assert roster_cost_gate.run_gate(
            FIXTURES / "malformed" / "profiles", FIXTURES / "malformed" / "docker-compose.yml"
        ) == 2
