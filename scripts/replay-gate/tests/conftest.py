"""Shared test fixtures for the Behavioural Replay Gate test suite."""
from __future__ import annotations

import sys
from pathlib import Path

# Make compose_prompt / prompt_contract_check importable as plain modules,
# the same way prompt_contract_check.py itself imports compose_prompt.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

GOOD_FRONTMATTER = """---
role: {role}
voice_id: ""
color:    "#123456"
emoji:    "🔧"
vibe:     "synthetic test fixture"
---
"""

GOOD_BODY = """
<!-- scope-guard:start -->
# Scope guard - READ THIS FIRST
## Hard rule
Bounce off-lane work back to Orchestrator.
<!-- scope-guard:end -->

# Identity

You are a synthetic test role.

# Completing an issue (disposition protocol)

No silent terminal states. plan_only. missing_disposition.
"""


def write_profile(profiles_dir: Path, role: str, *, frontmatter: str | None = None, body: str = GOOD_BODY) -> Path:
    """Write a synthetic <role>.AGENTS.md into profiles_dir. Also writes a
    matching minimal <role>.yaml sidecar so parse_roster() finds it."""
    if frontmatter is None:
        frontmatter = GOOD_FRONTMATTER.format(role=role)
    path = profiles_dir / f"{role}.AGENTS.md"
    path.write_text(frontmatter + body, encoding="utf-8")
    yaml_path = profiles_dir / f"{role}.yaml"
    if not yaml_path.exists():
        yaml_path.write_text(
            f"name: {role.title()}\nrole: {role}\n"
            f"description: synthetic test fixture role for the replay-gate test suite.\n"
            f"model_tier: economy\ntoolsets: [file]\nreports_to: null\n",
            encoding="utf-8",
        )
    return path


@pytest.fixture
def profiles_dir(tmp_path: Path) -> Path:
    d = tmp_path / "profiles"
    d.mkdir()
    return d
