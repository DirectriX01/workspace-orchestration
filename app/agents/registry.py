"""Agent registry: build the per-user gmail/calendar/drive agent bundle.

:func:`build_agents` wires an :class:`~app.search.embeddings.EmbeddingService`
and :class:`~app.search.hybrid.HybridSearcher` (sharing the request's async
session) together with per-service clients from the service factory, returning a
name -> agent mapping suitable for the DAG executor.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentDeps, BaseAgent
from app.agents.calendar_agent import CalendarAgent
from app.agents.drive_agent import DriveAgent
from app.agents.gmail_agent import GmailAgent
from app.search.embeddings import EmbeddingService
from app.search.hybrid import HybridSearcher
from app.services.factory import (
    get_calendar_client,
    get_drive_client,
    get_gmail_client,
)


async def build_agents(
    user: Any, session: AsyncSession, redis_async: Any
) -> dict[str, BaseAgent]:
    """Construct the gmail/calendar/drive agents for ``user``."""
    embedder = EmbeddingService(redis_async=redis_async)
    searcher = HybridSearcher(session, embedder)
    return {
        "gmail": GmailAgent(
            AgentDeps(
                user=user,
                client=get_gmail_client(user),
                searcher=searcher,
                embedder=embedder,
            )
        ),
        "calendar": CalendarAgent(
            AgentDeps(
                user=user,
                client=get_calendar_client(user),
                searcher=searcher,
                embedder=embedder,
            )
        ),
        "drive": DriveAgent(
            AgentDeps(
                user=user,
                client=get_drive_client(user),
                searcher=searcher,
                embedder=embedder,
            )
        ),
    }
