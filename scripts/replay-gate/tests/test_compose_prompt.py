"""Tests for scripts/replay-gate/compose_prompt.py.

Focus: composition failure modes must fail LOUDLY (a specific
CompositionError, never a silent no-op that ships broken bytes), plus the
manifest/token-estimate shape and the real-roster integration path.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import compose_prompt
from conftest import GOOD_BODY, GOOD_FRONTMATTER, write_profile

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = Path(__file__).resolve().parent.parent / "compose_prompt.py"
REAL_PROFILES_DIR = REPO_ROOT / "agents" / "profiles"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_compose_well_formed_profile(profiles_dir):
    write_profile(profiles_dir, "widget")
    m = compose_prompt.compose("widget", profiles_dir)
    assert m["role"] == "widget"
    assert "# Identity" in m["composed_text"]
    # frontmatter must be gone from the composed bytes
    assert "voice_id" not in m["composed_text"]
    assert m["frontmatter"]["role"] == "widget"


def test_composed_manifest_shape(profiles_dir):
    write_profile(profiles_dir, "widget")
    m = compose_prompt.compose("widget", profiles_dir)
    assert set(m["composed"]) == {"bytes", "sha256", "version"}
    assert m["composed"]["bytes"] == len(m["composed_text"].encode("utf-8"))
    assert len(m["composed"]["sha256"]) == 64
    assert m["composed"]["version"] == m["composed"]["sha256"][:12]
    tokens = m["tokens"]
    assert tokens["chars_per_4_estimate"]["count"] == round(len(m["composed_text"]) / 4)
    assert "ESTIMATE" in tokens["chars_per_4_estimate"]["note"]
    # cl100k_base_estimate always present as a key, count may be None if
    # tiktoken isn't installed — either way it must be labeled.
    assert "ESTIMATE" in tokens["cl100k_base_estimate"]["note"] or tokens["cl100k_base_estimate"]["count"] is None


def test_compose_is_deterministic(profiles_dir):
    write_profile(profiles_dir, "widget")
    m1 = compose_prompt.compose("widget", profiles_dir)
    m2 = compose_prompt.compose("widget", profiles_dir)
    assert m1["composed"]["sha256"] == m2["composed"]["sha256"]


# ---------------------------------------------------------------------------
# Loud failure modes — the whole point of this module
# ---------------------------------------------------------------------------

def test_missing_role_file_fails_loudly(profiles_dir):
    with pytest.raises(compose_prompt.CompositionError, match="no prompt file for role"):
        compose_prompt.compose("nonexistent", profiles_dir)


def test_missing_frontmatter_entirely_fails_loudly(profiles_dir):
    path = profiles_dir / "widget.AGENTS.md"
    path.write_text("# Just a heading, no frontmatter at all\n", encoding="utf-8")
    (profiles_dir / "widget.yaml").write_text(
        "name: Widget\nrole: widget\ndescription: x\nmodel_tier: economy\ntoolsets: [file]\nreports_to: null\n"
    )
    with pytest.raises(compose_prompt.CompositionError, match="missing YAML frontmatter entirely"):
        compose_prompt.compose("widget", profiles_dir)


def test_misplaced_frontmatter_fails_loudly_and_distinguishes_from_missing(profiles_dir):
    # A leading HTML comment before the '---' fence — exactly the shape that
    # would make a naive \A-anchored regex strip silently no-op in a future
    # deploy script.
    body = (
        "<!-- a leading comment before the fence -->\n"
        + GOOD_FRONTMATTER.format(role="widget")
        + GOOD_BODY
    )
    (profiles_dir / "widget.AGENTS.md").write_text(body, encoding="utf-8")
    (profiles_dir / "widget.yaml").write_text(
        "name: Widget\nrole: widget\ndescription: x\nmodel_tier: economy\ntoolsets: [file]\nreports_to: null\n"
    )
    with pytest.raises(compose_prompt.CompositionError, match="misplaced frontmatter"):
        compose_prompt.compose("widget", profiles_dir)


def test_disallowed_frontmatter_key_fails_loudly(profiles_dir):
    frontmatter = (
        '---\nrole: widget\nvoice_id: ""\ncolor: "#123456"\nemoji: "x"\n'
        'vibe: "x"\nmodel: gpt-4\n---\n'
    )
    write_profile(profiles_dir, "widget", frontmatter=frontmatter)
    with pytest.raises(compose_prompt.CompositionError, match="disallowed key"):
        compose_prompt.compose("widget", profiles_dir)


def test_missing_required_frontmatter_key_fails_loudly(profiles_dir):
    frontmatter = '---\nvoice_id: ""\ncolor: "#123456"\nemoji: "x"\nvibe: "x"\n---\n'
    write_profile(profiles_dir, "widget", frontmatter=frontmatter)
    with pytest.raises(compose_prompt.CompositionError, match="missing required key"):
        compose_prompt.compose("widget", profiles_dir)


def test_unfilled_template_placeholder_role_fails_loudly(profiles_dir):
    # Exactly the shape of an un-filled agents/templates/AGENTS.template.md
    # frontmatter left in place by accident.
    frontmatter = '---\nrole: <role-slug>\nvoice_id: ""\ncolor: "#<hex>"\nemoji: "x"\nvibe: "x"\n---\n'
    path = profiles_dir / "widget.AGENTS.md"
    path.write_text(frontmatter + GOOD_BODY, encoding="utf-8")
    with pytest.raises(compose_prompt.CompositionError, match="not a valid slug"):
        compose_prompt.compose("widget", profiles_dir)


def test_role_slug_mismatch_fails_loudly(profiles_dir):
    frontmatter = GOOD_FRONTMATTER.format(role="other-role")
    path = profiles_dir / "widget.AGENTS.md"
    path.write_text(frontmatter + GOOD_BODY, encoding="utf-8")
    with pytest.raises(compose_prompt.CompositionError, match="does not match filename slug"):
        compose_prompt.compose("widget", profiles_dir)


def test_malformed_frontmatter_yaml_fails_loudly(profiles_dir):
    frontmatter = "---\nrole: widget\n  bad: [unterminated\n---\n"
    path = profiles_dir / "widget.AGENTS.md"
    path.write_text(frontmatter + GOOD_BODY, encoding="utf-8")
    with pytest.raises(compose_prompt.CompositionError, match="not valid YAML"):
        compose_prompt.compose("widget", profiles_dir)


def test_unresolved_placeholder_token_fails_loudly(profiles_dir):
    body = GOOD_BODY + "\n\nModel: {{MODEL_NAME}}\n"
    write_profile(profiles_dir, "widget", body=body)
    with pytest.raises(compose_prompt.CompositionError, match="unresolved"):
        compose_prompt.compose("widget", profiles_dir)


# ---------------------------------------------------------------------------
# Roster + compose_many
# ---------------------------------------------------------------------------

def test_parse_roster_derives_from_yaml_sidecars(profiles_dir):
    write_profile(profiles_dir, "widget")
    write_profile(profiles_dir, "gadget")
    assert sorted(compose_prompt.parse_roster(profiles_dir)) == ["gadget", "widget"]


def test_parse_roster_empty_dir_fails_loudly(profiles_dir):
    with pytest.raises(compose_prompt.CompositionError, match="no \\*.yaml profiles found"):
        compose_prompt.parse_roster(profiles_dir)


def test_compose_many_reports_errors_without_raising(profiles_dir):
    write_profile(profiles_dir, "widget")
    # "gadget" has a yaml sidecar but no AGENTS.md — the generalist-shaped gap.
    (profiles_dir / "gadget.yaml").write_text(
        "name: Gadget\nrole: gadget\ndescription: x\nmodel_tier: economy\ntoolsets: [file]\nreports_to: null\n"
    )
    manifests, errors = compose_prompt.compose_many(["widget", "gadget"], profiles_dir)
    assert len(manifests) == 1
    assert manifests[0]["role"] == "widget"
    assert len(errors) == 1
    assert "gadget" in errors[0]
    assert "no prompt file" in errors[0]


# ---------------------------------------------------------------------------
# Real-repo integration — pins the current, fixed-up state of agents/profiles/
# ---------------------------------------------------------------------------

def test_real_roster_composes_with_zero_errors():
    """This is the regression test for the generalist.yaml/generalist.AGENTS.md
    gap found while building this gate: every role with a YAML sidecar must
    have a matching AGENTS.md, or compose_many fails loudly instead of
    silently shrinking the roster."""
    roles = compose_prompt.parse_roster(REAL_PROFILES_DIR)
    assert len(roles) == 14, f"expected 14 roles, found {len(roles)}: {roles}"
    manifests, errors = compose_prompt.compose_many(roles, REAL_PROFILES_DIR)
    assert errors == [], f"real roster failed to compose: {errors}"
    assert len(manifests) == 14


def test_real_roster_has_no_unresolved_placeholders():
    roles = compose_prompt.parse_roster(REAL_PROFILES_DIR)
    for role in roles:
        m = compose_prompt.compose(role, REAL_PROFILES_DIR)
        assert "{{" not in m["composed_text"], f"{role}: leftover {{PLACEHOLDER}} in composed text"


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------

def test_cli_help():
    r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    assert "compose" in r.stdout


def test_cli_compose_many_against_real_repo(tmp_path):
    out_dir = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "compose-many", "--out-dir", str(out_dir)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (out_dir / "manifest.json").is_file()
    assert "14 composed, 0 failed" in r.stdout


def test_cli_compose_many_fails_loudly_on_missing_role_file(tmp_path, profiles_dir):
    write_profile(profiles_dir, "widget")
    (profiles_dir / "gadget.yaml").write_text(
        "name: Gadget\nrole: gadget\ndescription: x\nmodel_tier: economy\ntoolsets: [file]\nreports_to: null\n"
    )
    out_dir = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "compose-many", "--profiles-dir", str(profiles_dir), "--out-dir", str(out_dir)],
        capture_output=True, text=True,
    )
    assert r.returncode == 1
    assert "FAILED  gadget" in r.stderr
