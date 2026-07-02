"""Integration tests for the calendar agent's conflict-aware ``create_event``.

The calendar agent runs an overlap check against the user's freshly-synced
``gcal_cache`` *before* touching the client, so a create in an occupied slot must
come back ``{"status": "conflict"}`` listing the clashing events and must NOT
write a new row, while a create in a free slot must succeed and be immediately
visible through the write-through cache (a subsequent single-doc fetch and a
window search both find it).

Everything runs against the local stack (Postgres :5433, Redis :6380) with the
fake LLM / fake embeddings / mock Google clients. Assertions are scoped to a
dedicated ``conflict-test@example.com`` whose calendar cache is wiped and
re-synced from the fixtures at the start of every run, so an event created by a
previous run can never leak the free slot into a false conflict.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

#: Dedicated, isolated user for every assertion in this module.
CONFLICT_EMAIL = "conflict-test@example.com"

#: Distinctive titles so the "was anything written?" checks are exact.
_CONFLICT_TITLE = "CONFLICT-PROBE do-not-create"
_FREE_TITLE = "FREE-SLOT-PROBE early morning hold"


@pytest_asyncio.fixture
async def conflict_user(stack_available: None, reset_async_engine: None) -> Any:
    """Return the id of a dedicated user with a pristine, fixture-only calendar.

    The gcal cache is wiped and re-synced on every run: ``sync_calendar`` upserts
    the 12 fixture events (evt_001..012) but never removes an ``evt_new_*`` row
    left behind by an earlier successful create, so wiping first is what keeps
    the free 03:00 slot genuinely free from one run to the next.
    """
    from sqlalchemy import delete, select

    from app.db.models import GcalCache, User
    from app.db.session import get_async_session_factory
    from app.sync.tasks import sync_calendar

    factory = get_async_session_factory()
    async with factory() as session:
        user = (
            await session.execute(select(User).where(User.email == CONFLICT_EMAIL))
        ).scalar_one_or_none()
        if user is None:
            user = User(email=CONFLICT_EMAIL, timezone="Asia/Kolkata")
            session.add(user)
            await session.commit()
            await session.refresh(user)
        user_id = user.id
        await session.execute(delete(GcalCache).where(GcalCache.user_id == user_id))
        await session.commit()

    # sync_calendar drives its own event loop internally (asyncio.run), so it
    # must run off the test's loop; it also uses the *sync* engine, so it never
    # touches the async engine bound to this test.
    await asyncio.to_thread(sync_calendar, str(user_id), full=True)
    return user_id


async def test_create_event_conflict_then_free_slot(conflict_user: Any) -> None:
    from sqlalchemy import func, select

    from app.agents.registry import build_agents
    from app.db.models import GcalCache, User
    from app.db.session import get_async_session_factory

    user_id = conflict_user
    factory = get_async_session_factory()
    async with factory() as session:
        user = await session.get(User, user_id)
        # ``build_agents`` ignores its redis arg (it builds its own binary-safe
        # embedding client), so None is fine here.
        agents = await build_agents(user, session, None)
        calendar = agents["calendar"]
        searcher = calendar.deps.searcher

        # evt_007 (60 min) and evt_008 (30 min) both start at day+6 14:00 local,
        # so evt_007's exact window overlaps both.
        evt7 = await searcher.get_by_source_id("gcal", user_id, "evt_007")
        evt8 = await searcher.get_by_source_id("gcal", user_id, "evt_008")
        assert evt7 is not None and evt8 is not None
        occupied_start = evt7["start"]
        occupied_end = evt7["end"]

        # ---------------------------------------------------------------- #
        # (1) Create over the occupied window -> conflict; nothing written. #
        # ---------------------------------------------------------------- #
        conflict = await calendar.run(
            "create_event",
            {
                "title": _CONFLICT_TITLE,
                "start": occupied_start,
                "end": occupied_end,
                "attendees": [CONFLICT_EMAIL],
            },
        )
        assert conflict["status"] == "conflict", conflict
        # A conflict short-circuits before the client call, so there is no
        # created-event payload (no "id"), only the clashing events.
        assert "id" not in conflict
        clash_ids = {c["id"] for c in conflict["conflicts"]}
        assert {"evt_007", "evt_008"} <= clash_ids, clash_ids

        # No row was persisted for the probe title (the write never happened).
        written = (
            await session.execute(
                select(func.count())
                .select_from(GcalCache)
                .where(
                    GcalCache.user_id == user_id,
                    GcalCache.title == _CONFLICT_TITLE,
                )
            )
        ).scalar_one()
        assert written == 0

        # ---------------------------------------------------------------- #
        # (2) Create in a free 03:00 slot -> ok + write-through visibility.  #
        # ---------------------------------------------------------------- #
        free_start = datetime.fromisoformat(occupied_start).replace(
            hour=3, minute=0, second=0, microsecond=0
        )
        free_end = free_start + timedelta(hours=1)
        created = await calendar.run(
            "create_event",
            {
                "title": _FREE_TITLE,
                "start": free_start.isoformat(),
                "end": free_end.isoformat(),
                "attendees": [CONFLICT_EMAIL],
                "description": "held open by the conflict integration test",
            },
        )
        assert created["status"] == "ok", created
        new_id = created["id"]
        assert new_id

        # Write-through: the cache-first single-doc fetch sees it immediately.
        fetched = await searcher.get_by_source_id("gcal", user_id, new_id)
        assert fetched is not None
        assert fetched["title"] == _FREE_TITLE

        # Write-through: a pure-metadata window search returns the new id. The
        # window (02:59 -> 04:00 on the same day) holds only this new event.
        window = await searcher.search_gcal(
            user_id,
            {
                "starts_after": (free_start - timedelta(minutes=1)).isoformat(),
                "starts_before": free_end.isoformat(),
            },
        )
        assert new_id in {row["id"] for row in window}
