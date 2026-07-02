"""Agent registry: build the per-user gmail/calendar/drive agent bundle.

:func:`build_agents` wires an :class:`~app.search.embeddings.EmbeddingService`
and :class:`~app.search.hybrid.HybridSearcher` (sharing the request's async
session) together with per-service clients from the service factory, returning a
name -> agent mapping suitable for the DAG executor.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentDeps, BaseAgent
from app.agents.calendar_agent import CalendarAgent
from app.agents.drive_agent import DriveAgent
from app.agents.gmail_agent import GmailAgent
from app.config import get_settings
from app.search.embeddings import EmbeddingService
from app.search.hybrid import HybridSearcher
from app.services.factory import (
    get_calendar_client,
    get_drive_client,
    get_gmail_client,
)


def _embedding_redis() -> Any:
    """A binary-safe async Redis client for the embedding cache.

    :class:`EmbeddingService` stores embeddings as raw float32 bytes, so its
    Redis client MUST be binary-safe (``decode_responses=False``). The shared
    ``app.state.redis`` client is created with ``decode_responses=True`` (for
    pub/sub and the conversation store), which would raise ``UnicodeDecodeError``
    when the cached bytes are read back. A dedicated binary-safe client, created
    on the running loop, keeps the embedding cache correct.
    """
    return aioredis.from_url(get_settings().redis_url)


async def build_agents(
    user: Any, session: AsyncSession, redis_async: Any
) -> dict[str, BaseAgent]:
    """Construct the gmail/calendar/drive agents for ``user``."""
    embedder = EmbeddingService(redis_async=_embedding_redis())
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
