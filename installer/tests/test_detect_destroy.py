"""Tests for the deploy-pipeline destroy detector.

The verdict logic itself is covered in test_core.py; here we test the CLI
wrapper's contract with GitHub Actions: it must write the right
``has_destroy`` output, list destroyed addresses, fail SAFE on an unreadable
plan, and never exit non-zero (gating is by job ``if:``, not exit code).
"""

import json

from installer import detect_destroy


def _plan(*action_lists):
    """Build a minimal `terraform show -json` plan with the given action sets."""
    return {"resource_changes": [
        {"address": f"azurerm_thing.r{i}", "change": {"actions": acts}}
        for i, acts in enumerate(action_lists)
    ]}


def _run(tmp_path, monkeypatch, plan):
    out = tmp_path / "gh_output"
    out.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    rc = detect_destroy.main(["detect_destroy", str(plan_path)])
    return rc, out.read_text()


def test_non_destructive_plan_auto_applies(tmp_path, monkeypatch):
    rc, output = _run(tmp_path, monkeypatch, _plan(["create"], ["update"], ["no-op"]))
    assert rc == 0
    assert "has_destroy=false" in output


def test_pure_delete_is_destructive(tmp_path, monkeypatch):
    rc, output = _run(tmp_path, monkeypatch, _plan(["create"], ["delete"]))
    assert rc == 0
    assert "has_destroy=true" in output
    assert "azurerm_thing.r1" in output


def test_replace_is_destructive(tmp_path, monkeypatch):
    rc, output = _run(tmp_path, monkeypatch, _plan(["delete", "create"]))
    assert rc == 0
    assert "has_destroy=true" in output


def test_unreadable_plan_fails_safe_to_destroy(tmp_path, monkeypatch):
    out = tmp_path / "gh_output"
    out.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    rc = detect_destroy.main(["detect_destroy", str(tmp_path / "does-not-exist.json")])
    assert rc == 0  # never breaks the pipeline
    assert "has_destroy=true" in out.read_text()  # but routes to human review


def test_bad_invocation_is_non_fatal(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert detect_destroy.main(["detect_destroy"]) == 0
