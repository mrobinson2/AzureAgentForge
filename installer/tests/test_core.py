"""Offline tests for the Forge Console core — no az, terraform, or network."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from installer import core  # noqa: E402


# ---------------------------------------------------------------------------
# Config validation + rendering
# ---------------------------------------------------------------------------

GOOD = dict(subscription_id="12345678-1234-1234-1234-123456789abc")


def test_valid_config_passes():
    assert core.DeployConfig(**GOOD).validate() == []


@pytest.mark.parametrize("field,value,fragment", [
    ("subscription_id", "not-a-guid", "GUID"),
    ("location", "East US!", "region"),
    ("environment", "Prod-1", "environment"),
    ("profile", "cheap", "profile"),
    ("ai_foundry_endpoint", "http://insecure.example.com", "https"),
    ("owner_email", 'evil"injection', "email"),
])
def test_invalid_config_rejected(field, value, fragment):
    cfg = core.DeployConfig(**{**GOOD, field: value})
    errs = cfg.validate()
    assert errs and any(fragment in e for e in errs)


def _kv(out: str) -> dict:
    """Parse rendered tfvars into {key: raw_value} ignoring fmt alignment."""
    pairs = {}
    for line in out.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            pairs[k.strip()] = v.strip()
    return pairs


def test_render_tfvars_minimal():
    out = core.render_tfvars(core.DeployConfig(**GOOD))
    kv = _kv(out)
    assert kv["subscription_id"] == '"12345678-1234-1234-1234-123456789abc"'
    assert kv["telegram_enabled"] == "false"
    assert "ai_foundry_endpoint" not in kv  # optional, unset


def test_render_tfvars_optional_fields_and_quoting():
    cfg = core.DeployConfig(**GOOD, ai_foundry_endpoint="https://x.example.com/",
                            owner_email="ops@example.com", telegram_enabled=True)
    kv = _kv(core.render_tfvars(cfg))
    assert kv["ai_foundry_endpoint"] == '"https://x.example.com/"'
    assert kv["telegram_enabled"] == "true"


def test_keyvault_admin_object_ids_valid_guid_passes():
    cfg = core.DeployConfig(**GOOD,
                            keyvault_admin_object_ids=["d4c41ac3-986d-4f52-95f4-22ca268cd058"])
    assert cfg.validate() == []


def test_keyvault_admin_object_ids_invalid_rejected():
    cfg = core.DeployConfig(**GOOD, keyvault_admin_object_ids=["not-a-guid"])
    errs = cfg.validate()
    assert errs and any("keyvault_admin_object_ids" in e for e in errs)


def test_render_tfvars_keyvault_admin_object_ids_as_hcl_list():
    cfg = core.DeployConfig(**GOOD, keyvault_admin_object_ids=[
        "d4c41ac3-986d-4f52-95f4-22ca268cd058",
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"])
    kv = _kv(core.render_tfvars(cfg))
    assert kv["keyvault_admin_object_ids"] == (
        '["d4c41ac3-986d-4f52-95f4-22ca268cd058", '
        '"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]')


def test_render_tfvars_omits_empty_keyvault_admin_object_ids():
    assert "keyvault_admin_object_ids" not in _kv(core.render_tfvars(core.DeployConfig(**GOOD)))


def test_render_tfvars_is_fmt_aligned():
    out = core.render_tfvars(core.DeployConfig(**GOOD))
    eq_cols = {line.index("=") for line in out.splitlines()
               if "=" in line and not line.startswith("#")}
    assert len(eq_cols) == 1, "all '=' must align for terraform fmt"


def test_write_config_creates_tfvars_and_backend_override(tmp_path):
    written = core.write_config(core.DeployConfig(**GOOD), tf_dir=tmp_path)
    assert (tmp_path / "terraform.tfvars").exists()
    backend = (tmp_path / "backend_override.tf").read_text()
    assert 'backend "local"' in backend
    assert written["profile_file"].endswith("cost-optimized.tfvars")


def test_write_config_refuses_invalid(tmp_path):
    with pytest.raises(ValueError):
        core.write_config(core.DeployConfig(subscription_id="nope"), tf_dir=tmp_path)
    assert not (tmp_path / "terraform.tfvars").exists()


# ---------------------------------------------------------------------------
# Step command allowlist
# ---------------------------------------------------------------------------

def test_known_steps_build_expected_commands():
    plan = core.build_step_command("plan", "cost-optimized")
    assert plan[0] == "terraform" and "-out=tfplan" in plan
    assert any(a.startswith("-var-file=") and a.endswith("cost-optimized.tfvars") for a in plan)
    apply_ = core.build_step_command("apply", "hardened")
    assert apply_[-1] == "tfplan"          # apply only ever runs the saved plan
    assert "-auto-approve" not in apply_


def test_unknown_step_rejected():
    with pytest.raises(ValueError):
        core.build_step_command("rm -rf /", "cost-optimized")


def test_dangerous_steps_flagged():
    assert "apply" in core.DANGEROUS_STEPS
    assert "destroy" in core.DANGEROUS_STEPS
    assert "plan" not in core.DANGEROUS_STEPS


# ---------------------------------------------------------------------------
# Destroy-aware apply gate — plan_has_destroy
# ---------------------------------------------------------------------------

def _plan(*action_lists):
    """Build a minimal `terraform show -json` plan with the given action sets."""
    return {"resource_changes": [
        {"address": f"azurerm_thing.r{i}", "change": {"actions": acts}}
        for i, acts in enumerate(action_lists)
    ]}


def test_plan_has_destroy_create_only():
    has, addrs = core.plan_has_destroy(_plan(["create"], ["create"]))
    assert has is False and addrs == []


def test_plan_has_destroy_update_only():
    has, addrs = core.plan_has_destroy(_plan(["update"], ["no-op"], ["read"]))
    assert has is False and addrs == []


def test_plan_has_destroy_pure_delete():
    has, addrs = core.plan_has_destroy(_plan(["create"], ["delete"]))
    assert has is True and addrs == ["azurerm_thing.r1"]


@pytest.mark.parametrize("replace", [["delete", "create"], ["create", "delete"]])
def test_plan_has_destroy_replace_pair(replace):
    has, addrs = core.plan_has_destroy(_plan(["update"], replace))
    assert has is True and addrs == ["azurerm_thing.r1"]


def test_plan_has_destroy_collects_all_addresses():
    has, addrs = core.plan_has_destroy(_plan(["delete"], ["create"], ["delete", "create"]))
    assert has is True
    assert addrs == ["azurerm_thing.r0", "azurerm_thing.r2"]


def test_plan_has_destroy_tolerates_empty_and_malformed():
    assert core.plan_has_destroy({}) == (False, [])
    assert core.plan_has_destroy({"resource_changes": None}) == (False, [])
    assert core.plan_has_destroy({"resource_changes": [{}]}) == (False, [])


# ---------------------------------------------------------------------------
# Runner — streaming, status, one-at-a-time
# ---------------------------------------------------------------------------

def _wait(runner, timeout=10.0):
    deadline = time.time() + timeout
    while runner.busy() and time.time() < deadline:
        time.sleep(0.02)
    assert not runner.busy(), "runner did not finish in time"


def test_runner_captures_output_and_status(tmp_path):
    r = core.Runner()
    r.start("echo-test", [sys.executable, "-c", "print('hello console')"], cwd=tmp_path)
    _wait(r)
    run = r.history[-1]
    assert run.status == "succeeded" and run.returncode == 0
    assert any("hello console" in ln for ln in run.lines)


def test_runner_reports_failure(tmp_path):
    r = core.Runner()
    r.start("fail-test", [sys.executable, "-c", "import sys; sys.exit(3)"], cwd=tmp_path)
    _wait(r)
    assert r.history[-1].status == "failed"
    assert r.history[-1].returncode == 3


def test_runner_rejects_concurrent_steps(tmp_path):
    r = core.Runner()
    r.start("slow", [sys.executable, "-c", "import time; time.sleep(1.5)"], cwd=tmp_path)
    with pytest.raises(RuntimeError):
        r.start("second", [sys.executable, "-c", "print('no')"], cwd=tmp_path)
    _wait(r)


def test_runner_handles_missing_binary(tmp_path):
    r = core.Runner()
    r.start("ghost", ["definitely-not-a-real-binary-xyz"], cwd=tmp_path)
    _wait(r)
    assert r.history[-1].status == "failed"


# ---------------------------------------------------------------------------
# API layer — guards (uses FastAPI TestClient when available)
# ---------------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from installer import app as forge_app  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(forge_app.app)


def _h():
    return {"x-forge-token": forge_app.SESSION_TOKEN}


def test_api_requires_token(client):
    assert client.get("/api/checks").status_code == 401
    assert client.post("/api/run", json={"step": "plan"}).status_code == 401


def test_api_rejects_cross_origin(client):
    r = client.get("/api/state", headers={**_h(), "origin": "https://evil.example"})
    assert r.status_code == 403


def test_bootstrap_requires_url_token(client):
    assert client.get("/api/bootstrap").status_code == 401
    ok = client.get(f"/api/bootstrap?token={forge_app.SESSION_TOKEN}")
    assert ok.status_code == 200 and ok.json()["profiles"]


def test_run_requires_config_first(client):
    forge_app._last_config.clear()
    r = client.post("/api/run", json={"step": "plan"}, headers=_h())
    assert r.status_code == 409


def test_apply_requires_typed_confirmation(client):
    forge_app._last_config.update({"profile": "cost-optimized", "environment": "dev"})
    r = client.post("/api/run", json={"step": "apply", "confirm": "wrong"}, headers=_h())
    assert r.status_code == 428
    forge_app._last_config.clear()


def test_unknown_step_rejected_by_api(client):
    forge_app._last_config.update({"profile": "cost-optimized", "environment": "dev"})
    r = client.post("/api/run", json={"step": "weird"}, headers=_h())
    assert r.status_code == 422
    forge_app._last_config.clear()


def test_config_endpoint_preview(client):
    r = client.post("/api/config", headers=_h(), json={
        "subscription_id": GOOD["subscription_id"], "preview_only": True})
    assert r.status_code == 200
    assert r.json()["written"] is False
    assert "subscription_id" in r.json()["preview"]


# ---------------------------------------------------------------------------
# API layer — destroy-aware apply gate (no terraform; plan inspection stubbed)
# ---------------------------------------------------------------------------

def _stub_runner_start(monkeypatch):
    """Replace the real subprocess Runner.start with a no-op that records the step."""
    started = {}
    monkeypatch.setattr(forge_app.runner, "start",
                        lambda step, cmd, **k: (started.setdefault("step", step),
                                                core.StepRun(step=step))[1])
    return started


def test_apply_blocked_when_plan_destroys(client, monkeypatch):
    forge_app._last_config.update({"profile": "cost-optimized", "environment": "dev"})
    monkeypatch.setattr(core, "inspect_saved_plan",
                        lambda *a, **k: {"has_destroy": True, "destroyed": ["azurerm_thing.r0"]})
    # Env name is correct, but the destructive approval token is missing -> blocked.
    r = client.post("/api/run", json={"step": "apply", "confirm": "dev"}, headers=_h())
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "destroy_approval_required"
    assert "azurerm_thing.r0" in detail["destroyed"]
    forge_app._last_config.clear()


def test_apply_proceeds_when_plan_has_no_destroy(client, monkeypatch):
    forge_app._last_config.update({"profile": "cost-optimized", "environment": "dev"})
    monkeypatch.setattr(core, "inspect_saved_plan",
                        lambda *a, **k: {"has_destroy": False, "destroyed": []})
    started = _stub_runner_start(monkeypatch)
    r = client.post("/api/run", json={"step": "apply", "confirm": "dev"}, headers=_h())
    assert r.status_code == 200 and started["step"] == "apply"
    forge_app._last_config.clear()


def test_apply_proceeds_with_destroy_approval_token(client, monkeypatch):
    forge_app._last_config.update({"profile": "cost-optimized", "environment": "dev"})
    monkeypatch.setattr(core, "inspect_saved_plan",
                        lambda *a, **k: {"has_destroy": True, "destroyed": ["azurerm_thing.r0"]})
    started = _stub_runner_start(monkeypatch)
    r = client.post("/api/run", json={
        "step": "apply", "confirm": "dev",
        "approve_destroy": core.DESTROY_APPROVAL_TOKEN}, headers=_h())
    assert r.status_code == 200 and started["step"] == "apply"
    forge_app._last_config.clear()


def test_apply_still_requires_env_name_even_with_destroy_token(client, monkeypatch):
    """The destroy approval is additive — it never replaces the env-name confirm."""
    forge_app._last_config.update({"profile": "cost-optimized", "environment": "dev"})
    monkeypatch.setattr(core, "inspect_saved_plan",
                        lambda *a, **k: {"has_destroy": True, "destroyed": ["azurerm_thing.r0"]})
    r = client.post("/api/run", json={
        "step": "apply", "confirm": "wrong",
        "approve_destroy": core.DESTROY_APPROVAL_TOKEN}, headers=_h())
    assert r.status_code == 428  # env-name confirm fails first
    forge_app._last_config.clear()


def test_plan_summary_endpoint(client, monkeypatch):
    forge_app._last_config.update({"profile": "cost-optimized", "environment": "dev"})
    monkeypatch.setattr(core, "inspect_saved_plan",
                        lambda *a, **k: {"has_destroy": True, "destroyed": ["azurerm_thing.r0"]})
    r = client.get("/api/plan-summary", headers=_h())
    assert r.status_code == 200 and r.json()["has_destroy"] is True
    forge_app._last_config.clear()
