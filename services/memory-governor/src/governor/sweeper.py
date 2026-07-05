"""Nightly TTL sweeper. Run as a scheduled job:

    python -m governor.sweeper

Runtime-gated by MEMORY_TTL_SWEEPER_ENABLED — the job can be scheduled
unconditionally; with the flag off it exits 0 without touching anything.
Every deletion emits a memory_expire event (audit spine).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from . import db

log = logging.getLogger("governor.sweeper")

SWEEPS: list[tuple[str, str]] = [
    (
        "expired_task_scoped",
        """DELETE FROM documents
           WHERE memory_class = 'task_scoped'
             AND expires_at IS NOT NULL AND expires_at < now()
           RETURNING id""",
    ),
    (
        "fully_decayed",
        """DELETE FROM documents
           WHERE memory_class = 'decaying'
             AND half_life_days IS NOT NULL AND half_life_days > 0
             AND EXP(-EXTRACT(EPOCH FROM now() - created_at)/86400.0/half_life_days) < 0.05
           RETURNING id""",
    ),
    (
        "expired_session_memory",
        "DELETE FROM session_memory WHERE expires_at < now() RETURNING id",
    ),
]

STALE_REVIEW_SQL = """
UPDATE documents
SET verification_state = 'needs_review'
WHERE memory_class IN ('durable_fact', 'user_preference')
  AND verification_state IN ('unverified', 'inferred', 'confirmed')
  AND last_confirmed_at IS NOT NULL
  AND last_confirmed_at < now() - interval '180 days'
RETURNING id
"""


async def run() -> int:
    if not await db.flag_enabled("MEMORY_TTL_SWEEPER_ENABLED"):
        log.info("MEMORY_TTL_SWEEPER_ENABLED is off — no-op")
        return 0

    p = await db.pool()
    total = 0
    for name, sql in SWEEPS:
        rows = await p.fetch(sql)
        total += len(rows)
        log.info("sweep %s: %d rows", name, len(rows))
        if rows:
            await db.emit_event(
                "memory_expire",
                "sweeper",
                {"sweep": name, "count": len(rows), "ids": [str(r["id"]) for r in rows[:50]]},
            )

    rows = await p.fetch(STALE_REVIEW_SQL)
    log.info("stale -> needs_review: %d rows", len(rows))
    if rows:
        await db.emit_event(
            "memory_needs_review",
            "sweeper",
            {"reason": "stale_180d", "count": len(rows), "ids": [str(r["id"]) for r in rows[:50]]},
        )
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    async def _go() -> None:
        try:
            n = await run()
            log.info("sweep complete: %d rows removed", n)
        finally:
            await db.close()

    asyncio.run(_go())
    sys.exit(0)


if __name__ == "__main__":
    main()
