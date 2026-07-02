"""Pydantic request/response schemas for the public API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    """Body for a natural-language orchestration request."""

    query: str = Field(min_length=1)
    conversation_id: str | None = None


class QueryResponse(BaseModel):
    """Full result of handling a query through the pipeline."""

    answer: str
    conversation_id: str
    intent: dict[str, Any]
    plan: list[dict[str, Any]]
    results: dict[str, Any]
    needs_clarification: bool
    pending_action: dict[str, Any] | None


class SyncTriggerResponse(BaseModel):
    """Result of triggering a sync (inline or via Celery)."""

    mode: str
    task_ids: dict[str, str | None]


class SyncServiceStatus(BaseModel):
    """Per-service sync bookkeeping snapshot."""

    service: str
    status: str
    last_synced_at: str | None
    items_synced: int
    error: str | None


class SyncStatusResponse(BaseModel):
    """Sync status across all workspace services."""

    statuses: list[SyncServiceStatus]
