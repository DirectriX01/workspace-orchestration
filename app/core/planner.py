"""Query planner: turn a classified :class:`IntentResult` into an executable DAG.

For deterministic intents (flight cancellation, meeting prep, single-service
searches, and confirm-required mutations) the planner emits a hand-written
:class:`~app.core.dag.ExecutionPlan` from :data:`PLAN_TEMPLATES`. For the
open-ended ``complex_multi_service`` intent it asks the LLM to emit an
:class:`LLMPlanOutput`, validates every step against the canonical agent action
surface, and falls back to a safe read-only plan when validation fails.

Template placeholders use the ``{{step_id.path}}`` syntax resolved later by
:func:`app.core.dag.resolve_params`; this module only *emits* those strings, it
never resolves them.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.dag import ExecutionPlan, PlanStep

if TYPE_CHECKING:  # avoid hard import coupling while sibling modules are built
    from app.core.intent import IntentResult
    from app.core.temporal import TimeRange


# --------------------------------------------------------------------------- #
# LLM plan schema (strict-structured-output friendly)                          #
# --------------------------------------------------------------------------- #
class LLMPlanStep(BaseModel):
    """One LLM-proposed step. ``params_json`` is a JSON string for strict mode."""

    id: str
    agent: str  # Literal["gmail", "calendar", "drive"] — validated in planner
    action: str
    params_json: str
    depends_on: list[str] = Field(default_factory=list)


class LLMPlanOutput(BaseModel):
    """Envelope for an LLM-generated multi-service plan."""

    steps: list[LLMPlanStep] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Canonical agent action surface                                               #
# --------------------------------------------------------------------------- #
KNOWN_ACTIONS: dict[str, set[str]] = {
    "gmail": {
        "search_emails",
        "get_email",
        "draft_email",
        "send_email",
        "update_labels",
    },
    "calendar": {
        "search_events",
        "get_event",
        "create_event",
        "update_event",
        "delete_event",
    },
    "drive": {
        "search_files",
        "get_file",
        "share_file",
        "create_folder",
        "move_file",
    },
}

_CANONICAL_ACTIONS_DOC = """\
gmail.search_emails {query: str, from_email?: str, label?: str, after?: iso, before?: iso, k?: int}
gmail.get_email {email_id}
gmail.draft_email {to: list[str], subject, body}
gmail.send_email {to: list[str], subject, body}
gmail.update_labels {email_id, add: list[str], remove: list[str]}
calendar.search_events {query?: str, attendee?: str, starts_after?: iso, starts_before?: iso, k?: int}
calendar.get_event {event_id}
calendar.create_event {title, start: iso, end: iso, attendees?: list[str], description?, location?}
calendar.update_event {event_id, changes: dict}
calendar.delete_event {event_id}
drive.search_files {query: str, mime_type?: str, owner?: str, modified_after?: iso, modified_before?: iso, k?: int}
drive.get_file {file_id}
drive.share_file {file_id, email, role?}
drive.create_folder {name, parent_id?}
drive.move_file {file_id, folder_id}"""


# --------------------------------------------------------------------------- #
# Small helpers                                                                #
# --------------------------------------------------------------------------- #
def _first(*values: Any) -> Any:
    """Return the first truthy value, else ``None``."""
    for value in values:
        if value:
            return value
    return None


def _range_bounds(time_range: "TimeRange | None") -> tuple[str | None, str | None]:
    """Return ISO ``(start, end)`` for a :class:`TimeRange`, ``None`` where absent."""
    if time_range is None:
        return None, None
    start = time_range.start.isoformat() if time_range.start else None
    end = time_range.end.isoformat() if time_range.end else None
    return start, end


def _mime_for_hint(file_hint: str | None) -> str | None:
    """Map a free-text file hint to a Drive mime type, or ``None``."""
    if not file_hint:
        return None
    hint = file_hint.lower()
    if "pdf" in hint:
        return "application/pdf"
    if "spreadsheet" in hint or "xlsx" in hint:
        return "application/vnd.google-apps.spreadsheet"
    if "doc" in hint:
        return "application/vnd.google-apps.document"
    return None


def _first_email(intent: "IntentResult") -> str | None:
    """First extracted person email, if any."""
    emails = intent.entities.person_emails
    return emails[0] if emails else None


# --------------------------------------------------------------------------- #
# Deterministic template builders                                              #
# --------------------------------------------------------------------------- #
def _build_flight_cancellation(
    intent: "IntentResult", time_range: "TimeRange | None", now: datetime
) -> ExecutionPlan:
    airline = intent.entities.airline or "flight"
    booking_query = f"{airline} flight booking confirmation"
    broad_query = intent.entities.airline or "flight booking"

    find_booking = PlanStep(
        id="find_booking",
        agent="gmail",
        action="search_emails",
        params={"query": booking_query, "k": 5},
        fallback=PlanStep(
            id="find_booking_broad",
            agent="gmail",
            action="search_emails",
            params={"query": broad_query, "k": 5},
        ),
    )
    find_flight_event = PlanStep(
        id="find_flight_event",
        agent="calendar",
        action="search_events",
        params={
            "query": "{{find_booking.top.subject}}",
            "starts_after": now.isoformat(),
        },
        depends_on=["find_booking"],
        optional=True,
    )
    subject_ref = "{{find_booking.top.subject}}"
    draft_cancellation = PlanStep(
        id="draft_cancellation",
        agent="gmail",
        action="draft_email",
        params={
            "to": ["{{find_booking.top.from_email}}"],
            "subject": f"Cancellation request - {subject_ref}",
            "body": (
                "Hello,\n\n"
                f'I would like to cancel my reservation associated with "{subject_ref}". '
                "Please cancel this booking and let me know about any refund or next steps.\n\n"
                "Thank you."
            ),
        },
        depends_on=["find_booking"],
    )
    return ExecutionPlan(
        steps=[find_booking, find_flight_event, draft_cancellation],
        pending_action_template={
            "description": "send the drafted cancellation email",
            "agent": "gmail",
            "action": "send_email",
            "params_from_step": "draft_cancellation",
        },
    )


def _build_meeting_prep(
    intent: "IntentResult", time_range: "TimeRange | None", now: datetime
) -> ExecutionPlan:
    topic = _first(intent.entities.company, intent.entities.topic, intent.entities.event_title) or ""

    start, end = _range_bounds(time_range)
    if start is None and end is None:
        # No explicit date ("prepare me for the roadmap meeting"): look at
        # the coming week, not just tomorrow — the target meeting is simply
        # the next upcoming match.
        window_end = now.replace(hour=23, minute=59, second=59, microsecond=999999) + timedelta(days=7)
        start, end = now.isoformat(), window_end.isoformat()

    meeting_params: dict[str, Any] = {"query": topic}
    if start:
        meeting_params["starts_after"] = start
    if end:
        meeting_params["starts_before"] = end

    find_meeting = PlanStep(
        id="find_meeting",
        agent="calendar",
        action="search_events",
        params=meeting_params,
        optional=True,
    )
    find_emails = PlanStep(
        id="find_emails",
        agent="gmail",
        action="search_emails",
        params={"query": topic, "k": 5},
        optional=True,
    )
    find_files = PlanStep(
        id="find_files",
        agent="drive",
        action="search_files",
        params={"query": topic, "k": 5},
        optional=True,
    )
    attendee_emails = PlanStep(
        id="attendee_emails",
        agent="gmail",
        action="search_emails",
        params={"query": "{{find_meeting.top.attendees}}", "k": 5},
        depends_on=["find_meeting"],
        optional=True,
    )
    return ExecutionPlan(steps=[find_meeting, find_emails, find_files, attendee_emails])


def _build_calendar_search(
    intent: "IntentResult", time_range: "TimeRange | None", now: datetime
) -> ExecutionPlan:
    params: dict[str, Any] = {}
    query = _first(intent.entities.topic, intent.entities.event_title)
    if query:
        params["query"] = query
    attendee = _first_email(intent)
    if attendee:
        params["attendee"] = attendee

    start, end = _range_bounds(time_range)
    if start:
        params["starts_after"] = start
    elif time_range is None:
        params["starts_after"] = now.isoformat()
    if end:
        params["starts_before"] = end

    return ExecutionPlan(
        steps=[
            PlanStep(
                id="search_events",
                agent="calendar",
                action="search_events",
                params=params,
            )
        ]
    )


def _build_email_search(
    intent: "IntentResult", time_range: "TimeRange | None", now: datetime
) -> ExecutionPlan:
    params: dict[str, Any] = {
        "query": _first(intent.entities.topic, intent.entities.event_title) or ""
    }
    from_email = _first_email(intent)
    if from_email:
        params["from_email"] = from_email
    start, end = _range_bounds(time_range)
    if start:
        params["after"] = start
    if end:
        params["before"] = end
    if intent.entities.label:
        params["label"] = intent.entities.label

    return ExecutionPlan(
        steps=[
            PlanStep(
                id="search_emails",
                agent="gmail",
                action="search_emails",
                params=params,
            )
        ]
    )


def _build_drive_search(
    intent: "IntentResult", time_range: "TimeRange | None", now: datetime
) -> ExecutionPlan:
    params: dict[str, Any] = {
        "query": _first(intent.entities.topic, intent.entities.file_hint) or ""
    }
    mime_type = _mime_for_hint(intent.entities.file_hint)
    if mime_type:
        params["mime_type"] = mime_type
    start, end = _range_bounds(time_range)
    if start:
        params["modified_after"] = start
    if end:
        params["modified_before"] = end

    return ExecutionPlan(
        steps=[
            PlanStep(
                id="search_files",
                agent="drive",
                action="search_files",
                params=params,
            )
        ]
    )


def _build_calendar_action(
    intent: "IntentResult", time_range: "TimeRange | None", now: datetime
) -> ExecutionPlan:
    topic = (intent.entities.topic or "").lower()
    is_delete = "delete" in topic or "cancel" in topic

    find_params: dict[str, Any] = {}
    query = _first(intent.entities.event_title, intent.entities.topic)
    if query:
        find_params["query"] = query
    attendee = _first_email(intent)
    if attendee:
        find_params["attendee"] = attendee

    find_target = PlanStep(
        id="find_target",
        agent="calendar",
        action="search_events",
        params=find_params,
        expect_single=True,
    )

    if is_delete:
        mutate = PlanStep(
            id="delete_target",
            agent="calendar",
            action="delete_event",
            params={"event_id": "{{find_target.top.id}}"},
            depends_on=["find_target"],
            requires_confirmation=True,
        )
    else:
        changes: dict[str, Any] = {}
        start, end = _range_bounds(time_range)
        if start:
            changes["start"] = start
        if end:
            changes["end"] = end
        mutate = PlanStep(
            id="update_target",
            agent="calendar",
            action="update_event",
            params={"event_id": "{{find_target.top.id}}", "changes": changes},
            depends_on=["find_target"],
            requires_confirmation=True,
        )

    return ExecutionPlan(steps=[find_target, mutate])


def _build_email_action(
    intent: "IntentResult", time_range: "TimeRange | None", now: datetime
) -> ExecutionPlan:
    find_params: dict[str, Any] = {
        "query": _first(intent.entities.topic, intent.entities.event_title) or ""
    }
    from_email = _first_email(intent)
    if from_email:
        find_params["from_email"] = from_email

    find_target = PlanStep(
        id="find_target",
        agent="gmail",
        action="search_emails",
        params=find_params,
        expect_single=True,
    )

    if intent.entities.label:
        mutate = PlanStep(
            id="apply_labels",
            agent="gmail",
            action="update_labels",
            params={
                "email_id": "{{find_target.top.id}}",
                "add": [intent.entities.label],
                "remove": [],
            },
            depends_on=["find_target"],
            requires_confirmation=True,
        )
    else:
        recipients = [from_email] if from_email else []
        mutate = PlanStep(
            id="draft_reply",
            agent="gmail",
            action="draft_email",
            params={
                "to": recipients,
                "subject": "Re: {{find_target.top.subject}}",
                "body": "Following up on {{find_target.top.subject}}.",
            },
            depends_on=["find_target"],
            requires_confirmation=True,
        )

    return ExecutionPlan(steps=[find_target, mutate])


def _build_drive_action(
    intent: "IntentResult", time_range: "TimeRange | None", now: datetime
) -> ExecutionPlan:
    find_params: dict[str, Any] = {
        "query": _first(intent.entities.file_hint, intent.entities.topic) or ""
    }
    mime_type = _mime_for_hint(intent.entities.file_hint)
    if mime_type:
        find_params["mime_type"] = mime_type

    find_target = PlanStep(
        id="find_target",
        agent="drive",
        action="search_files",
        params=find_params,
        expect_single=True,
    )
    share = PlanStep(
        id="share_file",
        agent="drive",
        action="share_file",
        params={
            "file_id": "{{find_target.top.id}}",
            "email": _first_email(intent),
        },
        depends_on=["find_target"],
        requires_confirmation=True,
    )
    return ExecutionPlan(steps=[find_target, share])


def _build_empty(
    intent: "IntentResult", time_range: "TimeRange | None", now: datetime
) -> ExecutionPlan:
    return ExecutionPlan(steps=[])


#: intent name -> deterministic plan builder. ``complex_multi_service`` is not
#: here because it is async and LLM-backed (handled by :meth:`QueryPlanner.plan`).
PLAN_TEMPLATES: dict[
    str, Callable[["IntentResult", "TimeRange | None", datetime], ExecutionPlan]
] = {
    "flight_cancellation": _build_flight_cancellation,
    "meeting_prep": _build_meeting_prep,
    "calendar_search": _build_calendar_search,
    "email_search": _build_email_search,
    "drive_search": _build_drive_search,
    "calendar_action": _build_calendar_action,
    "email_action": _build_email_action,
    "drive_action": _build_drive_action,
    "chitchat": _build_empty,
    "confirm_action": _build_empty,
    "clarification_reply": _build_empty,
}


# --------------------------------------------------------------------------- #
# Planner                                                                      #
# --------------------------------------------------------------------------- #
class QueryPlanner:
    """Build an :class:`ExecutionPlan` from a classified intent."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm
        self._settings = get_settings()

    async def plan(
        self,
        intent: "IntentResult",
        time_range: "TimeRange | None",
        user: Any,
    ) -> ExecutionPlan:
        """Return the execution plan for ``intent`` in the user's timezone."""
        tz = getattr(user, "timezone", None) or self._settings.default_tz
        now = datetime.now(ZoneInfo(tz))

        if intent.intent == "complex_multi_service":
            return await self._plan_complex(intent, time_range, now)

        builder = PLAN_TEMPLATES.get(intent.intent)
        if builder is None:
            return ExecutionPlan(steps=[])
        return builder(intent, time_range, now)

    # -- complex, LLM-backed planning -------------------------------------- #
    async def _plan_complex(
        self, intent: "IntentResult", time_range: "TimeRange | None", now: datetime
    ) -> ExecutionPlan:
        system = (
            "You are a planning engine for a workspace assistant. Decompose the "
            "user's multi-service request into a DAG of tool calls. Only use the "
            "canonical agent actions below, each with exactly its documented "
            "params. Emit params as a JSON string in `params_json`. Reference an "
            "upstream step's result with the template syntax {{step_id.path}} "
            "(use {{step_id.top.field}} for the first result). depends_on lists "
            "the ids of steps that must run first.\n\n"
            "Search results expose these fields per row: `id` (ALWAYS use `id` "
            "for get_/update_/delete_ params, never event_id/file_id/email_id), "
            "plus subject/title/name, from_email/attendees/owner_email, dates "
            "as ISO strings, and score. Search steps already return previews "
            "(body_preview/description/content_preview), so add a get_ step "
            "only when full content is essential.\n\n"
            f"Canonical actions:\n{_CANONICAL_ACTIONS_DOC}"
        )
        entities = intent.entities.model_dump()
        start, end = _range_bounds(time_range)
        user_msg = json.dumps(
            {
                "entities": entities,
                "temporal_phrase": intent.temporal_phrase,
                "time_range": {"start": start, "end": end},
                "now": now.isoformat(),
            }
        )

        try:
            output = await self._llm.complete_structured(system, user_msg, LLMPlanOutput)
            steps = self._validate_llm_plan(output)
        except Exception:
            steps = None

        if steps is None:
            return self._safe_plan(intent)
        return ExecutionPlan(steps=steps)

    @staticmethod
    def _validate_llm_plan(output: LLMPlanOutput) -> list[PlanStep] | None:
        """Validate and convert an LLM plan; ``None`` if anything is invalid."""
        if not output.steps:
            return None
        ids = {step.id for step in output.steps}
        converted: list[PlanStep] = []
        for step in output.steps:
            if step.agent not in KNOWN_ACTIONS:
                return None
            if step.action not in KNOWN_ACTIONS[step.agent]:
                return None
            if any(dep not in ids for dep in step.depends_on):
                return None
            try:
                params = json.loads(step.params_json)
            except (json.JSONDecodeError, TypeError):
                return None
            if not isinstance(params, dict):
                return None
            # LLM-proposed writes must never execute unconfirmed; only
            # searches, reads, and drafts are allowed to run directly.
            is_mutation = (
                not step.action.startswith(("search_", "get_"))
                and step.action != "draft_email"
            )
            converted.append(
                PlanStep(
                    id=step.id,
                    agent=step.agent,
                    action=step.action,
                    params=params,
                    depends_on=list(step.depends_on),
                    requires_confirmation=is_mutation,
                )
            )
        return converted

    @staticmethod
    def _safe_plan(intent: "IntentResult") -> ExecutionPlan:
        """Read-only three-service fan-out used when LLM planning is unusable."""
        topic = _first(intent.entities.topic, intent.entities.company, intent.entities.event_title) or ""
        return ExecutionPlan(
            steps=[
                PlanStep(
                    id="search_emails",
                    agent="gmail",
                    action="search_emails",
                    params={"query": topic},
                    optional=True,
                ),
                PlanStep(
                    id="search_events",
                    agent="calendar",
                    action="search_events",
                    params={"query": topic},
                    optional=True,
                ),
                PlanStep(
                    id="search_files",
                    agent="drive",
                    action="search_files",
                    params={"query": topic},
                    optional=True,
                ),
            ]
        )
