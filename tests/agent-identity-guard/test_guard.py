"""Offline tests for scripts/agent-identity-guard.py."""
import importlib.util, pathlib, subprocess, sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "agent-identity-guard.py"
spec = importlib.util.spec_from_file_location("guard", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def ident(*ras, enabled=True, reason="needs its own auditable identity"):
    return {"azure_identity": {"enabled": enabled, "reason": reason, "role_assignments": list(ras)}}


def ra(role, scope="resource_group", reason="read-only inspection of the platform", **kw):
    return {"role": role, "scope": scope, "reason": reason, **kw}


def test_no_identity_block_passes():
    assert guard.check_profile("coder", {"name": "Coder"}) == []


def test_read_only_reader_passes():
    assert guard.check_profile("infra", ident(ra("Reader"), ra("Key Vault Reader", "key_vault"))) == []


def test_forbidden_roles_are_rejected_at_any_scope():
    for role in ("Owner", "Contributor", "User Access Administrator", "Key Vault Administrator"):
        v = guard.check_profile("x", ident(ra(role, allow_write=True)))
        assert v and "forbidden" in v[0], role


def test_write_role_requires_allow_write_and_reason():
    v = guard.check_profile("x", ident(ra("Storage Blob Data Contributor")))
    assert any("allow_write" in m for m in v)
    v = guard.check_profile("x", ident(ra("Storage Blob Data Contributor", allow_write=True, reason="x")))
    assert any("reason" in m for m in v)
    assert guard.check_profile("x", ident(ra("Storage Blob Data Contributor", allow_write=True,
                                             reason="writes build artifacts to the platform share"))) == []


def test_enabled_without_assignments_is_a_violation():
    v = guard.check_profile("x", ident())
    assert any("role_assignments" in m for m in v)


def test_disabled_identity_is_ignored():
    assert guard.check_profile("x", ident(ra("Owner"), enabled=False)) == []


def test_shipped_profiles_pass_for_real():
    r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "none over-privileged" in r.stdout


def test_cli_fails_on_overprivileged_profile(tmp_path):
    d = tmp_path / "profiles"; d.mkdir()
    (d / "bad.yaml").write_text(yaml.safe_dump({"name": "Bad", **ident(ra("Owner"))}))
    r = subprocess.run([sys.executable, str(SCRIPT), "--profiles-dir", str(d)], capture_output=True, text=True)
    assert r.returncode == 1
    assert "forbidden" in r.stdout
