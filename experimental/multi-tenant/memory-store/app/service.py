# Reference design — NOT deployed. Part of the multi-tenant roadmap
# (see experimental/multi-tenant/README.md). Not wired into the runnable stack;
# provided to illustrate the intended design.

from __future__ import annotations

from typing import Any

from . import schemas
from .db import async_execute, async_execute_one


async def upsert_memory(record: schemas.MemoryRecordRequest) -> schemas.MemoryRecordResponse:
    query = """
    INSERT INTO memory_records (
        tenant_id,
        record_id,
        record_type,
        content,
        content_vector,
        tags,
        status,
        metadata
    ) VALUES (
        %(tenant_id)s,
        %(record_id)s,
        %(record_type)s,
        %(content)s,
        %(content_vector)s,
        %(tags)s,
        %(status)s,
        %(metadata)s
    )
    ON CONFLICT (tenant_id, record_id)
    DO UPDATE SET
        record_type = EXCLUDED.record_type,
        content = EXCLUDED.content,
        content_vector = EXCLUDED.content_vector,
        tags = EXCLUDED.tags,
        status = EXCLUDED.status,
        metadata = EXCLUDED.metadata,
        updated = now()
    RETURNING *;
    """

    row = await async_execute_one(
        query,
        {
            "tenant_id": record.tenant_id,
            "record_id": record.record_id,
            "record_type": record.record_type,
            "content": record.content,
            "content_vector": record.content_vector,
            "tags": record.tags,
            "status": record.status,
            "metadata": record.metadata,
        },
    )
    return schemas.MemoryRecordResponse(**dict(row))


async def delete_memory(tenant_id: str, record_id: str) -> bool:
    query = """
    DELETE FROM memory_records
    WHERE tenant_id = %(tenant_id)s AND record_id = %(record_id)s
    RETURNING id;
    """
    row = await async_execute_one(query, {"tenant_id": tenant_id, "record_id": record_id})
    return row is not None


async def search_memory(payload: schemas.SearchRequest) -> list[schemas.SearchResult]:
    filters: list[str] = ["1=1"]
    params: dict[str, Any] = {
        "query_vector": payload.query_vector,
        "limit": payload.limit,
    }

    if payload.tenant_id:
        filters.append("tenant_id = %(tenant_id)s")
        params["tenant_id"] = payload.tenant_id

    if payload.record_types:
        filters.append("record_type = ANY(%(record_types)s)")
        params["record_types"] = payload.record_types

    if payload.tags:
        filters.append("tags && %(tags)s")
        params["tags"] = payload.tags

    if payload.status:
        filters.append("status = ANY(%(status)s)")
        params["status"] = payload.status

    score_clause = "1 - (content_vector <=> %(query_vector)s)"
    filters_clause = " AND ".join(filters)

    query = f"""
    SELECT
        id,
        tenant_id,
        record_id,
        record_type,
        content,
        tags,
        status,
        created,
        updated,
        metadata,
        {score_clause} AS score
    FROM memory_records
    WHERE {filters_clause}
    ORDER BY content_vector <=> %(query_vector)s
    LIMIT %(limit)s;
    """

    rows = await async_execute(query, params)
    results: list[schemas.SearchResult] = []
    for row in rows or []:
        score = float(row[-1])
        if payload.min_score is not None and score < payload.min_score:
            continue
        record_dict = dict(row)
        record_dict.pop("score", None)
        results.append(schemas.SearchResult(**record_dict, score=score))
    return results
