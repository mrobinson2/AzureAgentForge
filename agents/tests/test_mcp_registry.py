"""MCP registry + profile grant validation (offline)."""
import json, pathlib, subprocess, sys
import yaml
from jsonschema import Draft202012Validator

AGENTS = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENTS))
import validate_profiles as vp  # noqa: E402


def test_registry_schema_is_valid_and_registry_passes():
    registry, errors = vp.load_registry()
    assert errors == []
    assert "deepwiki" in registry["servers"]
    assert "github" in registry["servers"]


def test_every_registered_server_has_explicit_include_list():
    registry, _ = vp.load_registry()
    for name, srv in registry["servers"].items():
        assert srv["tools"]["include"], f"{name}: tools.include must be an explicit allow-list"
        if srv["risk"] != "read":
            assert "auth" in srv, f"{name}: a write/destructive server must be credential-gated"


def test_secret_gated_server_names_an_mcp_env_and_kv_secret():
    registry, _ = vp.load_registry()
    gh = registry["servers"]["github"]
    assert gh["auth"]["secret_env"] == "MCP_GITHUB_TOKEN"
    assert gh["auth"]["key_vault_secret"] == "mcp-github-token"


def test_registry_rejects_http_without_url(tmp_path):
    schema = json.loads((AGENTS / "mcp-registry.schema.json").read_text())
    bad = {"version": 1, "servers": {"x": {"description": "no url at all here", "owner": "p",
                                            "risk": "read", "transport": "http",
                                            "tools": {"include": ["a"]}}}}
    assert list(Draft202012Validator(schema).iter_errors(bad))


def test_registry_rejects_plain_http_url():
    schema = json.loads((AGENTS / "mcp-registry.schema.json").read_text())
    bad = {"version": 1, "servers": {"x": {"description": "insecure transport here", "owner": "p",
                                            "risk": "read", "transport": "http", "url": "http://x",
                                            "tools": {"include": ["a"]}}}}
    assert list(Draft202012Validator(schema).iter_errors(bad))


def test_profile_schema_accepts_mcp_servers_list():
    schema = json.loads((AGENTS / "profile.schema.json").read_text())
    good = {"name": "Coder", "role": "coder", "description": "Implements code changes.",
            "model_tier": "standard", "toolsets": ["terminal"], "reports_to": "Orchestrator",
            "mcp_servers": ["deepwiki", "github"]}
    assert list(Draft202012Validator(schema).iter_errors(good)) == []


def test_unregistered_grant_is_an_error():
    registry, _ = vp.load_registry()
    errs = vp.grant_errors("x.yaml", {"mcp_servers": ["deepwiki", "not_a_server"]}, registry)
    assert len(errs) == 1 and "not_a_server" in errs[0]
    assert vp.grant_errors("x.yaml", {"mcp_servers": ["deepwiki"]}, registry) == []
    assert vp.grant_errors("x.yaml", {}, registry) == []


def test_shipped_profile_grants_are_all_registered():
    registry, _ = vp.load_registry()
    for f in sorted((AGENTS / "profiles").glob("*.yaml")):
        assert vp.grant_errors(f.name, yaml.safe_load(f.read_text()), registry) == []


def test_validator_cli_fails_on_unregistered_grant(tmp_path):
    # Copy the agents dir, add a bad grant, run the CLI, expect exit 1 naming it.
    import shutil
    dst = tmp_path / "agents"
    shutil.copytree(AGENTS, dst, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    p = dst / "profiles" / "coder.yaml"
    data = yaml.safe_load(p.read_text())
    data["mcp_servers"] = ["deepwiki", "ghost"]
    p.write_text(yaml.safe_dump(data))
    r = subprocess.run([sys.executable, str(dst / "validate_profiles.py")], capture_output=True, text=True)
    assert r.returncode == 1
    assert "ghost" in r.stdout
