# Reference design — NOT deployed. Part of the multi-tenant roadmap
# (see experimental/multi-tenant/README.md). Not wired into the runnable stack;
# provided to illustrate the intended design.

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class MemoryRecordBase(BaseModel):
    tenant_id: str = Field(..., min_length=1)
    record_id: str = Field(..., min_length=1)
    record_type: str = Field(..., min_length=1)
    content: str
    content_vector: list[float] = Field(..., min_items=1)
    tags: list[str] | None = None
    status: Literal["draft", "ready", "archived"] | None = None
    metadata: dict[str, Any] | None = None


class MemoryRecordRequest(MemoryRecordBase):
    pass


class MemoryRecordResponse(MemoryRecordBase):
    id: UUID
    created: datetime
    updated: datetime


class DeleteResponse(BaseModel):
    deleted: bool


class SearchRequest(BaseModel):
    query_vector: list[float] = Field(..., min_items=1)
    tenant_id: str | None = None
    record_types: list[str] | None = None
    tags: list[str] | None = None
    status: list[str] | None = None
    limit: int = Field(10, ge=1, le=100)
    min_score: float | None = Field(
        default=0.0,
        description="Optional cosine similarity threshold (0-1)",
    )


class SearchResult(MemoryRecordResponse):
    score: float


class HealthResponse(BaseModel):
    status: str
