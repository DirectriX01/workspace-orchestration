"""Integration test for the per-conversation WebSocket step-update stream.

A real uvicorn server is started in a background thread so the app runs on its
own event loop; the app's async DB engine and shared Redis client are therefore
created on the server loop and never collide with the client-side loop that this
test drives with ``asyncio.run``. A raw ``websockets`` client subscribes to
``/api/v1/ws/{conversation_id}`` and then a query is POSTed on that same
conversation id. The DAG executor publishes one ``step_update`` per step
transition to a Redis pub/sub channel, which the endpoint forwards to the
socket, so we assert at least one ``{"type": "step_update"}`` frame (with a
``running`` or terminal status) arrives within the budget.

The whole test skips cleanly when the local stack is down via the shared
``stack_available`` guard (pulled in by the ``live_server`` fixture).
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from typing import Iterator
from uuid import uuid4

import httpx
import pytest

# The client-side WebSocket + the ASGI server are hard requirements here; skip
# the whole module cleanly if either is somehow unavailable.
websockets = pytest.importorskip("websockets")
uvicorn = pytest.importorskip("uvicorn")

#: Dedicated, isolated user so this turn never collides with other users' rows.
WS_EMAIL = "ws-test@example.com"
_HEADERS = {"X-User-Email": WS_EMAIL}

#: Every status the executor can emit for a step (``running`` first, then one
#: terminal status). Any of these on a ``step_update`` frame satisfies the test.
_STEP_STATUSES = frozenset(
    {
        "running",
        "ok",
        "empty",
        "failed",
        "skipped",
        "ambiguous",
        "conflict",
        "pending_confirmation",
    }
)


def _free_port() -> int:
    """Return an ephemeral localhost TCP port (closed again before use)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def live_server(stack_available: None) -> Iterator[str]:
    """Run the ASGI app under uvicorn on a free port in a background thread.

    The global async engine is reset around the server's lifetime so it is
    (re)created on the *server's* event loop rather than a loop left over from an
    earlier pytest-asyncio test.
    """
    from app.db import session as db_session
    from app.main import app

    db_session._async_engine = None
    db_session._async_session_factory = None

    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10.0
    while not server.started:
        if time.time() > deadline:
            server.should_exit = True
            thread.join(timeout=5.0)
            pytest.fail("uvicorn did not start within 10s")
        time.sleep(0.05)

    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        db_session._async_engine = None
        db_session._async_session_factory = None


def test_ws_streams_step_updates(live_server: str) -> None:
    base_url = f"http://{live_server}"
    ws_url = f"ws://{live_server}/api/v1/ws"
    cid = uuid4().hex

    async def _flow() -> None:
        loop = asyncio.get_running_loop()
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
            # Seed this user's cache so the calendar-search step settles cleanly.
            sync = await http.post(
                "/api/v1/sync/trigger", params={"inline": "true"}, headers=_HEADERS
            )
            assert sync.status_code == 200, sync.text

            async with websockets.connect(f"{ws_url}/{cid}") as ws:
                # Give the endpoint a beat to finish SUBSCRIBE before the query
                # starts publishing: Redis pub/sub drops anything sent before the
                # subscription is live.
                await asyncio.sleep(0.5)

                post = asyncio.create_task(
                    http.post(
                        "/api/v1/query",
                        json={
                            "query": "What's on my calendar next week?",
                            "conversation_id": cid,
                        },
                        headers=_HEADERS,
                    )
                )

                found: dict | None = None
                deadline = loop.time() + 10.0
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    frame = json.loads(raw)
                    if (
                        frame.get("type") == "step_update"
                        and frame.get("status") in _STEP_STATUSES
                    ):
                        found = frame
                        break

                response = await post
                assert response.status_code == 200, response.text
                assert found is not None, "no step_update frame arrived within 10s"
                # The frame carries the step id it refers to.
                assert isinstance(found.get("step"), str) and found["step"]

    asyncio.run(_flow())
