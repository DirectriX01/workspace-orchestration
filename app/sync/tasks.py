"""Celery sync tasks: pull normalized records, embed them, upsert the cache.

Each task (``sync_gmail`` / ``sync_calendar`` / ``sync_drive``) is a plain
function wrapped by ``@celery_app.task`` (``bind=False``), so it can be invoked
directly as ``sync_gmail(user_id, full=True)`` from scripts as well as
dispatched through Celery. All three share :func:`_run_sync`, which:

* loads the :class:`User` and its per-service :class:`SyncState` row,
* marks the state ``running``,
* fetches items newer than the stored watermark (or everything when ``full``),
* builds embedding text + vectors and upserts them into the service cache
  table via ``INSERT ... ON CONFLICT DO UPDATE``,
* records ``items_synced`` and a tz-aware ``last_synced_at`` (the sync start
  time), flipping the state back to ``idle`` (or ``error`` on failure).

Datetimes are handled tz-aware end to end: the watermark stored in
``last_synced_at`` is UTC-aware and the mock/real clients emit tz-aware
timestamps, so the ``>=`` comparison never mixes naive and aware values.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import redis
from celery import group
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    GcalCache,
    GdriveCache,
    GmailCache,
    SyncState,
    User,
)
from app.db.session import get_sync_session_factory
from app.search.embed_texts import (
    clean_email_body,
    email_embed_text,
    event_embed_text,
    file_embed_text,
)
from app.search.embeddings import EmbeddingService
from app.services.factory import (
    get_calendar_client,
    get_drive_client,
    get_gmail_client,
)
from app.sync.celery_app import celery_app

#: Conflict target (unique key) columns per service.
_INDEX_ELEMENTS: dict[str, list[str]] = {
    "gmail": ["user_id", "email_id"],
    "calendar": ["user_id", "event_id"],
    "drive": ["user_id", "file_id"],
}

#: Cache ORM model per service.
_MODELS: dict[str, type] = {
    "gmail": GmailCache,
    "calendar": GcalCache,
    "drive": GdriveCache,
}


def _get_client(service: str, user: User) -> Any:
    """Return the factory-built client for ``service``."""
    if service == "gmail":
        return get_gmail_client(user)
    if service == "calendar":
        return get_calendar_client(user)
    if service == "drive":
        return get_drive_client(user)
    raise ValueError(f"unknown service: {service}")


async def _list_items(service: str, client: Any, watermark: datetime | None) -> list[dict]:
    """Fetch normalized records newer than ``watermark`` for ``service``."""
    if service == "gmail":
        return await client.list_messages(updated_after=watermark)
    if service == "calendar":
        return await client.list_events(updated_after=watermark)
    if service == "drive":
        return await client.list_files(updated_after=watermark)
    raise ValueError(f"unknown service: {service}")


def _embed_text(service: str, item: dict) -> str:
    """Build the embedding source text for one record."""
    if service == "gmail":
        return email_embed_text(item)
    if service == "calendar":
        return event_embed_text(item)
    if service == "drive":
        return file_embed_text(item)
    raise ValueError(f"unknown service: {service}")


def _build_row(
    service: str,
    user_id: uuid.UUID,
    item: dict,
    embed_text: str,
    embedding: list[float],
    synced_at: datetime,
) -> dict[str, Any]:
    """Map a normalized record onto a cache-table row dict for upsert."""
    if service == "gmail":
        return {
            "user_id": user_id,
            "email_id": item["id"],
            "thread_id": item.get("thread_id"),
            "subject": item.get("subject"),
            "from_email": item.get("from_email"),
            "from_name": item.get("from_name"),
            "to_emails": list(item.get("to") or []),
            "labels": list(item.get("labels") or []),
            "body_preview": clean_email_body(item.get("body", ""))[:500],
            "embed_text": embed_text,
            "embedding": embedding,
            "received_at": item.get("received_at"),
        }
    if service == "calendar":
        return {
            "user_id": user_id,
            "event_id": item["id"],
            "title": item.get("title"),
            "description": item.get("description"),
            "location": item.get("location"),
            "organizer_email": item.get("organizer_email"),
            "attendee_emails": list(item.get("attendees") or []),
            "start_time": item.get("start"),
            "end_time": item.get("end"),
            "status": item.get("status") or "confirmed",
            "embed_text": embed_text,
            "embedding": embedding,
            "updated_at": synced_at,
        }
    if service == "drive":
        return {
            "user_id": user_id,
            "file_id": item["id"],
            "name": item.get("name"),
            "mime_type": item.get("mime_type"),
            "owner_email": item.get("owner_email"),
            "content_preview": (item.get("content") or "").strip()[:500],
            "web_link": item.get("web_link"),
            "embed_text": embed_text,
            "embedding": embedding,
            "modified_at": item.get("modified_at"),
        }
    raise ValueError(f"unknown service: {service}")


def _get_or_create_state(session: Session, user_id: uuid.UUID, service: str) -> SyncState:
    """Return the (user, service) SyncState row, creating it if absent."""
    state = session.execute(
        select(SyncState).where(
            SyncState.user_id == user_id, SyncState.service == service
        )
    ).scalar_one_or_none()
    if state is None:
        state = SyncState(user_id=user_id, service=service)
        session.add(state)
    return state


def _run_sync(service: str, user_id: str, full: bool) -> int:
    """Core sync routine shared by all three service tasks; returns item count."""
    settings = get_settings()
    started_at = datetime.now(timezone.utc)
    model = _MODELS[service]
    index_elements = _INDEX_ELEMENTS[service]
    user_uuid = uuid.UUID(user_id)

    session_factory = get_sync_session_factory()
    session: Session = session_factory()
    try:
        user = session.get(User, user_uuid)
        if user is None:
            raise ValueError(f"user {user_id} not found")

        state = _get_or_create_state(session, user_uuid, service)
        state.status = "running"
        session.commit()

        watermark = None if full else state.last_synced_at
        client = _get_client(service, user)
        items = asyncio.run(_list_items(service, client, watermark))

        if items:
            texts = [_embed_text(service, item) for item in items]
            embedder = EmbeddingService(
                redis_sync=redis.Redis.from_url(settings.redis_url)
            )
            vectors = embedder.embed_batch_sync(texts)
            rows = [
                _build_row(service, user_uuid, item, text, vector, started_at)
                for item, text, vector in zip(items, texts, vectors)
            ]
            stmt = pg_insert(model).values(rows)
            update_cols = {
                column: getattr(stmt.excluded, column)
                for column in rows[0]
                if column not in index_elements
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=index_elements, set_=update_cols
            )
            session.execute(stmt)

        state.status = "idle"
        state.items_synced = len(items)
        state.last_synced_at = started_at
        state.error = None
        session.commit()
        return len(items)
    except Exception as exc:  # noqa: BLE001 - persist the error, then re-raise
        session.rollback()
        try:
            state = _get_or_create_state(session, user_uuid, service)
            state.status = "error"
            state.error = str(exc)
            session.commit()
        except Exception:
            session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(bind=False)
def sync_gmail(user_id: str, full: bool = False) -> int:
    """Sync (embed + cache) the user's Gmail messages."""
    return _run_sync("gmail", user_id, full)


@celery_app.task(bind=False)
def sync_calendar(user_id: str, full: bool = False) -> int:
    """Sync (embed + cache) the user's Calendar events."""
    return _run_sync("calendar", user_id, full)


@celery_app.task(bind=False)
def sync_drive(user_id: str, full: bool = False) -> int:
    """Sync (embed + cache) the user's Drive files."""
    return _run_sync("drive", user_id, full)


@celery_app.task(bind=False)
def sync_all(user_id: str) -> Any:
    """Dispatch all three service syncs for ``user_id`` as a Celery group."""
    return group(
        sync_gmail.s(user_id),
        sync_calendar.s(user_id),
        sync_drive.s(user_id),
    ).apply_async()
