"""End-to-end API tests driving the full query pipeline through the ASGI app.

Everything runs against the local stack (Postgres :5433, Redis :6380) with the
fake LLM / fake embeddings / mock Google clients. All requests carry a dedicated
``X-User-Email`` so the assertions never collide with other users' rows. The
pipeline is deterministic here because :class:`app.llm.fake.FakeLLM` drives both
intent classification and response synthesis.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

#: Dedicated, isolated user for every request in this module.
E2E_EMAIL = "e2e-test@example.com"
_HEADERS = {"X-User-Email": E2E_EMAIL}


async def _ask(client: Any, query: str, conversation_id: str | None) -> dict:
    """POST one turn to the query endpoint and return the decoded JSON body."""
    response = await client.post(
        "/api/v1/query",
        json={"query": query, "conversation_id": conversation_id},
        headers=_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_query_pipeline_end_to_end(client: Any) -> None:
    # ------------------------------------------------------------------ #
    # Seed this user's cache synchronously via an inline sync.           #
    # ------------------------------------------------------------------ #
    trigger = await client.post(
        "/api/v1/sync/trigger", params={"inline": "true"}, headers=_HEADERS
    )
    assert trigger.status_code == 200, trigger.text
    assert trigger.json()["mode"] == "inline"

    # ------------------------------------------------------------------ #
    # (1) Calendar search: real plan, ok step, non-empty results.        #
    # ------------------------------------------------------------------ #
    calendar = await _ask(client, "What's on my calendar next week?", None)
    assert calendar["answer"].strip()
    calendar_steps = [
        step
        for step in calendar["plan"]
        if step["agent"] == "calendar" and step["action"] == "search_events"
    ]
    assert calendar_steps, calendar["plan"]
    calendar_step = calendar_steps[0]
    assert calendar_step["status"] == "ok"
    assert calendar["results"][calendar_step["id"]]["results"]

    # ------------------------------------------------------------------ #
    # (2) Flight cancellation: multi-step plan + a send_email pending.    #
    # ------------------------------------------------------------------ #
    cancel = await _ask(client, "Cancel my Turkish Airlines flight", None)
    cid = cancel["conversation_id"]
    step_ids = {step["id"] for step in cancel["plan"]}
    assert {"find_booking", "find_flight_event", "draft_cancellation"} <= step_ids
    assert cancel["pending_action"] is not None
    assert cancel["pending_action"]["action"] == "send_email"
    assert "confirm" in cancel["answer"].lower()

    # ...confirm it in the SAME conversation -> the pending action executes.
    confirm = await _ask(client, "yes, send it", cid)
    assert confirm["pending_action"] is None
    confirm_answer = confirm["answer"].lower()
    assert "sent" in confirm_answer or "executed" in confirm_answer

    # ...and the pending action is now cleared: a further confirm finds nothing.
    again = await _ask(client, "yes, send it", cid)
    assert "no pending action" in again["answer"].lower()

    # ------------------------------------------------------------------ #
    # (3) Ambiguous mutation -> clarification, no plan executed.          #
    # ------------------------------------------------------------------ #
    clarify = await _ask(client, "Move the meeting with John", None)
    assert clarify["needs_clarification"] is True
    assert clarify["plan"] == []

    # ------------------------------------------------------------------ #
    # (4) Chitchat -> a friendly answer with an empty plan.              #
    # ------------------------------------------------------------------ #
    chitchat = await _ask(client, "hello there", None)
    assert chitchat["plan"] == []
    assert chitchat["needs_clarification"] is False
    assert chitchat["answer"].strip()

    # ------------------------------------------------------------------ #
    # (5) Sync status: all three services idle with rows synced.         #
    # ------------------------------------------------------------------ #
    status = await client.get("/api/v1/sync/status", headers=_HEADERS)
    assert status.status_code == 200, status.text
    statuses = status.json()["statuses"]
    assert {row["service"] for row in statuses} == {"gmail", "calendar", "drive"}
    assert all(row["status"] == "idle" for row in statuses)
    assert all(row["items_synced"] > 0 for row in statuses)
