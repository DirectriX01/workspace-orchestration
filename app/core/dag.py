"""Plan representation and wave-based DAG execution.

This module defines the data structures that a :class:`~app.core.planner`
produces (:class:`PlanStep`, :class:`StepResult`, :class:`ExecutionPlan`) and
the engine that runs them (:class:`DAGExecutor`).

A plan is a list of :class:`PlanStep` nodes wired together via
``depends_on``. The executor runs them in *waves*: every wave it selects the
steps whose dependencies have all settled, executes the runnable ones
concurrently, and repeats until nothing is pending. Templated parameters of
the form ``{{step_id.path}}`` are resolved against upstream results just
before a step runs (see :func:`resolve_params`).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ParamResolutionError(Exception):
    """Raised when a ``{{step_id.path}}`` template cannot be resolved."""

    def __init__(self, step_id: str, path: str) -> None:
        self.step_id = step_id
        self.path = path
        super().__init__(
            f"could not resolve template path '{path}' from step '{step_id}'"
        )


class PlanError(Exception):
    """Raised when a plan cannot make progress (cycle / unsatisfiable deps)."""


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #


@dataclass
class PlanStep:
    """A single node in an execution plan.

    Attributes:
        id: Unique identifier used by dependents and template references.
        agent: Which agent runs this step (``"gmail"``/``"calendar"``/``"drive"``).
        action: The agent action to invoke (e.g. ``"search_emails"``).
        params: Action parameters, possibly containing ``{{...}}`` templates.
        depends_on: Ids of steps that must settle before this one runs.
        fallback: A full :class:`PlanStep` executed inline when this step
            comes back ``empty`` or ``failed``; its result replaces this one
            when it is *better*.
        optional: When ``True`` a failure/empty of this step does not block
            steps that depend on it.
        requires_confirmation: When ``True`` the step is never executed; it
            settles as ``pending_confirmation`` carrying its resolved params.
        expect_single: When ``True`` an ``ok`` result with more than one row
            is downgraded to ``ambiguous``.
    """

    id: str
    agent: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    fallback: PlanStep | None = None
    optional: bool = False
    requires_confirmation: bool = False
    expect_single: bool = False


@dataclass
class StepResult:
    """The settled outcome of a single :class:`PlanStep`."""

    step_id: str
    status: str  # ok|empty|failed|skipped|ambiguous|conflict|pending_confirmation
    data: Any = None
    error: str | None = None
    latency_ms: int = 0


@dataclass
class ExecutionPlan:
    """An ordered set of steps plus an optional deferred-action template."""

    steps: list[PlanStep] = field(default_factory=list)
    #: ``{"description", "agent", "action", "params_from_step"}`` — the
    #: pipeline resolves the concrete params from that step's result after
    #: execution and offers the action for confirmation.
    pending_action_template: dict | None = None


# --------------------------------------------------------------------------- #
# Template parameter resolution
# --------------------------------------------------------------------------- #

#: Matches a ``{{ref}}`` template; the captured group is the reference body.
_TEMPLATE_RE = re.compile(r"\{\{([\w\.\[\]0-9]+)\}\}")

#: Extracts integer indices from a segment such as ``results[1]`` -> ``[1]``.
_INDEX_RE = re.compile(r"\[(\d+)\]")


def _resolve_ref(ref: str, results: dict[str, StepResult]) -> Any:
    """Resolve a single template reference (the text between ``{{`` and ``}}``).

    The first dotted segment is the step id; the remainder walks
    :attr:`StepResult.data`. The segment ``top`` aliases ``results[0]`` of the
    step's ``data["results"]`` list. ``[n]`` suffixes index into lists.
    """
    segments = ref.split(".")
    step_id = segments[0]
    path = ".".join(segments[1:])
    if step_id not in results:
        raise ParamResolutionError(step_id, path or ref)

    current: Any = results[step_id].data
    for segment in segments[1:]:
        name = segment.split("[", 1)[0]
        indices = [int(i) for i in _INDEX_RE.findall(segment)]
        try:
            if name == "top":
                current = current["results"][0]
            elif name.isdigit():
                # A bare numeric segment (e.g. "step1.0.id", a shape LLM
                # planners plausibly emit) indexes the step's results list
                # when walking the top-level data dict, else the current list.
                container = (
                    current["results"]
                    if isinstance(current, dict) and "results" in current
                    else current
                )
                current = container[int(name)]
            elif name:
                current = current[name]
            for index in indices:
                current = current[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise ParamResolutionError(step_id, path or ref) from exc
    return current


def _resolve_string(value: str, results: dict[str, StepResult]) -> Any:
    """Resolve templates inside a string.

    A string that is *exactly* one template yields the referenced raw object
    (type preserved). A string with embedded templates has each match replaced
    by ``str(...)`` of its resolved value.
    """
    matches = list(_TEMPLATE_RE.finditer(value))
    if not matches:
        return value
    if len(matches) == 1 and matches[0].group(0) == value:
        return _resolve_ref(matches[0].group(1), results)

    def _replace(match: re.Match[str]) -> str:
        return str(_resolve_ref(match.group(1), results))

    return _TEMPLATE_RE.sub(_replace, value)


def _resolve_value(value: Any, results: dict[str, StepResult]) -> Any:
    """Recursively resolve templates in strings nested in lists / dicts."""
    if isinstance(value, str):
        return _resolve_string(value, results)
    if isinstance(value, list):
        return [_resolve_value(item, results) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_value(item, results) for key, item in value.items()}
    return value


def resolve_params(
    params: dict[str, Any], results: dict[str, StepResult]
) -> dict[str, Any]:
    """Return a copy of ``params`` with every ``{{...}}`` template resolved.

    Raises:
        ParamResolutionError: if any referenced step id or path is missing.
    """
    return {key: _resolve_value(value, results) for key, value in params.items()}


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #

#: Dependency statuses that block a (non-optional) dependent from running.
_BLOCKING_STATUSES = frozenset(
    {"failed", "skipped", "ambiguous", "conflict", "pending_confirmation"}
)

#: Ranking used to decide whether a fallback result "replaces" the primary.
_STATUS_RANK = {
    "ok": 5,
    "ambiguous": 4,
    "conflict": 3,
    "empty": 2,
    "pending_confirmation": 1,
    "failed": 0,
    "skipped": 0,
}


class DAGExecutor:
    """Runs a list of :class:`PlanStep` objects wave by wave.

    Args:
        agents: Maps an agent name to an object exposing
            ``async run(action: str, params: dict) -> dict``.
        on_event: Optional callback invoked as ``on_event(step_id, status)``
            once with ``"running"`` when a step starts and once with its final
            status when it settles — for *every* step.
    """

    def __init__(
        self,
        agents: dict[str, Any],
        on_event: Callable[[str, str], None] | None = None,
    ) -> None:
        self.agents = agents
        self.on_event = on_event

    async def run(self, steps: list[PlanStep]) -> dict[str, StepResult]:
        """Execute ``steps`` and return the settled result of each by id."""
        step_by_id = {step.id: step for step in steps}
        results: dict[str, StepResult] = {}

        while True:
            settled = set(results.keys())
            pending = [step for step in steps if step.id not in settled]
            if not pending:
                break

            ready = [
                step
                for step in pending
                if all(dep in settled for dep in step.depends_on)
            ]
            if not ready:
                raise PlanError("cycle or unsatisfiable dependencies")

            runnable: list[PlanStep] = []
            for step in ready:
                skip_reason = self._blocking_reason(step, step_by_id, results)
                if skip_reason is not None:
                    self._emit(step.id, "running")
                    results[step.id] = StepResult(
                        step.id, "skipped", error=skip_reason
                    )
                    self._emit(step.id, "skipped")
                else:
                    runnable.append(step)

            if not runnable:
                # All ready steps were skipped this wave; progress was made so
                # the loop re-evaluates readiness on the next iteration.
                continue

            for step in runnable:
                self._emit(step.id, "running")
            settled_results = await asyncio.gather(
                *(self._run_step(step, results) for step in runnable),
                return_exceptions=True,
            )
            for step, outcome in zip(runnable, settled_results):
                if isinstance(outcome, BaseException):
                    outcome = StepResult(step.id, "failed", error=repr(outcome))
                results[step.id] = outcome
                self._emit(step.id, outcome.status)

        return results

    # -- internals ---------------------------------------------------------- #

    def _blocking_reason(
        self,
        step: PlanStep,
        step_by_id: dict[str, PlanStep],
        results: dict[str, StepResult],
    ) -> str | None:
        """Return ``"upstream <id> <status>"`` if a non-optional dep blocks."""
        for dep in step.depends_on:
            dep_result = results[dep]
            dep_step = step_by_id.get(dep)
            dep_optional = dep_step.optional if dep_step is not None else False
            if dep_result.status in _BLOCKING_STATUSES and not dep_optional:
                return f"upstream {dep} {dep_result.status}"
        return None

    async def _run_step(
        self, step: PlanStep, results: dict[str, StepResult]
    ) -> StepResult:
        """Execute one step, measuring wall-clock latency in milliseconds."""
        start = time.perf_counter()
        result = await self._execute_core(step, results)
        result.latency_ms = int((time.perf_counter() - start) * 1000)
        return result

    async def _execute_core(
        self, step: PlanStep, results: dict[str, StepResult]
    ) -> StepResult:
        # Confirmation-gated steps are never executed; carry resolved params.
        if step.requires_confirmation:
            try:
                resolved = resolve_params(step.params, results)
            except ParamResolutionError as exc:
                return StepResult(step.id, "failed", error=str(exc))
            return StepResult(
                step.id,
                "pending_confirmation",
                data={
                    "agent": step.agent,
                    "action": step.action,
                    "params": resolved,
                },
            )

        try:
            resolved = resolve_params(step.params, results)
        except ParamResolutionError as exc:
            return StepResult(step.id, "failed", error=str(exc))

        primary = await self._call_agent(
            step.agent, step.action, resolved, step.id, step.expect_single
        )

        if primary.status in ("empty", "failed") and step.fallback is not None:
            fallback = await self._run_fallback(step.fallback, results)
            if _STATUS_RANK.get(fallback.status, 0) > _STATUS_RANK.get(
                primary.status, 0
            ):
                # Keep the original step id so dependents resolve correctly.
                return StepResult(
                    step.id,
                    fallback.status,
                    data=fallback.data,
                    error=fallback.error,
                )
        return primary

    async def _run_fallback(
        self, fallback: PlanStep, results: dict[str, StepResult]
    ) -> StepResult:
        try:
            resolved = resolve_params(fallback.params, results)
        except ParamResolutionError as exc:
            return StepResult(fallback.id, "failed", error=str(exc))
        return await self._call_agent(
            fallback.agent,
            fallback.action,
            resolved,
            fallback.id,
            fallback.expect_single,
        )

    async def _call_agent(
        self,
        agent_name: str,
        action: str,
        params: dict[str, Any],
        step_id: str,
        expect_single: bool,
    ) -> StepResult:
        try:
            raw = await self.agents[agent_name].run(action, params)
        except Exception as exc:  # noqa: BLE001 - surfaced as a failed step
            return StepResult(step_id, "failed", error=repr(exc))
        return self._map_result(raw, step_id, expect_single)

    @staticmethod
    def _map_result(raw: Any, step_id: str, expect_single: bool) -> StepResult:
        """Map an agent's returned dict onto a :class:`StepResult`."""
        status = raw.get("status") if isinstance(raw, dict) else None
        if status == "empty":
            return StepResult(step_id, "empty", data=raw)
        if status == "ambiguous":
            return StepResult(step_id, "ambiguous", data=raw)
        if status == "conflict":
            return StepResult(step_id, "conflict", data=raw)

        result = StepResult(step_id, "ok", data=raw)
        if expect_single:
            rows = raw.get("results", []) if isinstance(raw, dict) else []
            if len(rows) > 1:
                result.status = "ambiguous"
        return result

    def _emit(self, step_id: str, status: str) -> None:
        if self.on_event is not None:
            self.on_event(step_id, status)
