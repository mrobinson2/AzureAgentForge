#!/usr/bin/env python3
"""
Patch hermes source before pip install to support HERMES_STATE_DB_PATH env var.

Redirects the SessionDB away from the HERMES_HOME directory (Azure File Share /
SMB mount) to an env-var-controlled path. Without this patch, every Hermes
session fails with "database is locked" because Azure Files SMB does not support
the POSIX byte-range locks that SQLite WAL mode requires.

Run this AFTER copying apps/hermes/src/ but BEFORE pip install.
"""

import sys
from pathlib import Path

src_root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/hermes-src")

# ── Patch 1: hermes_state.py DEFAULT_DB_PATH ──────────────────────────────────
state_py = src_root / "hermes_state.py"
content = state_py.read_text()
OLD1 = 'DEFAULT_DB_PATH = get_hermes_home() / "state.db"'
NEW1 = 'DEFAULT_DB_PATH = Path(os.environ.get("HERMES_STATE_DB_PATH") or str(get_hermes_home() / "state.db"))'
if OLD1 in content:
    state_py.write_text(content.replace(OLD1, NEW1))
    print("[patch-hermes-src] Patched DEFAULT_DB_PATH in hermes_state.py")
else:
    print("[patch-hermes-src] WARN: anchor not found in hermes_state.py — skipping (already patched or upstream changed)")

# ── Patch 2: acp_adapter/session.py hardcoded db_path ────────────────────────
session_py = src_root / "acp_adapter" / "session.py"
content = session_py.read_text()

if "from pathlib import Path" not in content:
    content = content.replace(
        "from hermes_constants import get_hermes_home",
        "from hermes_constants import get_hermes_home\nfrom pathlib import Path",
    )
    print("[patch-hermes-src] Added Path import to acp_adapter/session.py")

OLD2 = '            self._db_instance = SessionDB(db_path=hermes_home / "state.db")'
NEW2 = (
    "            import os\n"
    '            _state_db_path = Path(os.environ.get("HERMES_STATE_DB_PATH") or str(hermes_home / "state.db"))\n'
    "            self._db_instance = SessionDB(db_path=_state_db_path)"
)
if OLD2 in content:
    session_py.write_text(content.replace(OLD2, NEW2))
    print("[patch-hermes-src] Patched db_path in acp_adapter/session.py")
else:
    print("[patch-hermes-src] WARN: anchor not found in acp_adapter/session.py — skipping (already patched or upstream changed)")

print("[patch-hermes-src] Done")
