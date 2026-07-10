# Reference design — NOT deployed. Part of the multi-tenant roadmap
# (see experimental/multi-tenant/README.md). Not wired into the runnable stack;
# provided to illustrate the intended design.

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException

from . import db, schemas
from .auth import require_tenant
from .service import delete_memory, search_memory, upsert_memory


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # db.pool is constructed with open=False (see db.py) because opening it
    # implicitly at module-import time — before uvicorn's event loop runs —
    # silently never connects. Open it now that the loop is live.
    await db.pool.open(wait=True)
    try:
        yield
    finally:
        await db.pool.close()


app = FastAPI(title="Memory Store", version="0.1.0", lifespan=lifespan)


@app.get("/healthz", response_model=schemas.HealthResponse)
async def healthcheck():
    return schemas.HealthResponse(status="ok")


@app.post("/memory", response_model=schemas.MemoryRecordResponse)
async def upsert_memory_route(
    payload: schemas.MemoryRecordRequest, tenant_id: str = Depends(require_tenant)
):
    # aaf-0002: tenant scope comes from the verified token, never the body.
    # A body tenant_id that disagrees with the token is a cross-tenant write attempt.
    if payload.tenant_id and payload.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="tenant_id does not match caller identity")
    payload.tenant_id = tenant_id
    record = await upsert_memory(payload)
    return record


@app.delete("/memory/{record_id}", response_model=schemas.DeleteResponse)
async def delete_memory_route(record_id: str, tenant_id: str = Depends(require_tenant)):
    # aaf-0002: tenant_id derived from the token, NOT the URL path — the old
    # /memory/{tenant_id}/{record_id} let any caller delete any tenant's record.
    deleted = await delete_memory(tenant_id, record_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Record not found")
    return schemas.DeleteResponse(deleted=True)


@app.post("/memory/search", response_model=list[schemas.SearchResult])
async def search_memory_route(
    payload: schemas.SearchRequest, tenant_id: str = Depends(require_tenant)
):
    # aaf-0002: force the search tenant to the verified caller — an omitted
    # payload.tenant_id previously degraded to an all-tenant (1=1) search.
    payload.tenant_id = tenant_id
    results = await search_memory(payload)
    return results
