"""Integration tests for :class:`app.search.hybrid.HybridSearcher`.

These exercise the real pgvector + recency search against a dedicated user's
freshly-synced cache rows (Postgres :5433, Redis :6380, fake embeddings). All
assertions are scoped to ``hybrid-test@example.com`` so pre-existing rows for
other users never leak in.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

#: Dedicated, isolated user for every assertion in this module.
HYBRID_EMAIL = "hybrid-test@example.com"
#: Sync the fixture data at most once per test process (upserts are idempotent).
_SYNCED = False


def _now() -> datetime:
    from app.config import get_settings

    return datetime.now(ZoneInfo(get_settings().default_tz))


@pytest_asyncio.fixture
async def synced_user(stack_available: None, reset_async_engine: None) -> Any:
    """Return the id of a synced ``hybrid-test`` user (create + sync once)."""
    from sqlalchemy import select

    from app.db.models import User
    from app.db.session import get_async_session_factory
    from app.sync.tasks import sync_calendar, sync_drive, sync_gmail

    factory = get_async_session_factory()
    async with factory() as session:
        user = (
            await session.execute(select(User).where(User.email == HYBRID_EMAIL))
        ).scalar_one_or_none()
        if user is None:
            user = User(email=HYBRID_EMAIL, timezone="Asia/Kolkata")
            session.add(user)
            await session.commit()
            await session.refresh(user)
        user_id = user.id

    global _SYNCED
    if not _SYNCED:
        uid = str(user_id)
        # Run the (async-internally) sync tasks off the event loop: each task
        # calls ``asyncio.run`` internally, which would explode on a live loop.
        await asyncio.to_thread(sync_gmail, uid, full=True)
        await asyncio.to_thread(sync_calendar, uid, full=True)
        await asyncio.to_thread(sync_drive, uid, full=True)
        _SYNCED = True
    return user_id


def _make_searcher(session: Any) -> Any:
    from app.search.embeddings import EmbeddingService
    from app.search.hybrid import HybridSearcher

    # No Redis cache on the search path: fake embeddings are cheap and this
    # keeps latency measurements honest (and dodges any binary-cache concerns).
    return HybridSearcher(session, EmbeddingService(redis_async=None))


# --------------------------------------------------------------------------- #
# (1) Gmail: from_email filter returns exactly the three Acme-thread emails.   #
# --------------------------------------------------------------------------- #
async def test_gmail_from_email_filter_returns_three_acme_emails(
    synced_user: Any,
) -> None:
    from app.db.session import get_async_session_factory

    factory = get_async_session_factory()
    async with factory() as session:
        rows = await _make_searcher(session).search_gmail(
            synced_user,
            {"query": "Acme partnership Q3 proposal", "from_email": "sarah@acmecorp.com", "k": 10},
        )

    assert len(rows) == 3, [r["subject"] for r in rows]
    assert {r["id"] for r in rows} == {"msg_003", "msg_004", "msg_005"}
    assert all(r["from_email"] == "sarah@acmecorp.com" for r in rows)

    # A free-text query means the rows come back ranked (semantic + recency).
    scores = [r["score"] for r in rows]
    assert all(isinstance(s, float) for s in scores)
    assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------------------- #
# (2) Calendar: attendee + time-window filter returns >= 4 events with John.   #
# --------------------------------------------------------------------------- #
async def test_calendar_attendee_and_window_filter(synced_user: Any) -> None:
    from app.db.session import get_async_session_factory

    now = _now()
    factory = get_async_session_factory()
    async with factory() as session:
        rows = await _make_searcher(session).search_gcal(
            synced_user,
            {
                "attendee": "john@company.com",
                "starts_after": now.isoformat(),
                "starts_before": (now + timedelta(days=14)).isoformat(),
                "k": 20,
            },
        )

    assert len(rows) >= 4
    assert all("john@company.com" in r["attendees"] for r in rows)

    titles = [r["title"] for r in rows]
    assert titles.count("Sync with John") == 2
    assert "Product roadmap sync" in titles
    assert "API integration kickoff" in titles


# --------------------------------------------------------------------------- #
# (3) Drive: mime-type filter returns exactly the two PDFs.                    #
# --------------------------------------------------------------------------- #
async def test_drive_pdf_mime_filter(synced_user: Any) -> None:
    from app.db.session import get_async_session_factory

    factory = get_async_session_factory()
    async with factory() as session:
        rows = await _make_searcher(session).search_gdrive(
            synced_user,
            {"query": "itinerary onboarding checklist", "mime_type": "application/pdf", "k": 10},
        )

    assert len(rows) == 2, [r["name"] for r in rows]
    assert {r["id"] for r in rows} == {"file_005", "file_008"}
    assert all(r["mime_type"] == "application/pdf" for r in rows)


# --------------------------------------------------------------------------- #
# (4) Calendar: empty query over the next-week window is ordered by start.     #
# --------------------------------------------------------------------------- #
async def test_calendar_empty_query_ordered_by_start(synced_user: Any) -> None:
    from app.config import get_settings
    from app.core.temporal import resolve_temporal
    from app.db.session import get_async_session_factory

    tz = get_settings().default_tz
    now = _now()
    week = resolve_temporal("next week", now, tz)
    assert week is not None and week.start is not None and week.end is not None

    factory = get_async_session_factory()
    async with factory() as session:
        rows = await _make_searcher(session).search_gcal(
            synced_user,
            {"starts_after": week.start.isoformat(), "starts_before": week.end.isoformat()},
        )

    assert rows, "expected at least one event in the next-week window"
    starts = [datetime.fromisoformat(r["start"]) for r in rows]
    assert starts == sorted(starts)


# --------------------------------------------------------------------------- #
# (5) Latency: the filtered Gmail query stays well under 500ms across repeats. #
# --------------------------------------------------------------------------- #
async def test_gmail_search_latency_under_budget(synced_user: Any) -> None:
    from app.db.session import get_async_session_factory

    params = {
        "query": "Acme partnership Q3 proposal",
        "from_email": "sarah@acmecorp.com",
        "k": 5,
    }
    factory = get_async_session_factory()
    async with factory() as session:
        searcher = _make_searcher(session)
        latencies: list[float] = []
        for _ in range(5):
            started = time.perf_counter()
            rows = await searcher.search_gmail(synced_user, params)
            latencies.append((time.perf_counter() - started) * 1000)
            assert len(rows) == 3

    assert all(latency < 500.0 for latency in latencies), latencies
