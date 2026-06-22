# Reference design — NOT deployed. Part of the multi-tenant roadmap
# (see experimental/multi-tenant/README.md). Not wired into the runnable stack;
# provided to illustrate the intended design.

from typing import Awaitable, Callable

from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import get_settings

settings = get_settings()


async def _configure_connection(connection) -> None:
    register_vector(connection)


pool = AsyncConnectionPool(
    conninfo=settings.database_url,
    min_size=settings.min_pool_size,
    max_size=settings.max_pool_size,
    kwargs={"connect_timeout": settings.request_timeout_seconds},
    configure=_configure_connection,
)


async def async_execute(query: str, params: dict | None = None):
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            if cur.description:
                return await cur.fetchall()
            return None


async def async_execute_one(query: str, params: dict | None = None):
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            return await cur.fetchone()
