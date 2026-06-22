# Reference design — NOT deployed. Part of the multi-tenant roadmap
# (see experimental/multi-tenant/README.md). Not wired into the runnable stack;
# provided to illustrate the intended design.

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from . import schemas
from .service import delete_memory, search_memory, upsert_memory

app = FastAPI(title="Memory Store", version="0.1.0")


@app.get("/healthz", response_model=schemas.HealthResponse)
async def healthcheck():
    return schemas.HealthResponse(status="ok")


@app.post("/memory", response_model=schemas.MemoryRecordResponse)
async def upsert_memory_route(payload: schemas.MemoryRecordRequest):
    record = await upsert_memory(payload)
    return record


@app.delete("/memory/{tenant_id}/{record_id}", response_model=schemas.DeleteResponse)
async def delete_memory_route(tenant_id: str, record_id: str):
    deleted = await delete_memory(tenant_id, record_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Record not found")
    return schemas.DeleteResponse(deleted=True)


@app.post("/memory/search", response_model=list[schemas.SearchResult])
async def search_memory_route(payload: schemas.SearchRequest):
    results = await search_memory(payload)
    return results
