import json, pathlib, subprocess, sys
import yaml
from jsonschema import Draft202012Validator

AGENTS = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = AGENTS / "profile.schema.json"
PROFILES = AGENTS / "profiles"

def _schema():
    return json.loads(SCHEMA.read_text())

def test_schema_is_valid_jsonschema():
    Draft202012Validator.check_schema(_schema())

def test_good_profile_passes():
    v = Draft202012Validator(_schema())
    good = {
        "name": "Orchestrator", "role": "orchestrator",
        "description": "Coordinates the team and routes work.",
        "model_tier": "frontier", "toolsets": ["terminal", "file"],
        "reports_to": None,
    }
    assert list(v.iter_errors(good)) == []

def test_bad_profile_fails():
    v = Draft202012Validator(_schema())
    bad = {"name": "X", "model_tier": "supercomputer"}
    assert list(v.iter_errors(bad))

def test_all_shipped_profiles_valid():
    v = Draft202012Validator(_schema())
    files = sorted(PROFILES.glob("*.yaml"))
    assert len(files) == 14, f"expected 14 profiles, found {len(files)}"
    for f in files:
        data = yaml.safe_load(f.read_text())
        errs = list(v.iter_errors(data))
        assert errs == [], f"{f.name}: {[e.message for e in errs]}"

def test_validator_cli_passes_on_repo(tmp_path):
    r = subprocess.run([sys.executable, str(AGENTS / "validate_profiles.py")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
