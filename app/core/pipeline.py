"""End-to-end query pipeline.

:class:`QueryPipeline` is the orchestration seam that stitches together every
core component for a single user turn:

    classify -> (confirm | clarify) -> plan -> execute (DAG) -> synthesize

It owns no domain logic of its own; it wires the intent classifier, planner,
DAG executor, response synthesizer, and the Redis-backed conversation store into
one coherent flow, publishes step-progress events to a per-conversation pub/sub
channel, persists each turn (both to Postgres for the audit trail and to Redis
for short-term context), and returns a fully JSON-serializable response.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import build_agents
from app.config import get_settings
from app.core.context import ConversationStore
from app.core.dag import DAGExecutor, ExecutionPlan, StepResult
from app.core.intent import IntentClassifier, IntentResult
from app.core.planner import QueryPlanner
from app.core.synthesizer import ResponseSynthesizer
from app.core.temporal import resolve_temporal
from app.db.models import Conversation, User
from app.llm.client import get_llm_client


class QueryPipeline:
    """Orchestrate a single user query from raw text to a grounded answer."""

    def __init__(self, user: User, session: AsyncSession, redis: Any) -> None:
        self._user = user
        self._session = session
        self._redis = redis
        self._settings = get_settings()

        llm = get_llm_client()
        self._classifier = IntentClassifier(llm)
        self._planner = QueryPlanner(llm)
        self._synthesizer = ResponseSynthesizer(llm)
        # Scope conversation context (turns + pending action) to the
        # authenticated user so a caller cannot read another user's history or
        # fire another user's pending action by supplying its conversation id.
        self._store = ConversationStore(redis, scope=str(user.id))
        # Retain references to in-flight event-publish tasks so the event loop
        # does not garbage-collect them before completion (see below).
        self._event_tasks: set[asyncio.Task[Any]] = set()

    async def handle(self, query: str, conversation_id: str | None) -> dict:
        """Run the full pipeline for ``query`` and return a response dict."""
        cid = conversation_id or str(uuid4())
        turns = await self._store.get_turns(cid)
        tz = self._user.timezone or self._settings.default_tz
        now = datetime.now(ZoneInfo(tz))

        intent = await self._classifier.classify(query, turns, tz, now)

        if intent.intent == "confirm_action":
            return await self._handle_confirmation(cid, query, intent)

        if intent.needs_clarification:
            return await self._handle_clarification(cid, query, intent)

        time_range = (
            resolve_temporal(intent.temporal_phrase, now, tz)
            if intent.temporal_phrase
            else None
        )
        plan = await self._planner.plan(intent, time_range, self._user)

        if not plan.steps:
            return await self._handle_chitchat(cid, query, intent)

        return await self._handle_execution(cid, query, intent, plan)

    # ------------------------------------------------------------------ #
    # Branch handlers                                                    #
    # ------------------------------------------------------------------ #
    async def _handle_confirmation(
        self, cid: str, query: str, intent: IntentResult
    ) -> dict:
        """Execute a previously offered pending action, if one exists."""
        pending = await self._store.get_pending_action(cid)
        if pending is None:
            answer = "There is no pending action to confirm."
            await self._persist(query, intent, [], answer)
            await self._append_turn(cid, query, intent, {})
            return self._response(answer, cid, intent, [], {}, False, None)

        agents = await build_agents(self._user, self._session, self._redis)
        exec_result = await agents[pending["agent"]].run(
            pending["action"], pending["params"]
        )
        await self._store.set_pending_action(cid, None)

        answer = await self._synthesizer.confirmation(pending, exec_result)
        await self._persist(query, intent, [], answer)
        await self._append_turn(cid, query, intent, {})
        return self._response(
            answer, cid, intent, [], {"confirmation": exec_result}, False, None
        )

    async def _handle_clarification(
        self, cid: str, query: str, intent: IntentResult
    ) -> dict:
        """Return the classifier's clarifying question; execute no plan."""
        # This turn produces no pending action; drop any prior one so a later
        # "yes" cannot fire a stale action from an earlier, unrelated turn.
        await self._store.set_pending_action(cid, None)
        answer = intent.clarification_question or "Could you clarify your request?"
        await self._persist(query, intent, [], answer)
        await self._append_turn(cid, query, intent, {})
        return self._response(answer, cid, intent, [], {}, True, None)

    async def _handle_chitchat(
        self, cid: str, query: str, intent: IntentResult
    ) -> dict:
        """Answer an empty-plan (chitchat) turn with a capabilities blurb."""
        # No pending action arises from chitchat; clear any prior one so a
        # later confirmation cannot fire a stale action from an earlier turn.
        await self._store.set_pending_action(cid, None)
        answer = await self._synthesizer.chitchat(query)
        await self._persist(query, intent, [], answer)
        await self._append_turn(cid, query, intent, {})
        return self._response(answer, cid, intent, [], {}, False, None)

    async def _handle_execution(
        self, cid: str, query: str, intent: IntentResult, plan: ExecutionPlan
    ) -> dict:
        """Execute a non-empty plan, offer any pending action, and answer."""
        agents = await build_agents(self._user, self._session, self._redis)
        executor = DAGExecutor(agents, on_event=self._make_event_publisher(cid))
        results = await executor.run(plan.steps)

        # Always write the pending slot: store a freshly derived action, or
        # clear (None) so a prior turn's pending never survives into a later
        # confirmation on this conversation.
        pending = self._compute_pending(plan, results)
        await self._store.set_pending_action(cid, pending)

        answer = await self._synthesizer.synthesize(
            query, intent, plan, results, pending
        )

        plan_list = self._serialize_plan(plan, results)
        results_data = {step_id: result.data for step_id, result in results.items()}

        await self._persist(query, intent, plan_list, answer)
        await self._append_turn(
            cid, query, intent, self._resolved_entities(plan, results)
        )
        return self._response(
            answer, cid, intent, plan_list, results_data, False, pending
        )

    # ------------------------------------------------------------------ #
    # Pending-action derivation                                          #
    # ------------------------------------------------------------------ #
    def _compute_pending(
        self, plan: ExecutionPlan, results: dict[str, StepResult]
    ) -> dict | None:
        """Derive the single action to offer for confirmation, if any."""
        template = plan.pending_action_template
        if template is not None:
            src = results.get(template.get("params_from_step", ""))
            if src is not None and src.status == "ok" and isinstance(src.data, dict):
                data = src.data
                return {
                    "description": template["description"],
                    "agent": template["agent"],
                    "action": template["action"],
                    "params": {
                        "to": data.get("to"),
                        "subject": data.get("subject"),
                        "body": data.get("body"),
                    },
                }

        for step in plan.steps:
            result = results.get(step.id)
            if (
                result is not None
                and result.status == "pending_confirmation"
                and isinstance(result.data, dict)
            ):
                pending = dict(result.data)
                action = pending.get("action", step.action)
                agent = pending.get("agent", step.agent)
                pending["description"] = f"execute {action} via {agent}"
                return pending
        return None

    # ------------------------------------------------------------------ #
    # Serialization / persistence helpers                                #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _serialize_plan(
        plan: ExecutionPlan, results: dict[str, StepResult]
    ) -> list[dict]:
        """Serialize each plan step with its settled status/latency/error."""
        serialized: list[dict] = []
        for step in plan.steps:
            result = results.get(step.id)
            serialized.append(
                {
                    "id": step.id,
                    "agent": step.agent,
                    "action": step.action,
                    "params": step.params,
                    "depends_on": list(step.depends_on),
                    "optional": step.optional,
                    "requires_confirmation": step.requires_confirmation,
                    "status": result.status if result is not None else None,
                    "latency_ms": result.latency_ms if result is not None else None,
                    "error": result.error if result is not None else None,
                }
            )
        return serialized

    @staticmethod
    def _resolved_entities(
        plan: ExecutionPlan, results: dict[str, StepResult]
    ) -> dict[str, Any]:
        """Map each successful search step to its top result's id for context."""
        step_by_id = {step.id: step for step in plan.steps}
        resolved: dict[str, Any] = {}
        for step_id, result in results.items():
            step = step_by_id.get(step_id)
            if step is None or not step.action.startswith("search_"):
                continue
            if result.status != "ok" or not isinstance(result.data, dict):
                continue
            rows = result.data.get("results")
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                top_id = rows[0].get("id")
                if top_id is not None:
                    resolved[step_id] = top_id
        return resolved

    async def _persist(
        self,
        query: str,
        intent: IntentResult,
        plan_list: list[dict],
        answer: str,
    ) -> None:
        """Persist the turn as a :class:`Conversation` audit row."""
        conversation = Conversation(
            user_id=self._user.id,
            query=query,
            intent=intent.model_dump(),
            plan=plan_list,
            response=answer,
        )
        self._session.add(conversation)
        await self._session.commit()

    async def _append_turn(
        self,
        cid: str,
        query: str,
        intent: IntentResult,
        resolved_entities: dict[str, Any],
    ) -> None:
        """Append this turn to the short-term Redis conversation context."""
        await self._store.append_turn(
            cid,
            {
                "query": query,
                "intent": intent.intent,
                "resolved_entities": resolved_entities,
            },
        )

    # ------------------------------------------------------------------ #
    # Event publishing / response assembly                               #
    # ------------------------------------------------------------------ #
    def _make_event_publisher(self, cid: str) -> Callable[[str, str], None]:
        """Return a sync callback that fans step updates onto Redis pub/sub."""
        channel = f"conv:{cid}:events"

        async def _emit(message: str) -> None:
            # Event delivery is best-effort progress signalling; a Redis hiccup
            # must never surface as an unretrieved-task error or break the turn.
            try:
                await self._redis.publish(channel, message)
            except Exception:  # noqa: BLE001 - best-effort, non-critical
                pass

        def publish(step_id: str, status: str) -> None:
            message = json.dumps(
                {"type": "step_update", "step": step_id, "status": status}
            )
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            # Retain a strong reference until completion; asyncio only keeps a
            # weak one, so an unreferenced task can be GC'd mid-publish.
            task = loop.create_task(_emit(message))
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)

        return publish

    @staticmethod
    def _response(
        answer: str,
        cid: str,
        intent: IntentResult,
        plan_list: list[dict],
        results_data: dict[str, Any],
        needs_clarification: bool,
        pending_action: dict | None,
    ) -> dict:
        """Assemble the JSON-serializable response envelope."""
        return {
            "answer": answer,
            "conversation_id": cid,
            "intent": intent.model_dump(),
            "plan": plan_list,
            "results": results_data,
            "needs_clarification": needs_clarification,
            "pending_action": pending_action,
        }
