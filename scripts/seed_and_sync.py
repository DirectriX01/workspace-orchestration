"""Seed the demo user and run a full workspace sync, then smoke-test search.

Standalone entrypoint (adds the project root to ``sys.path``). It ensures a
demo user row exists, calls the three sync tasks directly as functions with
``full=True``, prints per-table row counts, and finally runs one sample hybrid
search over Gmail, printing scores and latency.

Run against the local docker stack (Postgres :5433, Redis :6380)::

    EMBEDDINGS_PROVIDER=fake MOCK_GOOGLE=true python scripts/seed_and_sync.py

If :mod:`app.search.hybrid` is not present yet (a concurrent build target), the
sample search is skipped with a note and only the counts are reported.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models import (  # noqa: E402
    GcalCache,
    GdriveCache,
    GmailCache,
    User,
)
from app.db.session import (  # noqa: E402
    get_async_session_factory,
    get_sync_session_factory,
)
from app.sync.tasks import sync_calendar, sync_drive, sync_gmail  # noqa: E402

_SAMPLE_QUERY = "turkish airlines flight booking"


def ensure_demo_user() -> str:
    """Ensure the demo user row exists; return its id as a string."""
    settings = get_settings()
    session_factory = get_sync_session_factory()
    with session_factory() as session:
        user = session.execute(
            select(User).where(User.email == settings.demo_user_email)
        ).scalar_one_or_none()
        if user is None:
            user = User(email=settings.demo_user_email, timezone=settings.default_tz)
            session.add(user)
            session.commit()
            session.refresh(user)
        return str(user.id)


def print_counts(user_id: str) -> None:
    """Print per-cache-table row counts for the user."""
    user_uuid = uuid.UUID(user_id)
    session_factory = get_sync_session_factory()
    tables = [
        ("gmail_cache", GmailCache),
        ("gcal_cache", GcalCache),
        ("gdrive_cache", GdriveCache),
    ]
    with session_factory() as session:
        for label, model in tables:
            count = session.execute(
                select(func.count())
                .select_from(model)
                .where(model.user_id == user_uuid)
            ).scalar_one()
            print(f"  {label}: {count} rows")


async def sample_search(user_id: str) -> None:
    """Run one hybrid Gmail search and print scores + latency (best effort)."""
    try:
        from app.search.hybrid import HybridSearcher
    except ImportError as exc:
        print(f"  [note] app.search.hybrid unavailable ({exc}); skipping sample search")
        return

    from app.search.embeddings import EmbeddingService

    user_uuid = uuid.UUID(user_id)
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        searcher = HybridSearcher(session, EmbeddingService())
        started = time.perf_counter()
        rows = await searcher.search_gmail(
            user_uuid, {"query": _SAMPLE_QUERY, "k": 5}
        )
        latency_ms = (time.perf_counter() - started) * 1000
        print(f"  query {_SAMPLE_QUERY!r} -> {len(rows)} rows in {latency_ms:.1f} ms")
        for row in rows:
            print(f"    {row.get('score')!s:>8}  {row.get('subject')}")


def main() -> None:
    settings = get_settings()
    print(
        f"provider(embeddings)={settings.embeddings_provider} "
        f"mock_google={settings.mock_google}"
    )
    user_id = ensure_demo_user()
    print(f"demo user: {settings.demo_user_email} ({user_id})")

    print("syncing...")
    for label, task in (
        ("gmail", sync_gmail),
        ("calendar", sync_calendar),
        ("drive", sync_drive),
    ):
        synced = task(user_id, full=True)
        print(f"  {label}: {synced} items")

    print("cache counts:")
    print_counts(user_id)

    print("sample search:")
    asyncio.run(sample_search(user_id))


if __name__ == "__main__":
    main()
