"""Shared pytest fixtures and environment setup for the test-suite.

The three provider knobs are forced to their deterministic, network-free modes
*before* any ``app`` module is imported (and before ``get_settings`` is first
cached), so both the unit tests and the integration tests run without OpenAI or
real Google credentials. Integration tests additionally require the local
Postgres/Redis stack; a session-scoped skip-guard fixture short-circuits them
with a clear reason when the stack is unreachable.
"""

from __future__ import annotations

import os

# --------------------------------------------------------------------------- #
# Force deterministic providers BEFORE importing anything from ``app``.        #
# Environment variables take precedence over any values in a local .env file,  #
# so this reliably pins the fake LLM / fake embeddings / mock Google clients.  #
# --------------------------------------------------------------------------- #
os.environ["LLM_PROVIDER"] = "fake"
os.environ["EMBEDDINGS_PROVIDER"] = "fake"
os.environ["MOCK_GOOGLE"] = "true"

import asyncio  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

#: Local stack coordinates (docker-compose maps Postgres->5433, Redis->6380).
_PG_HOST = "localhost"
_PG_PORT = 5433
_PG_USER = "postgres"
_PG_PASSWORD = "postgres"
_PG_DB = "orchestrator"


@pytest.fixture(scope="session", autouse=True)
def _force_fake_providers() -> None:
    """Re-assert the fake providers and clear the settings cache once."""
    os.environ["LLM_PROVIDER"] = "fake"
    os.environ["EMBEDDINGS_PROVIDER"] = "fake"
    os.environ["MOCK_GOOGLE"] = "true"

    from app.config import get_settings

    get_settings.cache_clear()


@pytest.fixture(scope="session")
def stack_available() -> None:
    """Skip the requesting test unless local Postgres AND Redis are reachable."""
    import redis as sync_redis

    from app.config import get_settings

    settings = get_settings()

    try:
        client = sync_redis.from_url(settings.redis_url)
        client.ping()
        client.close()
    except Exception:  # noqa: BLE001 - any failure means "stack not running"
        pytest.skip("local stack not running (redis unreachable)")

    async def _probe_postgres() -> None:
        import asyncpg

        conn = await asyncpg.connect(
            host=_PG_HOST,
            port=_PG_PORT,
            user=_PG_USER,
            password=_PG_PASSWORD,
            database=_PG_DB,
        )
        await conn.close()

    try:
        asyncio.run(_probe_postgres())
    except Exception:  # noqa: BLE001 - any failure means "stack not running"
        pytest.skip("local stack not running (postgres unreachable)")


@pytest_asyncio.fixture
async def reset_async_engine() -> AsyncIterator[None]:
    """Bind the app's async DB engine to *this* test's event loop.

    pytest-asyncio gives each test function a fresh event loop, but the async
    engine / session factory are process-global singletons. An asyncpg pool
    created on one loop cannot be reused on another, so the cached engine is
    dropped before the test (forcing lazy re-creation on the current loop) and
    disposed afterwards.
    """
    from app.db import session as db_session

    db_session._async_engine = None
    db_session._async_session_factory = None
    try:
        yield
    finally:
        engine = db_session._async_engine
        if engine is not None:
            await engine.dispose()
        db_session._async_engine = None
        db_session._async_session_factory = None


@pytest_asyncio.fixture
async def client(
    stack_available: None, reset_async_engine: None
) -> AsyncIterator[Any]:
    """An httpx ``AsyncClient`` bound to the ASGI app with its lifespan run.

    asgi-lifespan is not installed, so the app's lifespan (which opens the
    shared Redis client on ``app.state``) is driven manually via Starlette's
    ``lifespan_context``.
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as http_client:
            yield http_client
