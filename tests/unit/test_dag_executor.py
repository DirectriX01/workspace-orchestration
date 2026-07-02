"""Unit tests for :mod:`app.core.dag` — DAG execution and param resolution.

These tests use lightweight in-process fake agents (classes exposing an
``async run(action, params)`` method) and never touch Postgres/Redis/OpenAI.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.dag import (
    DAGExecutor,
    ExecutionPlan,
    ParamResolutionError,
    PlanError,
    PlanStep,
    StepResult,
    resolve_params,
)


# --------------------------------------------------------------------------- #
# Fake agents
# --------------------------------------------------------------------------- #


class RecordingAgent:
    """Returns a fixed payload and records every ``run`` call."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload if payload is not None else {"status": "ok", "results": []}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, params))
        return self.payload


class RaisingAgent:
    """Always raises, recording that it was invoked."""

    def __init__(self, message: str = "boom") -> None:
        self.message = message
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, params))
        raise RuntimeError(self.message)


class SlowAgent:
    """Sleeps a fixed duration so a step accrues measurable latency."""

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay

    async def run(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(self.delay)
        return {"status": "ok", "results": []}


class ConcurrencyAgent:
    """Tracks peak concurrent invocations via a shared counter."""

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.current = 0
        self.max_concurrent = 0

    async def run(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.current += 1
        self.max_concurrent = max(self.max_concurrent, self.current)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.current -= 1
        return {"status": "ok", "results": []}


def _two_rows() -> dict[str, Any]:
    return {
        "status": "ok",
        "results": [
            {"id": "e1", "subject": "First", "from_email": "a@x.com", "score": 0.9},
            {"id": "e2", "subject": "Second", "from_email": "b@x.com", "score": 0.8},
        ],
    }


# --------------------------------------------------------------------------- #
# (1) Independent steps run concurrently
# --------------------------------------------------------------------------- #


async def test_independent_steps_run_concurrently() -> None:
    agent = ConcurrencyAgent()
    executor = DAGExecutor({"gmail": agent, "calendar": agent})
    steps = [
        PlanStep(id="s1", agent="gmail", action="search_emails"),
        PlanStep(id="s2", agent="calendar", action="search_events"),
    ]

    results = await executor.run(steps)

    assert results["s1"].status == "ok"
    assert results["s2"].status == "ok"
    # Both steps overlapped inside the same wave.
    assert agent.max_concurrent == 2


# --------------------------------------------------------------------------- #
# (2) Templated params resolved from upstream results
# --------------------------------------------------------------------------- #


async def test_dependent_step_receives_resolved_templates() -> None:
    upstream = RecordingAgent(_two_rows())
    downstream = RecordingAgent()
    executor = DAGExecutor({"gmail": upstream, "calendar": downstream})
    steps = [
        PlanStep(id="s1", agent="gmail", action="search_emails"),
        PlanStep(
            id="s2",
            agent="calendar",
            action="create_event",
            depends_on=["s1"],
            params={
                # whole-string template -> raw object (type preserved)
                "the_id": "{{s1.top.id}}",
                "the_score": "{{s1.top.score}}",
                "whole_row": "{{s1.top}}",
                # embedded template -> str() substitution
                "subject": "Re: {{s1.results[1].subject}}",
                # template inside a list value
                "to": ["{{s1.top.from_email}}"],
            },
        ),
    ]

    results = await executor.run(steps)

    assert results["s2"].status == "ok"
    assert len(downstream.calls) == 1
    _, resolved = downstream.calls[0]
    assert resolved["the_id"] == "e1"
    # Raw substitution preserves the float type, not str.
    assert resolved["the_score"] == 0.9
    assert isinstance(resolved["the_score"], float)
    assert resolved["whole_row"] == {
        "id": "e1",
        "subject": "First",
        "from_email": "a@x.com",
        "score": 0.9,
    }
    assert resolved["subject"] == "Re: Second"
    assert resolved["to"] == ["a@x.com"]


# --------------------------------------------------------------------------- #
# (3) Failed non-optional dep -> dependent skipped
# --------------------------------------------------------------------------- #


async def test_failed_non_optional_dep_skips_dependent() -> None:
    upstream = RaisingAgent()
    downstream = RecordingAgent()
    executor = DAGExecutor({"gmail": upstream, "calendar": downstream})
    steps = [
        PlanStep(id="s1", agent="gmail", action="search_emails"),
        PlanStep(id="s2", agent="calendar", action="search_events", depends_on=["s1"]),
    ]

    results = await executor.run(steps)

    assert results["s1"].status == "failed"
    assert results["s2"].status == "skipped"
    assert results["s2"].error == "upstream s1 failed"
    assert downstream.calls == []  # never executed


# --------------------------------------------------------------------------- #
# (4) Failed OPTIONAL dep -> dependent still runs
# --------------------------------------------------------------------------- #


async def test_failed_optional_dep_does_not_block_dependent() -> None:
    upstream = RaisingAgent()
    downstream = RecordingAgent()
    executor = DAGExecutor({"gmail": upstream, "calendar": downstream})
    steps = [
        PlanStep(id="s1", agent="gmail", action="search_emails", optional=True),
        # depends on the optional s1 but does NOT reference its result.
        PlanStep(id="s2", agent="calendar", action="search_events", depends_on=["s1"]),
    ]

    results = await executor.run(steps)

    assert results["s1"].status == "failed"
    assert results["s2"].status == "ok"
    assert len(downstream.calls) == 1


# --------------------------------------------------------------------------- #
# (5) Fallback runs on empty primary and replaces it
# --------------------------------------------------------------------------- #


async def test_fallback_replaces_empty_primary() -> None:
    primary = RecordingAgent({"status": "empty", "results": []})
    broad = RecordingAgent(
        {"status": "ok", "results": [{"id": "b1", "subject": "Found broadly"}]}
    )
    executor = DAGExecutor({"gmail": primary, "calendar": broad})
    steps = [
        PlanStep(
            id="s1",
            agent="gmail",
            action="search_emails",
            fallback=PlanStep(id="s1_broad", agent="calendar", action="search_events"),
        )
    ]

    results = await executor.run(steps)

    assert results["s1"].status == "ok"
    assert results["s1"].data["results"][0]["id"] == "b1"
    assert len(primary.calls) == 1
    assert len(broad.calls) == 1


# --------------------------------------------------------------------------- #
# (6) Fallback runs on a raised exception
# --------------------------------------------------------------------------- #


async def test_fallback_replaces_raised_primary() -> None:
    primary = RaisingAgent()
    broad = RecordingAgent(
        {"status": "ok", "results": [{"id": "b1", "subject": "Recovered"}]}
    )
    executor = DAGExecutor({"gmail": primary, "calendar": broad})
    steps = [
        PlanStep(
            id="s1",
            agent="gmail",
            action="search_emails",
            fallback=PlanStep(id="s1_broad", agent="calendar", action="search_events"),
        )
    ]

    results = await executor.run(steps)

    assert results["s1"].status == "ok"
    assert results["s1"].data["results"][0]["id"] == "b1"
    assert len(broad.calls) == 1


# --------------------------------------------------------------------------- #
# (7) expect_single with >1 result -> ambiguous, dependent skipped
# --------------------------------------------------------------------------- #


async def test_expect_single_two_results_is_ambiguous_and_skips_dependent() -> None:
    upstream = RecordingAgent(_two_rows())
    downstream = RecordingAgent()
    executor = DAGExecutor({"gmail": upstream, "calendar": downstream})
    steps = [
        PlanStep(id="s1", agent="gmail", action="search_events", expect_single=True),
        PlanStep(id="s2", agent="calendar", action="update_event", depends_on=["s1"]),
    ]

    results = await executor.run(steps)

    assert results["s1"].status == "ambiguous"
    # Candidates are retained in the ambiguous result's data.
    assert len(results["s1"].data["results"]) == 2
    assert results["s2"].status == "skipped"
    assert results["s2"].error == "upstream s1 ambiguous"
    assert downstream.calls == []


# --------------------------------------------------------------------------- #
# (8) requires_confirmation -> pending_confirmation, agent NOT called
# --------------------------------------------------------------------------- #


async def test_requires_confirmation_defers_without_calling_agent() -> None:
    upstream = RecordingAgent(_two_rows())
    mutating = RecordingAgent()
    executor = DAGExecutor({"gmail": upstream, "calendar": mutating})
    steps = [
        PlanStep(id="s1", agent="gmail", action="search_emails"),
        PlanStep(
            id="s2",
            agent="calendar",
            action="delete_event",
            depends_on=["s1"],
            requires_confirmation=True,
            params={"event_id": "{{s1.top.id}}"},
        ),
    ]

    results = await executor.run(steps)

    assert results["s2"].status == "pending_confirmation"
    assert mutating.calls == []  # confirmation-gated step never runs
    assert results["s2"].data == {
        "agent": "calendar",
        "action": "delete_event",
        "params": {"event_id": "e1"},  # template still resolved
    }


# --------------------------------------------------------------------------- #
# (9) Cycle raises PlanError
# --------------------------------------------------------------------------- #


async def test_cycle_raises_plan_error() -> None:
    agent = RecordingAgent()
    executor = DAGExecutor({"gmail": agent})
    steps = [
        PlanStep(id="s1", agent="gmail", action="search_emails", depends_on=["s2"]),
        PlanStep(id="s2", agent="gmail", action="search_emails", depends_on=["s1"]),
    ]

    with pytest.raises(PlanError):
        await executor.run(steps)


# --------------------------------------------------------------------------- #
# (10) on_event fires running + final for every step
# --------------------------------------------------------------------------- #


async def test_on_event_fires_running_and_final_for_every_step() -> None:
    upstream = RecordingAgent(_two_rows())
    downstream = RecordingAgent()
    events: list[tuple[str, str]] = []
    executor = DAGExecutor(
        {"gmail": upstream, "calendar": downstream},
        on_event=lambda step_id, status: events.append((step_id, status)),
    )
    steps = [
        PlanStep(id="s1", agent="gmail", action="search_emails"),
        PlanStep(id="s2", agent="calendar", action="search_events", depends_on=["s1"]),
    ]

    await executor.run(steps)

    assert ("s1", "running") in events
    assert ("s1", "ok") in events
    assert ("s2", "running") in events
    assert ("s2", "ok") in events
    # Exactly two events per step.
    assert [s for s, _ in events].count("s1") == 2
    assert [s for s, _ in events].count("s2") == 2
    # "running" precedes the final status for each step.
    assert events.index(("s1", "running")) < events.index(("s1", "ok"))
    assert events.index(("s2", "running")) < events.index(("s2", "ok"))


async def test_on_event_fires_for_skipped_step() -> None:
    upstream = RaisingAgent()
    downstream = RecordingAgent()
    events: list[tuple[str, str]] = []
    executor = DAGExecutor(
        {"gmail": upstream, "calendar": downstream},
        on_event=lambda step_id, status: events.append((step_id, status)),
    )
    steps = [
        PlanStep(id="s1", agent="gmail", action="search_emails"),
        PlanStep(id="s2", agent="calendar", action="search_events", depends_on=["s1"]),
    ]

    await executor.run(steps)

    assert ("s2", "running") in events
    assert ("s2", "skipped") in events


# --------------------------------------------------------------------------- #
# (11) Missing template path -> failed with ParamResolutionError message
# --------------------------------------------------------------------------- #


async def test_missing_template_path_fails_step() -> None:
    upstream = RecordingAgent(_two_rows())
    downstream = RecordingAgent()
    executor = DAGExecutor({"gmail": upstream, "calendar": downstream})
    steps = [
        PlanStep(id="s1", agent="gmail", action="search_emails"),
        PlanStep(
            id="s2",
            agent="calendar",
            action="create_event",
            depends_on=["s1"],
            params={"x": "{{s1.top.nonexistent}}"},
        ),
    ]

    results = await executor.run(steps)

    assert results["s2"].status == "failed"
    assert "top.nonexistent" in (results["s2"].error or "")
    assert downstream.calls == []  # step never executed with bad params


def test_resolve_params_raises_on_missing_step() -> None:
    with pytest.raises(ParamResolutionError) as excinfo:
        resolve_params({"x": "{{ghost.top.id}}"}, {})
    assert excinfo.value.step_id == "ghost"


def test_resolve_params_whole_value_vs_embedded() -> None:
    results = {
        "s1": StepResult(
            step_id="s1",
            status="ok",
            data={"results": [{"id": "e1", "score": 7, "subject": "Hi"}]},
        )
    }
    resolved = resolve_params(
        {"raw": "{{s1.top.score}}", "embedded": "n={{s1.top.score}}"}, results
    )
    # Whole-value keeps the int type; embedded coerces to string.
    assert resolved["raw"] == 7
    assert isinstance(resolved["raw"], int)
    assert resolved["embedded"] == "n=7"


# --------------------------------------------------------------------------- #
# (12) latency_ms populated
# --------------------------------------------------------------------------- #


async def test_latency_ms_populated() -> None:
    executor = DAGExecutor({"gmail": SlowAgent(delay=0.01)})
    steps = [PlanStep(id="s1", agent="gmail", action="search_emails")]

    results = await executor.run(steps)

    assert results["s1"].status == "ok"
    assert isinstance(results["s1"].latency_ms, int)
    assert results["s1"].latency_ms > 0


# --------------------------------------------------------------------------- #
# Misc: ExecutionPlan defaults
# --------------------------------------------------------------------------- #


def test_execution_plan_defaults() -> None:
    plan = ExecutionPlan()
    assert plan.steps == []
    assert plan.pending_action_template is None
    # Independent instances do not share the mutable default.
    other = ExecutionPlan()
    plan.steps.append(PlanStep(id="s1", agent="gmail", action="search_emails"))
    assert other.steps == []
