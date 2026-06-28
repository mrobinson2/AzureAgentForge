"""Idempotent SQL migration runner for the governed-memory overlay.

The governor shares Honcho's Postgres and adds an overlay (governed-memory
columns on `documents` + its own tables). This applies the ordered *.sql files in
../../migrations once each, tracked in `governor_schema_migrations`. Every
migration is written IF-NOT-EXISTS so re-applying is a no-op.

Run standalone (`python -m governor.migrate`) or via apply() on startup. Safe to
call on every boot; already-applied files are skipped.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from . import db

log = logging.getLogger("governor.migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_TRACK_DDL = """
CREATE TABLE IF NOT EXISTS governor_schema_migrations (
    filename   text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


async def apply() -> list[str]:
    """Apply every not-yet-applied migration in order. Returns applied filenames."""
    pool = await db.pool()
    applied: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute(_TRACK_DDL)
        done = {r["filename"] for r in await conn.fetch(
            "SELECT filename FROM governor_schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            log.info("applying migration %s", path.name)
            # Each file is idempotent; run it + record atomically.
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute(
                    "INSERT INTO governor_schema_migrations (filename) VALUES ($1) "
                    "ON CONFLICT (filename) DO NOTHING", path.name)
            applied.append(path.name)
    if applied:
        log.info("applied %d migration(s): %s", len(applied), ", ".join(applied))
    else:
        log.info("schema up to date (%d known)", len(list(MIGRATIONS_DIR.glob("*.sql"))))
    return applied


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    applied = asyncio.run(apply())
    print(f"applied {len(applied)} migration(s): {applied or 'none'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
