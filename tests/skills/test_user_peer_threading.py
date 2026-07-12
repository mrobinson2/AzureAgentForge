"""A5 — canonical user peer threading through the skill helper scripts.

pc-memory.sh must send `observed` EXPLICITLY on record (an omitted field is
one config drift away from a fragmenting write), and pc-honcho.sh must default
--peer to the same input — both resolving HONCHO_USER_PEER_ID with the same
"user" fallback the governor and compose stacks use. Offline: curl is stubbed
onto PATH and logs its argv; nothing is contacted.
See docs/design/memory-system.md §18."""

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "apps" / "hermes" / "overrides" / "skills" / "playbooks" / "honcho-memory" / "scripts"

CURL_STUB = """#!/bin/sh
{
for a in "$@"; do printf 'ARG:%s\\n' "$a"; done
printf 'CALL-END\\n'
} >> "$CURL_LOG"
"""


def _run(tmp_path, script, args, extra_env=None, unset=()):
    """Run a helper script with a stubbed curl; return the list of curl calls,
    each call a list of argv strings."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "curl"
    stub.write_text(CURL_STUB)
    stub.chmod(0o755)
    log = tmp_path / "curl.log"
    log.write_text("")

    env = dict(os.environ)
    for name in ("HONCHO_USER_PEER_ID", *unset):
        env.pop(name, None)
    env.update(extra_env or {})
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["CURL_LOG"] = str(log)

    proc = subprocess.run(
        ["sh", str(SCRIPTS / script), *args],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"{script} failed: {proc.stderr}"

    calls, current = [], []
    for line in log.read_text().splitlines():
        if line == "CALL-END":
            calls.append(current)
            current = []
        elif line.startswith("ARG:"):
            current.append(line[4:])
    return calls


def _payload(call):
    """The JSON body following -d in a stubbed curl argv."""
    return json.loads(call[call.index("-d") + 1])


# ── pc-memory.sh: record sends observed explicitly ───────────────────────────

def test_pc_memory_record_defaults_observed_to_user(tmp_path):
    calls = _run(tmp_path, "pc-memory.sh", ["record", "--content", "fact"])
    body = _payload(calls[0])
    assert body["observed"] == "user"
    # and the field is IN the request — never left to the server-side default
    assert "observed" in body


def test_pc_memory_record_threads_env_peer(tmp_path):
    calls = _run(
        tmp_path, "pc-memory.sh", ["record", "--content", "fact"],
        extra_env={"HONCHO_USER_PEER_ID": "principal-42"},
    )
    body = _payload(calls[0])
    assert body["observed"] == "principal-42"
    # writer identity stays the agent — only the SUBJECT is the canonical peer
    assert body["observer"] == body["created_by_peer"] != "principal-42"


# ── pc-honcho.sh: --peer defaults to the canonical peer ──────────────────────

def test_pc_honcho_ask_defaults_peer_to_user(tmp_path):
    calls = _run(tmp_path, "pc-honcho.sh", ["ask", "--query", "what do you know"])
    url = next(a for a in calls[0] if "/peers/" in a)
    assert "/peers/user/chat" in url


def test_pc_honcho_ask_threads_env_peer(tmp_path):
    calls = _run(
        tmp_path, "pc-honcho.sh", ["ask", "--query", "what do you know"],
        extra_env={"HONCHO_USER_PEER_ID": "principal-42"},
    )
    url = next(a for a in calls[0] if "/peers/" in a)
    assert "/peers/principal-42/chat" in url


def test_pc_honcho_ask_explicit_peer_wins(tmp_path):
    calls = _run(
        tmp_path, "pc-honcho.sh",
        ["ask", "--peer", "researcher", "--query", "self check"],
        extra_env={"HONCHO_USER_PEER_ID": "principal-42"},
    )
    url = next(a for a in calls[0] if "/peers/" in a)
    assert "/peers/researcher/chat" in url


def test_pc_honcho_record_defaults_peer(tmp_path):
    calls = _run(
        tmp_path, "pc-honcho.sh", ["record", "--content", "note"],
        extra_env={"HONCHO_USER_PEER_ID": "principal-42"},
    )
    # two calls: idempotent session ensure, then the message post
    msg_body = _payload(calls[-1])
    assert msg_body["messages"][0]["peer_id"] == "principal-42"
