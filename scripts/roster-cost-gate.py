#!/usr/bin/env python3
"""roster-cost-gate.py — Roster Cost Gate: config-time roster budget solvency.

AAF already enforces cost at two other layers: budget_enforcement.py acts on
a *single* model tier that has exhausted its *_DAILY_BUDGET_USD (warn /
downgrade / block, see services/model-router/budget_enforcement.py), and the
watchdog's spend burn-rate detector flags a single agent pacing over its
monthly cap *after* runs have happened (services/watchdog/detectors.py,
detect_spend_burn_rate). Neither one ever sums the roster.

Every agent in agents/profiles/*.yaml declares a daily_budget_usd ceiling.
Nothing checked whether those ceilings, added up and projected across a
month, could even fit inside PLATFORM_MONTHLY_BUDGET_USD (docker-compose.yml)
-- the platform's committed monthly cap. Two numbers, configured in two
different files, that nobody reconciled. This gate reconciles them at config
review time, before either one reaches an invoice.

IMPORTANT: daily_budget_usd values are CEILINGS, not a spend forecast. An
agent rarely burns its full daily cap every day. This gate does not claim the
platform is overspending -- it catches a configuration defect: if every agent
simultaneously spent its full ceiling for a 30-day month, would the roster's
combined ceiling fit under the platform's hard cap? See
docs/design/roster-cost-gate.md for the full writeup.

Usage:
    python scripts/roster-cost-gate.py [--profiles-dir DIR] [--compose-file PATH]

Exit codes:
    0  pass -- roster's 30-day ceiling sum is within the platform cap
    1  fail -- roster's 30-day ceiling sum exceeds the platform cap
    2  fatal -- config missing, unreadable, or malformed (can't gate at all)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

DEFAULT_PROFILES_DIR = REPO_ROOT / "agents" / "profiles"
DEFAULT_COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

DAYS_PER_MONTH = 30
CAP_VAR = "PLATFORM_MONTHLY_BUDGET_USD"
CAP_ANCHOR = "x-roster-cost-gate"
BUDGET_FIELD = "daily_budget_usd"


class GateConfigError(Exception):
    """Fatal, un-recoverable config problem -- the gate can't run at all (exit 2)."""


def load_agent_ceilings(profiles_dir: Path) -> list[dict]:
    """Reads every agents/profiles/*.yaml and returns each agent's declared
    daily_budget_usd ceiling. Fatal (not a FAIL) if a profile is missing the
    field entirely -- an undeclared ceiling can't be summed, so the gate
    can't render a verdict, rather than silently treating it as $0."""
    if not profiles_dir.is_dir():
        raise GateConfigError(f"profiles directory not found: {profiles_dir}")

    files = sorted(profiles_dir.glob("*.yaml"))
    if not files:
        raise GateConfigError(f"no agent profiles found in {profiles_dir}")

    agents = []
    for f in files:
        try:
            data = yaml.safe_load(f.read_text())
        except yaml.YAMLError as err:
            raise GateConfigError(f"{f.name}: could not parse YAML: {err}") from err

        if not isinstance(data, dict):
            raise GateConfigError(f"{f.name}: profile is not a YAML mapping")

        slug = data.get("role") or f.stem
        name = data.get("name", slug)
        ceiling = data.get(BUDGET_FIELD)

        if ceiling is None:
            raise GateConfigError(
                f"{f.name}: missing '{BUDGET_FIELD}' -- every agent profile must "
                "declare a per-agent cost ceiling for the roster cost gate to sum "
                "it. Add e.g. 'daily_budget_usd: 0.20' and re-run."
            )
        if isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)):
            raise GateConfigError(
                f"{f.name}: '{BUDGET_FIELD}' must be a non-negative number, got {ceiling!r}"
            )
        if ceiling < 0:
            raise GateConfigError(
                f"{f.name}: '{BUDGET_FIELD}' must be a non-negative number, got {ceiling!r}"
            )

        agents.append({"slug": slug, "name": name, BUDGET_FIELD: float(ceiling)})

    return agents


def parse_platform_monthly_cap(compose_path: Path) -> float:
    """Reads PLATFORM_MONTHLY_BUDGET_USD straight from docker-compose.yml's
    x-roster-cost-gate block -- the same file a deployment actually loads,
    not a parallel manifest. Handles both the shell-interpolated
    ${VAR:-default} form the compose file ships and a bare numeric override
    (an operator who set the env var directly rather than editing the
    default)."""
    if not compose_path.is_file():
        raise GateConfigError(f"compose file not found: {compose_path}")

    try:
        data = yaml.safe_load(compose_path.read_text())
    except yaml.YAMLError as err:
        raise GateConfigError(f"{compose_path.name}: could not parse YAML: {err}") from err

    anchor = (data or {}).get(CAP_ANCHOR)
    if not isinstance(anchor, dict) or CAP_VAR not in anchor:
        raise GateConfigError(
            f"{CAP_VAR} not declared under '{CAP_ANCHOR}' in {compose_path}"
        )

    raw = anchor[CAP_VAR]
    match = re.search(r"\$\{" + re.escape(CAP_VAR) + r":-([0-9]*\.?[0-9]+)\}", str(raw))
    if match:
        return float(match.group(1))

    try:
        return float(raw)
    except (TypeError, ValueError) as err:
        raise GateConfigError(
            f"{CAP_VAR} value {raw!r} in {compose_path} is neither a "
            f"'${{{CAP_VAR}:-N}}' default nor a bare number"
        ) from err


def compute_monthly_sum(agents: list[dict]) -> float:
    """Projects each agent's daily ceiling across a 30-day month and sums."""
    return sum(a.get(BUDGET_FIELD) or 0.0 for a in agents) * DAYS_PER_MONTH


def format_pass(agents: list[dict], monthly_sum: float, monthly_cap: float) -> str:
    headroom = monthly_cap - monthly_sum
    return (
        f"[roster-cost-gate] OK sum=${monthly_sum:.2f} cap=${monthly_cap:.2f} "
        f"headroom=${headroom:.2f} agents={len(agents)}\n"
        f"[roster-cost-gate] {len(agents)} agents' 30-day daily_budget_usd "
        f"ceiling sums to ${monthly_sum:.2f}, within {CAP_VAR} ${monthly_cap:.2f} "
        f"(${headroom:.2f} headroom)."
    )


def format_fail(agents: list[dict], monthly_sum: float, monthly_cap: float) -> str:
    delta = monthly_sum - monthly_cap
    top = sorted(
        (a for a in agents if a.get(BUDGET_FIELD, 0) > 0),
        key=lambda a: a[BUDGET_FIELD],
        reverse=True,
    )
    lines = [
        f"[roster-cost-gate] FAIL sum=${monthly_sum:.2f} cap=${monthly_cap:.2f} "
        f"delta=${delta:.2f} agents={len(agents)}",
        "",
        f"[roster-cost-gate] roster's 30-day ceiling sums to ${monthly_sum:.2f}, "
        f"exceeding {CAP_VAR} ${monthly_cap:.2f} by ${delta:.2f}.",
        "",
        "  These are ceilings, not a spend forecast -- this does not mean the",
        "  platform is currently overspending. It means the roster's combined",
        "  per-agent ceilings and the platform's committed monthly cap are",
        "  configured inconsistently, which nothing else in the deploy path",
        "  checks.",
        "",
        "  Top contributors (by daily_budget_usd, descending):",
    ]
    for a in top:
        monthly = a[BUDGET_FIELD] * DAYS_PER_MONTH
        lines.append(
            f"    - {a['slug']} ({a['name']}): "
            f"${a[BUDGET_FIELD]:.2f}/day -> ${monthly:.2f}/mo"
        )
    lines += [
        "",
        f"  Fix one of the two: raise {CAP_VAR} in docker-compose.yml, or lower",
        "  one or more daily_budget_usd values in agents/profiles/*.yaml, until",
        "  the roster's 30-day ceiling sum is <= the platform cap.",
    ]
    return "\n".join(lines)


def run_gate(profiles_dir: Path, compose_file: Path) -> int:
    try:
        agents = load_agent_ceilings(profiles_dir)
        monthly_cap = parse_platform_monthly_cap(compose_file)
    except GateConfigError as err:
        print(f"[roster-cost-gate] FATAL: {err}", file=sys.stderr)
        return 2

    monthly_sum = compute_monthly_sum(agents)

    if monthly_sum > monthly_cap:
        print(format_fail(agents, monthly_sum, monthly_cap), file=sys.stderr)
        return 1

    print(format_pass(agents, monthly_sum, monthly_cap))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Roster Cost Gate")
    parser.add_argument(
        "--profiles-dir", type=Path, default=DEFAULT_PROFILES_DIR,
        help="Directory of agent profile YAML files (default: agents/profiles)",
    )
    parser.add_argument(
        "--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE,
        help="Compose file declaring PLATFORM_MONTHLY_BUDGET_USD (default: docker-compose.yml)",
    )
    args = parser.parse_args(argv)
    return run_gate(args.profiles_dir, args.compose_file)


if __name__ == "__main__":
    sys.exit(main())
