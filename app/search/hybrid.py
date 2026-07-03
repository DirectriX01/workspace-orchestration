"""Hybrid (vector + recency) search over the per-user cache tables.

:class:`HybridSearcher` runs pgvector cosine-similarity queries blended with a
recency decay against the ``gmail_cache`` / ``gcal_cache`` / ``gdrive_cache``
tables. Scoring::

    score = 0.8 * (1 - cosine_distance(embedding, qvec))
          + 0.2 * exp(-age_seconds / (86400 * 90))

where ``age_seconds`` is derived from each table's recency column
(``received_at`` / ``start_time`` / ``modified_at``). Calendar events use the
*absolute* age so upcoming events are not penalised for being in the future.

When a query has no free-text component the embedding step is skipped entirely
and rows are ordered purely by recency (calendar ascending by start time,
gmail/drive descending by recency).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from dateutil import parser as dateutil_parser
from sqlalchemy import func, nulls_last, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GcalCache, GdriveCache, GmailCache
from app.search.embeddings import EmbeddingService

#: Weight applied to the semantic (cosine) similarity component.
_SEMANTIC_WEIGHT = 0.8
#: Weight applied to the recency-decay component.
_RECENCY_WEIGHT = 0.2
#: Recency decay half-life scale in seconds (~90 days).
_RECENCY_SCALE = 86400.0 * 90.0


def _parse_iso(value: Any) -> datetime | None:
    """Coerce an ISO string (or passthrough datetime) to a datetime, defensively."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return dateutil_parser.isoparse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None


def _query_text(value: Any) -> str:
    """Coerce a ``query`` param to a clean free-text search string.

    A whole-string template (e.g. ``{{find_meeting.top.attendees}}``) resolves to
    the referenced raw object, which for the attendee list is a ``list[str]``.
    Join list/tuple values into a single space-separated query so the downstream
    embedding step and ``.strip()`` never receive a non-string.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value if item).strip()
    return str(value).strip()


class HybridSearcher:
    """Vector + recency search and cache lookups over the workspace cache tables."""

    def __init__(self, session: AsyncSession, embedder: EmbeddingService) -> None:
        self.session = session
        self.embedder = embedder

    # ------------------------------------------------------------------ #
    # Scoring helpers                                                    #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _recency_expr(column: Any, *, use_abs: bool = False) -> Any:
        """SQL expression for the recency-decay component in ``[0, 1]``."""
        diff = func.extract("epoch", func.now() - column)
        if use_abs:
            diff = func.abs(diff)
        return func.coalesce(func.exp(-diff / _RECENCY_SCALE), 0.0)

    @classmethod
    def _score_expr(cls, embedding_col: Any, qvec: list[float], recency: Any) -> Any:
        """Blended semantic + recency score expression."""
        distance = embedding_col.cosine_distance(qvec)
        return _SEMANTIC_WEIGHT * (1 - distance) + _RECENCY_WEIGHT * recency

    @staticmethod
    def _score_value(score: Any) -> float | None:
        if score is None:
            return None
        return round(float(score), 4)

    # ------------------------------------------------------------------ #
    # Gmail                                                              #
    # ------------------------------------------------------------------ #
    async def search_gmail(self, user_id: uuid.UUID, params: dict) -> list[dict]:
        query = _query_text(params.get("query"))
        k = int(params.get("k") or 5)
        conditions = [GmailCache.user_id == user_id]
        if params.get("from_email"):
            conditions.append(GmailCache.from_email.ilike(f"%{params['from_email']}%"))
        if params.get("label"):
            conditions.append(GmailCache.labels.any(params["label"]))
        after = _parse_iso(params.get("after"))
        if after is not None:
            conditions.append(GmailCache.received_at >= after)
        before = _parse_iso(params.get("before"))
        if before is not None:
            conditions.append(GmailCache.received_at < before)

        recency = self._recency_expr(GmailCache.received_at)
        if query:
            qvec = await self.embedder.embed(query)
            score = self._score_expr(GmailCache.embedding, qvec, recency)
            stmt = (
                select(GmailCache, score.label("score"))
                .where(*conditions)
                .order_by(score.desc())
                .limit(k)
            )
        else:
            stmt = (
                select(GmailCache, recency.label("score"))
                .where(*conditions)
                .order_by(nulls_last(GmailCache.received_at.desc()))
                .limit(k)
            )
        result = await self.session.execute(stmt)
        return [self._serialize_gmail(row, score) for row, score in result.all()]

    @classmethod
    def _serialize_gmail(cls, row: GmailCache, score: Any = None) -> dict:
        return {
            "id": row.email_id,
            "email_id": row.email_id,
            "source": "gmail",
            "thread_id": row.thread_id,
            "subject": row.subject,
            "from_name": row.from_name,
            "from_email": row.from_email,
            "to": list(row.to_emails or []),
            "labels": list(row.labels or []),
            "body_preview": row.body_preview,
            "received_at": row.received_at.isoformat() if row.received_at else None,
            "score": cls._score_value(score),
        }

    # ------------------------------------------------------------------ #
    # Calendar                                                           #
    # ------------------------------------------------------------------ #
    async def search_gcal(self, user_id: uuid.UUID, params: dict) -> list[dict]:
        query = _query_text(params.get("query"))
        k = int(params.get("k") or 10)
        conditions = [GcalCache.user_id == user_id]
        if params.get("attendee"):
            conditions.append(GcalCache.attendee_emails.any(params["attendee"]))
        starts_after = _parse_iso(params.get("starts_after"))
        if starts_after is not None:
            conditions.append(GcalCache.start_time >= starts_after)
        starts_before = _parse_iso(params.get("starts_before"))
        if starts_before is not None:
            conditions.append(GcalCache.start_time < starts_before)

        recency = self._recency_expr(GcalCache.start_time, use_abs=True)
        if query:
            qvec = await self.embedder.embed(query)
            score = self._score_expr(GcalCache.embedding, qvec, recency)
            stmt = (
                select(GcalCache, score.label("score"))
                .where(*conditions)
                .order_by(score.desc())
                .limit(k)
            )
        else:
            stmt = (
                select(GcalCache, recency.label("score"))
                .where(*conditions)
                .order_by(nulls_last(GcalCache.start_time.asc()))
                .limit(k)
            )
        result = await self.session.execute(stmt)
        return [self._serialize_gcal(row, score) for row, score in result.all()]

    @classmethod
    def _serialize_gcal(cls, row: GcalCache, score: Any = None) -> dict:
        return {
            "id": row.event_id,
            "event_id": row.event_id,
            "source": "gcal",
            "title": row.title,
            "description": row.description,
            "location": row.location,
            "organizer_email": row.organizer_email,
            "attendees": list(row.attendee_emails or []),
            "start": row.start_time.isoformat() if row.start_time else None,
            "end": row.end_time.isoformat() if row.end_time else None,
            "status": row.status,
            "score": cls._score_value(score),
        }

    # ------------------------------------------------------------------ #
    # Drive                                                              #
    # ------------------------------------------------------------------ #
    async def search_gdrive(self, user_id: uuid.UUID, params: dict) -> list[dict]:
        query = _query_text(params.get("query"))
        k = int(params.get("k") or 5)
        conditions = [GdriveCache.user_id == user_id]
        if params.get("mime_type"):
            conditions.append(GdriveCache.mime_type == params["mime_type"])
        if params.get("owner"):
            conditions.append(GdriveCache.owner_email.ilike(f"%{params['owner']}%"))
        modified_after = _parse_iso(params.get("modified_after"))
        if modified_after is not None:
            conditions.append(GdriveCache.modified_at >= modified_after)
        modified_before = _parse_iso(params.get("modified_before"))
        if modified_before is not None:
            conditions.append(GdriveCache.modified_at < modified_before)

        recency = self._recency_expr(GdriveCache.modified_at)
        if query:
            qvec = await self.embedder.embed(query)
            score = self._score_expr(GdriveCache.embedding, qvec, recency)
            stmt = (
                select(GdriveCache, score.label("score"))
                .where(*conditions)
                .order_by(score.desc())
                .limit(k)
            )
        else:
            stmt = (
                select(GdriveCache, recency.label("score"))
                .where(*conditions)
                .order_by(nulls_last(GdriveCache.modified_at.desc()))
                .limit(k)
            )
        result = await self.session.execute(stmt)
        return [self._serialize_gdrive(row, score) for row, score in result.all()]

    @classmethod
    def _serialize_gdrive(cls, row: GdriveCache, score: Any = None) -> dict:
        return {
            "id": row.file_id,
            "file_id": row.file_id,
            "source": "gdrive",
            "name": row.name,
            "mime_type": row.mime_type,
            "owner_email": row.owner_email,
            "content_preview": row.content_preview,
            "web_link": row.web_link,
            "modified_at": row.modified_at.isoformat() if row.modified_at else None,
            "score": cls._score_value(score),
        }

    # ------------------------------------------------------------------ #
    # Cache lookups                                                      #
    # ------------------------------------------------------------------ #
    async def get_by_source_id(
        self, source: str, user_id: uuid.UUID, source_id: str
    ) -> dict | None:
        """Return a single cached row (as a serialized dict) or ``None``."""
        if source == "gmail":
            stmt = select(GmailCache).where(
                GmailCache.user_id == user_id, GmailCache.email_id == source_id
            )
            row = (await self.session.execute(stmt)).scalar_one_or_none()
            return self._serialize_gmail(row) if row is not None else None
        if source == "gcal":
            stmt = select(GcalCache).where(
                GcalCache.user_id == user_id, GcalCache.event_id == source_id
            )
            row = (await self.session.execute(stmt)).scalar_one_or_none()
            return self._serialize_gcal(row) if row is not None else None
        if source == "gdrive":
            stmt = select(GdriveCache).where(
                GdriveCache.user_id == user_id, GdriveCache.file_id == source_id
            )
            row = (await self.session.execute(stmt)).scalar_one_or_none()
            return self._serialize_gdrive(row) if row is not None else None
        raise ValueError(f"unknown source: {source}")

    async def find_overlapping(
        self,
        user_id: uuid.UUID,
        start: datetime,
        end: datetime,
        exclude_event_id: str | None = None,
    ) -> list[dict]:
        """Return non-cancelled cached events overlapping ``[start, end)``."""
        conditions = [
            GcalCache.user_id == user_id,
            GcalCache.status != "cancelled",
            GcalCache.start_time < end,
            GcalCache.end_time > start,
        ]
        if exclude_event_id is not None:
            conditions.append(GcalCache.event_id != exclude_event_id)
        stmt = select(GcalCache).where(*conditions).order_by(GcalCache.start_time.asc())
        rows = (await self.session.execute(stmt)).scalars().all()
        return [self._serialize_gcal(row) for row in rows]
