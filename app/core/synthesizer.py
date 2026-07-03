"""Natural-language response synthesis.

:class:`ResponseSynthesizer` turns the structured output of a run (the classified
intent, the execution plan, the per-step results, and any deferred pending
action) into a short, grounded, natural-language answer via the injected LLM's
``complete_text``. It never invents data: the system prompt constrains the model
to the provided step digests and instructs it to ask for confirmation whenever a
pending action is present.

Three entry points cover the pipeline's needs:

* :meth:`synthesize` — the main answer for an executed plan.
* :meth:`chitchat`   — a brief capabilities blurb for non-workspace turns.
* :meth:`confirmation` — a short "done" summary after an approved action runs.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.dag import ExecutionPlan, StepResult
from app.core.intent import IntentResult
from app.llm.client import LLMClient

#: Number of result rows per step included in the digest handed to the LLM.
_MAX_DIGEST_ROWS = 5

_SYNTHESIS_SYSTEM = (
    "You are a helpful Google Workspace assistant operating over the user's "
    "Gmail, Calendar, and Drive. Ground your answer ONLY in the provided step "
    "results — never invent emails, events, files, dates, or people, and never "
    "claim an action was taken unless a result shows it. State any failed or "
    "skipped steps plainly rather than glossing over them. Cite concrete items "
    "by their subject / title / filename together with their date. When the "
    "user asks for a comparison, overlap, or conflict check, DO the analysis "
    "yourself from the returned data (e.g. compare document contents against "
    "event dates) and state the specific outcome — do not hand the comparison "
    "back to the user. Keep the answer under 150 words. If a pending action "
    "is present, briefly summarize what it will do and explicitly ask the "
    "user to confirm before it is executed."
)

_CHITCHAT_SYSTEM = (
    "You are a friendly Google Workspace assistant. The user's message is small "
    "talk rather than a workspace task. Reply in one or two sentences and briefly "
    "mention that you can search and act across their Gmail, Calendar, and Drive "
    "— for example finding emails, checking their schedule, locating files, "
    "preparing for meetings, or drafting replies. Do not fabricate any data."
)

_CONFIRMATION_SYSTEM = (
    "You are a helpful Google Workspace assistant confirming that a previously "
    "approved action has now been executed. Reply with a short (one to two "
    "sentence) confirmation summarizing what was done, grounded strictly in the "
    "provided execution result. Do not invent any details."
)


class ResponseSynthesizer:
    """Render structured run output into a grounded natural-language answer."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def synthesize(
        self,
        query: str,
        intent: IntentResult,
        plan: ExecutionPlan,
        results: dict[str, StepResult],
        pending_action: dict | None,
    ) -> str:
        """Produce the user-facing answer for an executed plan."""
        digest = self._build_digest(plan, results)

        lines: list[str] = [
            f"User query: {query}",
            f"Detected intent: {intent.intent}",
        ]
        if pending_action:
            description = pending_action.get("description") or "the proposed action"
            lines.append(
                "IMPORTANT: A pending action is awaiting the user's confirmation: "
                f"{description}. Summarize it and explicitly ask the user to confirm "
                "before it is executed."
            )
        lines.append("Step results (JSON):")
        lines.append(json.dumps(digest, default=str))
        if pending_action:
            lines.append("Pending action (JSON):")
            lines.append(json.dumps(pending_action, default=str))

        user = "\n".join(lines)
        return await self._llm.complete_text(_SYNTHESIS_SYSTEM, user)

    async def chitchat(self, query: str) -> str:
        """Return a brief capabilities blurb for a non-workspace turn."""
        user = (
            f"The user said: {query}\n"
            "Respond warmly and briefly describe what you can help them with."
        )
        return await self._llm.complete_text(_CHITCHAT_SYSTEM, user)

    async def confirmation(self, pending: dict, exec_result: Any) -> str:
        """Return a short confirmation that ``pending`` was executed."""
        lines = [
            "The approved action has now been executed.",
            f"Action: {pending.get('action')} via {pending.get('agent')}",
            f"Action description: {pending.get('description')}",
            f"Execution result (JSON): {json.dumps(exec_result, default=str)}",
        ]
        user = "\n".join(lines)
        return await self._llm.complete_text(_CONFIRMATION_SYSTEM, user)

    # ------------------------------------------------------------------ #
    # Digest construction                                                #
    # ------------------------------------------------------------------ #
    def _build_digest(
        self, plan: ExecutionPlan, results: dict[str, StepResult]
    ) -> list[dict]:
        """Build a compact, per-step digest of statuses and top result rows."""
        digest: list[dict] = []
        for step in plan.steps:
            result = results.get(step.id)
            entry: dict[str, Any] = {
                "step": step.id,
                "agent": step.agent,
                "action": step.action,
                "status": result.status if result is not None else "not_run",
                "error": result.error if result is not None else None,
                "results": [],
            }
            if result is not None and isinstance(result.data, dict):
                rows = result.data.get("results")
                if isinstance(rows, list):
                    entry["results"] = [
                        self._digest_row(row) for row in rows[:_MAX_DIGEST_ROWS]
                    ]
            digest.append(entry)
        return digest

    @staticmethod
    def _digest_row(row: Any) -> dict:
        """Reduce a result row to id / label / who / date / score."""
        if not isinstance(row, dict):
            return {"value": row}
        label = row.get("subject") or row.get("title") or row.get("name")
        who = (
            row.get("from_email")
            or row.get("attendees")
            or row.get("owner_email")
            or row.get("organizer_email")
        )
        date = row.get("received_at") or row.get("start") or row.get("modified_at")
        # Include a capped slice of the item's text so the LLM can actually
        # reason over content (e.g. conflict checks against a document),
        # not just metadata.
        preview = (
            row.get("body_preview")
            or row.get("content_preview")
            or row.get("content")
            or row.get("body")
            or row.get("description")
        )
        return {
            "id": row.get("id"),
            "label": label,
            "who": who,
            "date": date,
            "score": row.get("score"),
            "preview": str(preview)[:300] if preview else None,
        }
