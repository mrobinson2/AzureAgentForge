"""Offline tests for the scaffold-cicd Forge Console integration — no az/gh/network."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from installer import core  # noqa: E402


class TestBuildScaffoldCommand:
    def test_preview_has_no_apply_flag(self):
        cmd = core.build_scaffold_command({"repo": "me/proj"})
        assert cmd[0] == "bash"
        assert cmd[1].endswith("scripts/scaffold-cicd.sh")
        assert "--repo" in cmd and "me/proj" in cmd
        assert "--apply" not in cmd

    def test_apply_flag_when_requested(self):
        cmd = core.build_scaffold_command({"repo": "me/proj", "apply": True})
        assert "--apply" in cmd

    def test_string_flags_only_included_when_set(self):
        cmd = core.build_scaffold_command({
            "repo": "me/proj", "subscription": "sub-123", "location": "westus2",
        })
        assert cmd[cmd.index("--subscription") + 1] == "sub-123"
        assert cmd[cmd.index("--location") + 1] == "westus2"
        assert "--registry" not in cmd  # not provided → absent

    def test_bool_flags(self):
        cmd = core.build_scaffold_command({
            "repo": "me/proj", "grant_uaa": True, "skip_github": True,
            "environment_subject": True,
        })
        assert "--grant-uaa" in cmd
        assert "--skip-github" in cmd
        assert "--environment-subject" in cmd

    def test_invalid_repo_rejected(self):
        with pytest.raises(ValueError, match="OWNER/REPO"):
            core.build_scaffold_command({"repo": "not-a-repo"})

    def test_secrets_never_placed_on_command_line(self):
        cmd = core.build_scaffold_command({"repo": "me/proj", "GPT4O_API_KEY": "super-secret"})
        assert "super-secret" not in cmd
        assert "--GPT4O_API_KEY" not in cmd


import time


def _wait_done(run, timeout=10.0):
    deadline = time.time() + timeout
    while run.status == "running" and time.time() < deadline:
        time.sleep(0.02)
    return run


class TestRunnerEnv:
    def test_env_reaches_subprocess(self):
        runner = core.Runner()
        run = runner.start(
            "scaffold",
            ["sh", "-c", 'printf %s "$FORGE_SCAFFOLD_TEST"'],
            env={"FORGE_SCAFFOLD_TEST": "envvalue"},
        )
        _wait_done(run)
        assert run.status == "succeeded"
        assert any("envvalue" in line for line in run.lines)

    def test_no_env_still_runs(self):
        runner = core.Runner()
        run = runner.start("scaffold", ["sh", "-c", "printf ok"])
        _wait_done(run)
        assert run.status == "succeeded"
        assert any("ok" in line for line in run.lines)
