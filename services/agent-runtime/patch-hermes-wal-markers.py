#!/usr/bin/env python3
"""AAF build-time patch — teach Hermes' WAL fallback about Azure Files SMB.

apps/hermes/src is an upstream submodule (NousResearch/hermes-agent) we can't
edit, so we patch the copied source at image-build time.

Hermes' apply_wal_with_fallback() is designed to fall back to DELETE journal
mode on WAL-hostile filesystems, but _WAL_INCOMPAT_MARKERS only matches
"locking protocol" / "not authorized" / "disk i/o error". Azure Files (SMB/CIFS)
surfaces the `PRAGMA journal_mode=WAL` failure as "database is locked"
(SQLITE_BUSY), which fell through the marker check and was re-raised — crashing
the kanban dispatcher every tick. This adds that marker so any SMB-resident DB
degrades to DELETE mode instead of crashing.

Idempotent. Run from the Hermes source root (where hermes_state.py lives).
Upstream fix: add the marker to _WAL_INCOMPAT_MARKERS in NousResearch/hermes-agent.
"""
import sys
import pathlib

MARKER = '"database is locked"'
NEW_LINE = '    "database is locked",     # Azure Files / SMB surfaces WAL-incompat as SQLITE_BUSY'


def patch(src: str) -> str:
    """Insert the new marker line after the 'disk i/o error' line. Idempotent."""
    if MARKER in src:
        return src
    out = []
    inserted = False
    for line in src.splitlines(keepends=True):
        out.append(line)
        if not inserted and '"disk i/o error"' in line:
            eol = "\n" if line.endswith("\n") else ""
            out.append(NEW_LINE + eol)
            inserted = True
    if not inserted:
        raise SystemExit("[patch-wal-markers] FATAL: anchor '\"disk i/o error\"' not found")
    return "".join(out)


def main() -> int:
    target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "hermes_state.py")
    src = target.read_text()
    if MARKER in src:
        print("[patch-wal-markers] already applied")
        return 0
    patched = patch(src)
    target.write_text(patched)
    assert MARKER in target.read_text(), "patch did not take"
    print("[patch-wal-markers] added 'database is locked' to _WAL_INCOMPAT_MARKERS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
