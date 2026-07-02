"""Sync endpoints: trigger workspace syncs and inspect their status."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.schemas import (
    SyncServiceStatus,
    SyncStatusResponse,
    SyncTriggerResponse,
)
from app.db.models import SyncState, User
from app.db.session import get_db
from app.sync.tasks import sync_all, sync_calendar, sync_drive, sync_gmail

router = APIRouter(tags=["sync"])

#: Canonical service order for status/trigger responses.
_SERVICES: tuple[str, ...] = ("gmail", "calendar", "drive")


@router.post("/sync/trigger", response_model=SyncTriggerResponse)
async def trigger_sync(
    inline: bool = False,
    user: User = Depends(get_current_user),
) -> SyncTriggerResponse:
    """Kick off syncs for the current user.

    ``inline=true`` runs all three service syncs synchronously in worker
    threads (each task internally drives its own event loop) and returns
    ``mode="inline"``. Otherwise the syncs are dispatched as a Celery group and
    the group/task ids are returned as strings.
    """
    user_id = str(user.id)
    if inline:
        await asyncio.to_thread(sync_gmail, user_id, full=True)
        await asyncio.to_thread(sync_calendar, user_id, full=True)
        await asyncio.to_thread(sync_drive, user_id, full=True)
        return SyncTriggerResponse(
            mode="inline",
            task_ids={service: None for service in _SERVICES},
        )

    group_result = await asyncio.to_thread(sync_all, user_id)
    children = list(getattr(group_result, "results", []) or [])
    task_ids: dict[str, str | None] = {
        service: (str(children[index].id) if index < len(children) else None)
        for index, service in enumerate(_SERVICES)
    }
    group_id = getattr(group_result, "id", None)
    task_ids["group"] = str(group_id) if group_id is not None else None
    return SyncTriggerResponse(mode="celery", task_ids=task_ids)


@router.get("/sync/status", response_model=SyncStatusResponse)
async def sync_status(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> SyncStatusResponse:
    """Return the per-service sync state for the current user."""
    result = await session.execute(
        select(SyncState).where(SyncState.user_id == user.id)
    )
    rows = {row.service: row for row in result.scalars().all()}
    statuses: list[SyncServiceStatus] = []
    for service in _SERVICES:
        row = rows.get(service)
        if row is None:
            statuses.append(
                SyncServiceStatus(
                    service=service,
                    status="idle",
                    last_synced_at=None,
                    items_synced=0,
                    error=None,
                )
            )
        else:
            statuses.append(
                SyncServiceStatus(
                    service=service,
                    status=row.status,
                    last_synced_at=(
                        row.last_synced_at.isoformat()
                        if row.last_synced_at is not None
                        else None
                    ),
                    items_synced=row.items_synced,
                    error=row.error,
                )
            )
    return SyncStatusResponse(statuses=statuses)
