"""Agent identity in the memory helper — observer/created_by_peer, and the
fallback when the slug does not resolve.

Two things are pinned here:

1. `pc-memory record` attributes the write to PAPERCLIP_AGENT_SLUG, so memory is
   per-agent rather than collapsed onto one shared peer.
2. The fallback when no slug is set is an UNPRIVILEGED slug — not "operator".
   governor/profiles.py maps "operator" to the SYSTEM profile (write: every
   class), so the old fallback silently handed any agent whose identity failed to
   resolve full write authority, defeating the per-agent memoryProfile it is
   supposed to be checked against. An unknown slug falls to SPECIALIST, which is
   least-privilege.

Offline: curl is stubbed onto PATH and logs its argv; nothing is contacted.
See docs/design/memory-system.md §18.
"""

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "apps" / "hermes" / "overrides" / "skills" / "playbooks" / "honcho-memory" / "scripts"
PROFILES = REPO / "services" / "memory-governor" / "src" / "governor" / "profiles.py"

CURL_STUB = """#!/bin/sh
{
for a in "$@"; do printf 'ARG:%s\\n' "$a"; done
printf 'CALL-END\\n'
} >> "$CURL_LOG"
"""


def _record(tmp_path, extra_env=None, unset=()):
    """Run `pc-memory record` with a stubbed curl; return the JSON body sent."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "curl"
    stub.write_text(CURL_STUB)
    stub.chmod(0o755)
    log = tmp_path / "curl.log"
    log.write_text("")

    env = dict(os.environ)
    for name in ("PAPERCLIP_AGENT_SLUG", "GOVERNOR_AGENT_SLUG", *unset):
        env.pop(name, None)
    env.update(extra_env or {})
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["CURL_LOG"] = str(log)
    env.setdefault("GOVERNOR_API_KEY", "test-key")

    subprocess.run(
        ["sh", str(SCRIPTS / "pc-memory.sh"), "record",
         "--content", "the user's dog is named Biscuit"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    args = [l[4:] for l in log.read_text().splitlines() if l.startswith("ARG:")]
    for i, a in enumerate(args):
        if a == "-d" and i + 1 < len(args):
            return json.loads(args[i + 1])
    raise AssertionError(f"no JSON body in curl args: {args}")


def test_record_attributes_the_write_to_the_agent_slug(tmp_path):
    body = _record(tmp_path, {"PAPERCLIP_AGENT_SLUG": "researcher"})
    assert body["observer"] == "researcher"
    assert body["created_by_peer"] == "researcher"


def test_governor_slug_is_the_second_source(tmp_path):
    body = _record(tmp_path, {"GOVERNOR_AGENT_SLUG": "watchdog"})
    assert body["created_by_peer"] == "watchdog"


def test_paperclip_slug_wins_over_governor_slug(tmp_path):
    body = _record(
        tmp_path, {"PAPERCLIP_AGENT_SLUG": "coder", "GOVERNOR_AGENT_SLUG": "watchdog"}
    )
    assert body["created_by_peer"] == "coder"


def test_fallback_is_not_the_privileged_operator_peer(tmp_path):
    # The regression this guards: "operator" carries the SYSTEM profile, so an
    # unresolved identity used to inherit write authority over every memory
    # class. The fallback must be a slug the profile table does NOT special-case.
    body = _record(tmp_path)
    assert body["created_by_peer"] != "operator"
    assert body["observer"] != "operator"
    assert body["created_by_peer"] == "unknown-agent"


def test_fallback_slug_is_absent_from_the_governor_profile_table(tmp_path):
    # Pins the two files together: if someone later adds "unknown-agent" to
    # DEFAULT_PROFILES with elevated write authority, this fails rather than
    # quietly restoring the privilege bug.
    profiles_src = PROFILES.read_text()
    assert '"unknown-agent"' not in profiles_src, (
        "the helper's fallback slug must stay out of DEFAULT_PROFILES so it "
        "resolves to the least-privilege SPECIALIST default"
    )
