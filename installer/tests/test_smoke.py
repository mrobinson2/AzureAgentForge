"""Tests for the post-deploy smoke verdict.

The pure logic (check_app/check_http/evaluate) is exercised directly; the CLI
contract is checked the same way as test_detect_destroy — but note smoke is a
real gate, so it exits non-zero on failure (and fail-closed on an unreadable
payload), unlike the always-exit-0 destroy detector.
"""

import json

from installer import smoke


def _app(name, state="Succeeded", running="Running", show=True):
    if not show:
        return {"name": name}
    return {"name": name, "show": {"properties": {
        "provisioningState": state, "runningStatus": running}}}


# ── pure verdict logic ──────────────────────────────────────────────────────

def test_healthy_app_passes():
    res = smoke.check_app(_app("ca-paperclip-dev"))
    assert res.ok
    assert "Succeeded" in res.detail


def test_unprovisioned_app_fails():
    res = smoke.check_app(_app("ca-x", state="Failed"))
    assert not res.ok
    assert "Failed" in res.detail


def test_bad_running_status_fails():
    res = smoke.check_app(_app("ca-x", running="Stopped"))
    assert not res.ok
    assert "Stopped" in res.detail


def test_missing_show_fails_closed():
    res = smoke.check_app(_app("ca-x", show=False))
    assert not res.ok
    assert "missing" in res.detail.lower() or "unreadable" in res.detail.lower()


def test_flat_show_shape_tolerated():
    # Some `az` shapes put fields at the top level, not under .properties.
    res = smoke.check_app({"name": "ca-x", "show": {"provisioningState": "Succeeded"}})
    assert res.ok


def test_scale_to_zero_running_is_ok():
    # Container Apps that scale to zero still report runningStatus "Running".
    res = smoke.check_app(_app("ca-honcho-dev", running="Running"))
    assert res.ok


def test_http_2xx_3xx_pass_4xx_5xx_fail():
    assert smoke.check_http({"name": "ui", "status": 200}).ok
    assert smoke.check_http({"name": "ui", "status": 302}).ok
    assert not smoke.check_http({"name": "ui", "status": 404}).ok
    assert not smoke.check_http({"name": "ui", "status": 503}).ok
    assert not smoke.check_http({"name": "ui", "status": None}).ok


def test_evaluate_all_healthy():
    payload = {"apps": [_app("ca-a"), _app("ca-b")], "http": [{"name": "ui", "status": 200}]}
    report = smoke.evaluate(payload)
    assert report.ok
    assert len(report.checks) == 3


def test_evaluate_flags_missing_expected_app():
    payload = {"expected": ["ca-a", "ca-missing"], "apps": [_app("ca-a")]}
    report = smoke.evaluate(payload)
    assert not report.ok
    fails = [c for c in report.failures()]
    assert any(c.name == "ca-missing" for c in fails)


def test_evaluate_one_bad_app_fails_whole_report():
    payload = {"apps": [_app("ca-a"), _app("ca-b", state="Failed")]}
    report = smoke.evaluate(payload)
    assert not report.ok


def test_evaluate_empty_is_failure():
    report = smoke.evaluate({})
    assert not report.ok


# ── CLI contract ────────────────────────────────────────────────────────────

def _run_cli(tmp_path, monkeypatch, payload_text):
    out = tmp_path / "gh_output"
    out.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    p = tmp_path / "payload.json"
    p.write_text(payload_text)
    rc = smoke.main(["smoke", str(p)])
    return rc, out.read_text()


def test_cli_passes_on_healthy(tmp_path, monkeypatch):
    payload = json.dumps({"apps": [_app("ca-a")]})
    rc, output = _run_cli(tmp_path, monkeypatch, payload)
    assert rc == 0
    assert "ok=true" in output


def test_cli_fails_nonzero_on_unhealthy(tmp_path, monkeypatch):
    payload = json.dumps({"apps": [_app("ca-a", state="Failed")]})
    rc, output = _run_cli(tmp_path, monkeypatch, payload)
    assert rc == 1
    assert "ok=false" in output


def test_cli_fails_closed_on_unreadable_payload(tmp_path, monkeypatch):
    out = tmp_path / "gh_output"
    out.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    rc = smoke.main(["smoke", str(tmp_path / "nope.json")])
    assert rc == 1
    assert "ok=false" in out.read_text()
