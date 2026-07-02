"""Lazy database engine and session factories.

Importing this module never opens a connection; engines and session
factories are created on first use so that tooling (Alembic, tests) can
import models without a live database.
"""

from collections.abc import AsyncGenerator

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_async_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None
_sync_engine: Engine | None = None
_sync_session_factory: sessionmaker[Session] | None = None


def get_async_engine() -> AsyncEngine:
    """Return the process-wide async (asyncpg) engine, creating it lazily."""
    global _async_engine
    if _async_engine is None:
        _async_engine = create_async_engine(get_settings().database_url)
    return _async_engine


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the async session factory, creating it lazily."""
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            bind=get_async_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _async_session_factory


def get_sync_engine() -> Engine:
    """Return the process-wide sync (psycopg) engine, creating it lazily."""
    global _sync_engine
    if _sync_engine is None:
        _sync_engine = create_engine(get_settings().sync_database_url)
    return _sync_engine


def get_sync_session_factory() -> sessionmaker[Session]:
    """Return the sync session factory, creating it lazily."""
    global _sync_session_factory
    if _sync_session_factory is None:
        _sync_session_factory = sessionmaker(
            bind=get_sync_engine(),
            expire_on_commit=False,
        )
    return _sync_session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an AsyncSession."""
    factory = get_async_session_factory()
    async with factory() as session:
        yield session
