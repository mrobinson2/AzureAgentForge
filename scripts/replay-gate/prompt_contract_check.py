#!/usr/bin/env python3
"""prompt_contract_check.py — the Behavioural Replay Gate's contract runner.

Runs `prompt_contract` blocks from scripts/replay-gate/fixtures/*.yaml (or a
custom --fixtures-dir, e.g. the tests/ synthetic fixtures) against a COMPOSED
prompt tree (compose_prompt.py), for a given profiles directory. This is what
makes the gate behavioural rather than merely structural: it checks the exact
bytes a role would run with — required sections present, forbidden content
absent, a token-budget ceiling — not just that a fixture YAML parses.

Two ways to run it:

  1. Single-tree pass/fail — compose --profiles-dir and check every fixture.
     Useful standalone, or as a fast pre-check.
  2. Diff mode (the actual gate) — pass --compare-profiles-dir pointing at a
     base-ref checkout of agents/profiles/, and this prints a pass/fail delta:
     a fixture that PASSED on base and FAILS on candidate is a REGRESSION and
     always exits non-zero; a pre-existing failure that still fails is
     reported but does not, by itself, newly block; an already-failing
     fixture that now passes is an IMPROVEMENT, noted but never blocking.

Usage:
  # Compose --profiles-dir fresh, then check every fixture.
  python3 scripts/replay-gate/prompt_contract_check.py \\
      --profiles-dir agents/profiles --label candidate

  # Compare two composed trees (base ref vs candidate) and print a
  # pass/fail delta table; exit 1 on any regression.
  python3 scripts/replay-gate/prompt_contract_check.py \\
      --profiles-dir agents/profiles --label candidate \\
      --compare-profiles-dir /tmp/base-profiles --compare-label base

  # JUnit XML for CI upload
  python3 scripts/replay-gate/prompt_contract_check.py \\
      --profiles-dir agents/profiles --label candidate --junit results/prompt-contract.xml

  # Prove the gate actually catches a regression before trusting a pass
  python3 scripts/replay-gate/prompt_contract_check.py --self-test

Exit codes: 0 all checks passed / no regression, 1 one or more failed or a
regression was detected, 2 setup error.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compose_prompt  # noqa: E402

DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
DEFAULT_PROFILES_DIR = compose_prompt.DEFAULT_PROFILES_DIR


@dataclass
class ContractResult:
    fixture_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)


def load_contract_fixtures(fixtures_dir: Path) -> list[dict]:
    out = []
    for path in sorted(fixtures_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SystemExit(f"setup error: {path} did not parse to a mapping")
        if "prompt_contract" in raw:
            out.append(raw)
    return out


def _resolve_agents(spec: list[str] | str, profiles_dir: Path) -> list[str]:
    if spec == "all":
        return compose_prompt.parse_roster(profiles_dir)
    if isinstance(spec, list):
        return spec
    raise SystemExit(f"setup error: prompt_contract.agents must be a list or 'all', got {spec!r}")


def check_prompt_contract(fx: dict, profiles_dir: Path, cache: dict[str, dict]) -> list[str]:
    failures: list[str] = []
    pc = fx.get("prompt_contract")
    if not pc:
        return failures

    agents = _resolve_agents(pc.get("agents") or [], profiles_dir)
    must_contain = pc.get("must_contain") or []
    must_not_contain = pc.get("must_not_contain") or []
    max_chars_per_4 = pc.get("max_chars_per_4_estimate")

    for agent in agents:
        cache_key = f"{profiles_dir}:{agent}"
        if cache_key not in cache:
            try:
                cache[cache_key] = compose_prompt.compose(agent, profiles_dir)
            except compose_prompt.CompositionError as e:
                cache[cache_key] = {"error": str(e)}

        entry = cache[cache_key]
        if "error" in entry:
            failures.append(f"{agent}: composition failed: {entry['error']}")
            continue

        text = entry["composed_text"]
        for pattern in must_contain:
            if not re.search(pattern, text):
                failures.append(f"{agent}: composed prompt missing required pattern /{pattern}/")
        for pattern in must_not_contain:
            m = re.search(pattern, text)
            if m:
                failures.append(f"{agent}: composed prompt matches forbidden pattern /{pattern}/: {m.group(0)!r}")
        if max_chars_per_4 is not None:
            count = entry["tokens"]["chars_per_4_estimate"]["count"]
            if count > max_chars_per_4:
                failures.append(
                    f"{agent}: chars/4 token ESTIMATE {count} exceeds budget ceiling {max_chars_per_4}"
                )

    return failures


def run(fixtures: list[dict], profiles_dir: Path) -> list[ContractResult]:
    cache: dict[str, dict] = {}
    results = []
    for fx in fixtures:
        failures = check_prompt_contract(fx, profiles_dir, cache)
        results.append(ContractResult(
            fixture_id=fx.get("fixture_id", "?"),
            passed=not failures,
            failures=failures,
        ))
    return results


def write_junit_xml(results: list[ContractResult], path: str) -> None:
    failures = sum(1 for r in results if not r.passed)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name="prompt-contract" tests="{len(results)}" failures="{failures}">',
    ]
    for r in results:
        lines.append(f'  <testcase name="{escape(r.fixture_id)}">')
        if not r.passed:
            msg = escape("; ".join(r.failures)[:1000])
            lines.append(f'    <failure message="{msg}"/>')
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def print_results(results: list[ContractResult], label: str) -> bool:
    print(f"\n=== prompt-contract check: {label} ===")
    all_passed = True
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"  {mark}  {r.fixture_id}")
        for f in r.failures:
            print(f"        - {f}")
        all_passed = all_passed and r.passed
    print(f"\n{sum(1 for r in results if r.passed)}/{len(results)} passed ({label})")
    return all_passed


def print_delta(base: list[ContractResult], candidate: list[ContractResult]) -> bool:
    """Prints a pass/fail delta table. Returns True if candidate introduces
    no NEW failure relative to base (a pre-existing failure that also fails
    on base is flagged FAILING but does not, by itself, newly block; a
    fixture that passed on base and fails on candidate is a REGRESSION and
    always blocks; a fixture that failed on base and now passes is an
    IMPROVEMENT)."""
    by_id_base = {r.fixture_id: r for r in base}
    by_id_candidate = {r.fixture_id: r for r in candidate}
    all_ids = sorted(set(by_id_base) | set(by_id_candidate))

    print("\n=== pass/fail delta (base -> candidate) ===")
    print(f"{'fixture':<55} {'base':<8} {'candidate':<10} verdict")
    ok = True
    for fid in all_ids:
        b = by_id_base.get(fid)
        c = by_id_candidate.get(fid)
        b_mark = "PASS" if (b and b.passed) else ("FAIL" if b else "n/a")
        c_mark = "PASS" if (c and c.passed) else ("FAIL" if c else "n/a")
        if b and c and b.passed and not c.passed:
            verdict = "REGRESSION"
            ok = False
        elif b and c and not b.passed and c.passed:
            verdict = "IMPROVEMENT"
        elif c and not c.passed:
            verdict = "FAILING"
            ok = False
        else:
            verdict = "ok"
        print(f"{fid:<55} {b_mark:<8} {c_mark:<10} {verdict}")
    return ok


# ---------------------------------------------------------------------------
# --self-test: prove the gate actually detects a seeded regression before
# trusting a pass. Mirrors the house convention in scripts/validate_vendored_config.py
# and scripts/seed-keyvault.sh --self-check.
# ---------------------------------------------------------------------------

_GOOD_PROFILE = """---
role: widget
voice_id: ""
color:    "#000000"
emoji:    "🔧"
vibe:     "self-test fixture"
---

<!-- scope-guard:start -->
# Scope guard - READ THIS FIRST
## Hard rule
Bounce off-lane work.
<!-- scope-guard:end -->

# Completing an issue (disposition protocol)

No silent terminal states. missing_disposition. plan_only.
"""

_BROKEN_PROFILE = """---
role: widget
voice_id: ""
color:    "#000000"
emoji:    "🔧"
vibe:     "self-test fixture, disposition section removed"
---

<!-- scope-guard:start -->
# Scope guard - READ THIS FIRST
## Hard rule
Bounce off-lane work.
<!-- scope-guard:end -->

# (disposition protocol section deliberately deleted for this self-test)
"""

_SELF_TEST_FIXTURE = {
    "fixture_id": "self-test-disposition-protocol",
    "prompt_contract": {
        "agents": ["widget"],
        "must_contain": [r"# Completing an issue \(disposition protocol\)"],
    },
}


def self_test() -> int:
    print("=== self-test: seed a regression and confirm the gate catches it ===")
    tmp = Path(tempfile.mkdtemp(prefix="replay-gate-selftest-"))
    try:
        base_dir = tmp / "base"
        candidate_dir = tmp / "candidate"
        base_dir.mkdir()
        candidate_dir.mkdir()
        (base_dir / "widget.AGENTS.md").write_text(_GOOD_PROFILE, encoding="utf-8")
        (candidate_dir / "widget.AGENTS.md").write_text(_BROKEN_PROFILE, encoding="utf-8")
        (base_dir / "widget.yaml").write_text(
            "name: Widget\nrole: widget\ndescription: self-test fixture role.\n"
            "model_tier: economy\ntoolsets: [file]\nreports_to: null\n",
            encoding="utf-8",
        )

        fixtures = [_SELF_TEST_FIXTURE]
        base_results = run(fixtures, base_dir)
        candidate_results = run(fixtures, candidate_dir)

        base_ok = print_results(base_results, "self-test base (should PASS)")
        print_results(candidate_results, "self-test candidate (should FAIL)")
        delta_ok = print_delta(base_results, candidate_results)

        if not base_ok:
            print("\nSELF-TEST FAILED: the known-good base fixture did not pass — "
                  "the checker itself is broken.", file=sys.stderr)
            return 1
        if delta_ok:
            print("\nSELF-TEST FAILED: the gate did not detect the seeded regression "
                  "(disposition-protocol section removed) — it would have let a real "
                  "regression through.", file=sys.stderr)
            return 1

        print("\nSELF-TEST PASSED: base passed, candidate failed, delta reported REGRESSION.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profiles-dir", help="the candidate agents/profiles/ tree to compose and check")
    ap.add_argument("--label", default="candidate")
    ap.add_argument("--compare-profiles-dir", help="a second (e.g. base-ref) profiles/ tree to diff against")
    ap.add_argument("--compare-label", default="base")
    ap.add_argument("--fixtures-dir", default=str(DEFAULT_FIXTURES_DIR))
    ap.add_argument("--junit", metavar="PATH")
    ap.add_argument("--self-test", action="store_true",
                     help="prove the gate detects a seeded regression, then exit (ignores other flags)")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if not args.profiles_dir:
        print("error: --profiles-dir is required (or use --self-test)", file=sys.stderr)
        return 2

    fixtures = load_contract_fixtures(Path(args.fixtures_dir))
    if not fixtures:
        print(f"no prompt_contract fixtures found in {args.fixtures_dir}", file=sys.stderr)
        return 2

    candidate_results = run(fixtures, Path(args.profiles_dir))
    candidate_ok = print_results(candidate_results, args.label)

    overall_ok = candidate_ok
    if args.compare_profiles_dir:
        base_results = run(fixtures, Path(args.compare_profiles_dir))
        print_results(base_results, args.compare_label)
        overall_ok = print_delta(base_results, candidate_results)

    if args.junit:
        write_junit_xml(candidate_results, args.junit)
        print(f"\nJUnit XML written to {args.junit}")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
