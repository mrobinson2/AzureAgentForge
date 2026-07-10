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
        sub = "11111111-2222-3333-4444-555555555555"
        cmd = core.build_scaffold_command({
            "repo": "me/proj", "subscription": sub, "location": "westus2",
        })
        assert cmd[cmd.index("--subscription") + 1] == sub
        assert cmd[cmd.index("--location") + 1] == "westus2"
        assert "--registry" not in cmd  # not provided → absent

    def test_arg_smuggling_param_rejected(self):
        # aaf-0022: a value that would be misread as another flag is rejected.
        with pytest.raises(ValueError, match="location"):
            core.build_scaffold_command({"repo": "me/proj", "location": "--apply"})

    def test_invalid_subscription_rejected(self):
        with pytest.raises(ValueError, match="subscription"):
            core.build_scaffold_command({"repo": "me/proj", "subscription": "not-a-guid"})

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
            ["sh", "-c", 'printf %s "$SCAFFOLD_ENV_TEST"'],
            env={"SCAFFOLD_ENV_TEST": "envvalue"},
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


from fastapi.testclient import TestClient

from installer import app as appmod  # noqa: E402


_ORIGIN = f"http://{appmod.HOST}:{appmod.PORT}"


def _client_and_headers():
    # base_url gives a loopback Host (TrustedHostMiddleware, aaf-0021); the Origin
    # header lets state-changing POSTs pass the guard's origin-presence check.
    return (TestClient(appmod.app, base_url=_ORIGIN),
            {"x-forge-token": appmod.SESSION_TOKEN, "origin": _ORIGIN})


class TestScaffoldEndpoint:
    def test_preview_wires_command_and_env(self, monkeypatch):
        captured = {}

        def fake_start(step, cmd, cwd=appmod.core.REPO_ROOT, timeout=None, env=None):
            captured.update(step=step, cmd=cmd, env=env)
            run = appmod.core.StepRun(step=step)
            return run

        monkeypatch.setattr(appmod.runner, "start", fake_start)
        client, headers = _client_and_headers()
        resp = client.post("/api/scaffold", headers=headers, json={
            "params": {"repo": "me/proj"},
            "secrets": {"GPT4O_API_KEY": "super-secret"},
        })
        assert resp.status_code == 200
        assert resp.json()["preview"] is True
        assert "--apply" not in captured["cmd"]
        assert "super-secret" not in captured["cmd"]          # secret not on argv
        assert captured["env"]["GPT4O_API_KEY"] == "super-secret"  # secret via env

    def test_apply_requires_confirmation_token(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("runner.start must not be called without confirmation")

        monkeypatch.setattr(appmod.runner, "start", boom)
        client, headers = _client_and_headers()
        resp = client.post("/api/scaffold", headers=headers, json={
            "params": {"repo": "me/proj"}, "apply": True,
        })
        assert resp.status_code == 428
        assert appmod.SCAFFOLD_APPLY_TOKEN in resp.json()["detail"]

    def test_apply_runs_with_correct_token(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            appmod.runner, "start",
            lambda step, cmd, cwd=appmod.core.REPO_ROOT, timeout=None, env=None:
                (captured.update(cmd=cmd) or appmod.core.StepRun(step=step)),
        )
        client, headers = _client_and_headers()
        resp = client.post("/api/scaffold", headers=headers, json={
            "params": {"repo": "me/proj"}, "apply": True,
            "confirm": appmod.SCAFFOLD_APPLY_TOKEN,
        })
        assert resp.status_code == 200
        assert "--apply" in captured["cmd"]

    def test_invalid_repo_returns_422(self, monkeypatch):
        monkeypatch.setattr(appmod.runner, "start", lambda *a, **k: None)
        client, headers = _client_and_headers()
        resp = client.post("/api/scaffold", headers=headers, json={"params": {"repo": "bad"}})
        assert resp.status_code == 422

    def test_requires_session_token(self):
        client, _ = _client_and_headers()
        # Valid Origin present so this isolates the missing-token check (-> 401).
        resp = client.post("/api/scaffold", json={"params": {"repo": "me/proj"}},
                           headers={"origin": _ORIGIN})
        assert resp.status_code == 401
