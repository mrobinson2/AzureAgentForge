# Reference design — NOT deployed. Part of the multi-tenant roadmap
# (see experimental/multi-tenant/README.md). Not wired into the runnable stack;
# provided to illustrate the intended design.

from typing import Awaitable, Callable

from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import get_settings

settings = get_settings()


async def _configure_connection(connection) -> None:
    # register_vector (sync) expects a sync psycopg.Connection; called on the
    # AsyncConnection this pool hands out, it never registers the vector type
    # adapter and the connection setup silently fails. register_vector_async
    # is pgvector's counterpart for AsyncConnectionPool/AsyncConnection.
    await register_vector_async(connection)


# open=False: this module is imported synchronously (at process/app-module
# import time, before uvicorn's event loop is running), and psycopg_pool's
# implicit open-in-constructor path silently fails to establish connections
# outside a running loop — the pool looks constructed but every checkout hangs
# until its connect_timeout. main.py's lifespan hook calls `await pool.open()`
# once the event loop is actually running (and `await pool.close()` on
# shutdown), which is the supported way to open an AsyncConnectionPool.
pool = AsyncConnectionPool(
    conninfo=settings.database_url,
    min_size=settings.min_pool_size,
    max_size=settings.max_pool_size,
    kwargs={"connect_timeout": settings.request_timeout_seconds},
    configure=_configure_connection,
    open=False,
)


async def _set_tenant_scope(cur, tenant_id: str | None) -> None:
    """Scope the rest of THIS transaction to tenant_id for the RLS policy on
    memory_records (aaf-0018: ``USING (tenant_id = current_setting('app.tenant_id', true))``).

    Uses ``set_config(..., is_local=true)`` — the parameterized equivalent of
    ``SET LOCAL app.tenant_id = '<value>'`` — so tenant_id (which originates
    from a verified bearer token, never client input, but may still contain
    arbitrary text) is bound as a query parameter rather than interpolated
    into SQL. ``is_local=true`` matches ``SET LOCAL``: scoped to this
    transaction/connection-pool checkout, never leaks to the next pooled use.
    A None tenant_id is a deliberate no-op — the policy's
    ``current_setting(..., true)`` returns NULL when unset, so the connection
    sees zero rows (fail closed), never another tenant's.
    """
    if tenant_id is None:
        return
    await cur.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))


async def async_execute(query: str, params: dict | None = None, *, tenant_id: str | None = None):
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await _set_tenant_scope(cur, tenant_id)
            await cur.execute(query, params)
            if cur.description:
                return await cur.fetchall()
            return None


async def async_execute_one(query: str, params: dict | None = None, *, tenant_id: str | None = None):
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await _set_tenant_scope(cur, tenant_id)
            await cur.execute(query, params)
            return await cur.fetchone()
