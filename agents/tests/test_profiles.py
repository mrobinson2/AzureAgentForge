import json, pathlib, subprocess, sys
import yaml
from jsonschema import Draft202012Validator

AGENTS = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = AGENTS / "profile.schema.json"
PROFILES = AGENTS / "profiles"
TEMPLATES = AGENTS / "templates"

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

# ── Disposition protocol lint ────────────────────────────────────────────────
# Ported from upstream private deployment incident learnings: agents that end
# a run without recording a platform-recognized disposition (done/cancelled
# with a comment, in_review with a reviewer path, blocked with blockers)
# get flagged missing_disposition/plan_only and their issues end blocked.
# Every ISSUE-SCOPED prompt must carry the protocol section.

DISPOSITION_HEADING = "# Completing an issue (disposition protocol)"
# intake is conversation-scoped (no issue-status workflow), so it is exempt.
DISPOSITION_EXEMPT = {"intake.AGENTS.template.md"}

def _issue_scoped_prompt_files():
    # "*AGENTS.template.md" (no leading dot) also matches the base
    # AGENTS.template.md, not just the role-prefixed variants.
    return sorted(PROFILES.glob("*.AGENTS.md")) + sorted(TEMPLATES.glob("*AGENTS.template.md"))

def test_prompt_files_discovered():
    files = _issue_scoped_prompt_files()
    # 14 shipped profile prompts + 3 templates (generic, coordinator, intake).
    # Was 13 profile prompts (16 total) until generalist.AGENTS.md was added:
    # generalist.yaml had no matching system prompt — a role with a
    # machine-readable contract and zero governance text. Found and fixed by
    # scripts/replay-gate/compose_prompt.py, which now fails loudly on this
    # exact gap; see docs/design/prompt-replay-gate.md.
    assert len(files) == 17, f"expected 17 prompt files, found {len(files)}"

def test_disposition_protocol_present_in_issue_scoped_prompts():
    missing = [
        f.name
        for f in _issue_scoped_prompt_files()
        if f.name not in DISPOSITION_EXEMPT
        and DISPOSITION_HEADING not in f.read_text()
    ]
    assert missing == [], f"missing disposition-protocol section: {missing}"

def test_disposition_protocol_forbids_silent_terminal_states():
    for f in _issue_scoped_prompt_files():
        if f.name in DISPOSITION_EXEMPT:
            continue
        text = f.read_text()
        section = text.split(DISPOSITION_HEADING, 1)[1]
        assert "No silent terminal states" in section, f.name
        assert "plan_only" in section, f.name
        assert "missing_disposition" in section, f.name
