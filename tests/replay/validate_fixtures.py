#!/usr/bin/env python3
"""Static validator for the golden replay fixtures in tests/replay/fixtures/.

Run:  python tests/replay/validate_fixtures.py
      python tests/replay/validate_fixtures.py tests/replay/fixtures/19-*.yaml

For every fixture it confirms:
  - the YAML parses,
  - fixture_id matches the filename,
  - children.count is an integer >= 0,
  - every agent referenced (by_agent keys, exactly_one_per_agent,
    forbidden_agents) is a known role slug from agents/profiles/,
  - every regex pattern compiles under the replay runner's compile
    semantics (a leading inline (?i...) flag group is stripped and the
    i/m flags re-applied), across:
      title_must_match, description_must_match_all,
      description_must_not_match, summary_comment_must_reference,
      accepted_tool_patterns, forbidden_in_trace.

These fixtures are executable contracts for the agent Orchestrator: each
describes a request and asserts exactly how it must be routed, refused,
or answered. The live replay runner that drives them needs a deployed
platform; this validator checks the fixtures themselves stay well-formed
and in sync with the role model. Exit code is non-zero if anything fails.
"""
import glob
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIR = os.path.join(HERE, "fixtures")
PROFILE_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "agents", "profiles"))

# Inline-flag prefix the runner strips, mirroring compileRegex():
#   /^\(\?([imsux]+)\)(.*)/  ->  new RegExp(body, flags without s/u/x)
_INLINE = re.compile(r"^\(\?([imsux]+)\)(.*)$", re.S)
_PY_FLAG = {"i": re.I, "m": re.M, "s": re.S, "x": re.X}


def compile_like_runner(pattern):
    """Compile a pattern the way the runner does (strip a leading inline
    flag group, re-apply i/m as real flags)."""
    text = str(pattern)
    m = _INLINE.match(text)
    flags = 0
    if m:
        for ch in m.group(1):
            if ch in ("i", "m"):  # runner keeps only i/m
                flags |= _PY_FLAG[ch]
        text = m.group(2)
    return re.compile(text, flags)


def known_roles():
    roles = set()
    for fp in glob.glob(os.path.join(PROFILE_DIR, "*.yaml")):
        try:
            data = yaml.safe_load(open(fp))
            if isinstance(data, dict) and data.get("role"):
                roles.add(str(data["role"]))
        except Exception:
            pass
    return roles


def collect_regexes(fx):
    pats = []
    by_agent = (((fx or {}).get("expected") or {}).get("children") or {}).get("by_agent") or {}
    for child in by_agent.values():
        if not isinstance(child, dict):
            continue
        if child.get("title_must_match"):
            pats.append(child["title_must_match"])
        for key in ("description_must_match_all", "description_must_not_match"):
            v = child.get(key)
            if isinstance(v, list):
                pats.extend(v)
    parent = ((fx or {}).get("expected") or {}).get("parent") or {}
    if isinstance(parent.get("summary_comment_must_reference"), list):
        pats.extend(parent["summary_comment_must_reference"])
    trace = (fx or {}).get("trace_assertions") or {}
    for key in ("accepted_tool_patterns", "forbidden_in_trace"):
        if isinstance(trace.get(key), list):
            pats.extend(trace[key])
    return pats


def collect_agents(fx):
    """Every role slug the fixture references."""
    agents = set()
    children = ((fx or {}).get("expected") or {}).get("children") or {}
    by_agent = children.get("by_agent") or {}
    agents.update(by_agent.keys())
    for key in ("exactly_one_per_agent", "forbidden_agents"):
        v = children.get(key)
        if isinstance(v, list):
            agents.update(v)
    deps = ((fx or {}).get("expected") or {}).get("dependencies") or {}
    for k, v in deps.items():
        if isinstance(v, list):
            agents.update(v)
    return {str(a) for a in agents}


def validate_one(fp, roles):
    errors = []
    base = os.path.basename(fp)[: -len(".yaml")]
    try:
        fx = yaml.safe_load(open(fp))
    except Exception as e:
        return [f"YAML parse error: {e}"], 0
    if not isinstance(fx, dict):
        return ["top-level YAML is not a mapping"], 0

    if fx.get("fixture_id") != base:
        errors.append(f"fixture_id '{fx.get('fixture_id')}' != filename '{base}'")

    count = (((fx.get("expected") or {}).get("children")) or {}).get("count")
    if not isinstance(count, int) or count < 0:
        errors.append(f"children.count must be int >= 0 (got {count!r})")

    if roles:
        for agent in collect_agents(fx):
            if agent not in roles:
                errors.append(f"unknown role slug referenced: '{agent}'")

    pats = collect_regexes(fx)
    for p in pats:
        try:
            compile_like_runner(p)
        except re.error as e:
            errors.append(f"regex does not compile: {p!r} ({e})")
    return errors, len(pats)


def main():
    roles = known_roles()
    targets = sys.argv[1:] or sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.yaml")))
    if not targets:
        print("no fixtures found", file=sys.stderr)
        return 1
    total_pats = 0
    failed = 0
    for fp in targets:
        errs, npat = validate_one(fp, roles)
        total_pats += npat
        name = os.path.basename(fp)
        if errs:
            failed += 1
            print(f"FAIL  {name}")
            for e in errs:
                print(f"        - {e}")
        else:
            print(f"OK    {name}  ({npat} regex patterns)")
    print()
    if failed:
        print(f"FAILED: {failed}/{len(targets)} fixtures invalid.")
        return 1
    print(f"OK: {len(targets)} fixtures valid, {total_pats} regex patterns compiled, "
          f"{len(roles)} known roles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
