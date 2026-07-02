"""Unit tests for :mod:`app.core.planner`.

These tests exercise the deterministic :data:`PLAN_TEMPLATES` builders and the
LLM-backed ``complex_multi_service`` path with a stub LLM. They require no
Postgres/Redis/OpenAI — ``IntentResult``/``Entities`` are constructed directly
and a rule-free stub stands in for the LLM.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.dag import ExecutionPlan, PlanStep
from app.core.intent import Entities, IntentResult
from app.core.planner import (
    PLAN_TEMPLATES,
    LLMPlanOutput,
    LLMPlanStep,
    QueryPlanner,
)
from app.core.temporal import TimeRange

TZ = "Asia/Kolkata"
_USER = types.SimpleNamespace(timezone=TZ)


def _intent(intent: str, **entity_kwargs) -> IntentResult:
    return IntentResult(intent=intent, entities=Entities(**entity_kwargs))


class _StubLLM:
    """Deterministic LLM stand-in returning a preset structured output."""

    def __init__(self, output: object) -> None:
        self._output = output
        self.calls: list[tuple[str, str, type]] = []

    async def complete_structured(self, system: str, user: str, response_model: type):
        self.calls.append((system, user, response_model))
        return self._output

    async def complete_text(self, system: str, user: str) -> str:  # pragma: no cover
        return ""


def _planner(llm: object | None = None) -> QueryPlanner:
    return QueryPlanner(llm if llm is not None else object())


# --------------------------------------------------------------------------- #
# flight_cancellation                                                          #
# --------------------------------------------------------------------------- #
async def test_flight_cancellation_structure() -> None:
    plan = await _planner().plan(
        _intent("flight_cancellation", airline="Turkish Airlines"), None, _USER
    )
    assert isinstance(plan, ExecutionPlan)
    ids = [s.id for s in plan.steps]
    assert ids == ["find_booking", "find_flight_event", "draft_cancellation"]

    by_id = {s.id: s for s in plan.steps}
    # s1 carries a broad-search fallback.
    fallback = by_id["find_booking"].fallback
    assert isinstance(fallback, PlanStep)
    assert fallback.id == "find_booking_broad"
    assert "Turkish Airlines" in by_id["find_booking"].params["query"]

    # s2 and s3 both depend ONLY on find_booking (a single parallel wave 2).
    assert by_id["find_flight_event"].depends_on == ["find_booking"]
    assert by_id["draft_cancellation"].depends_on == ["find_booking"]

    draft = by_id["draft_cancellation"]
    assert draft.action == "draft_email"
    assert draft.params["to"] == ["{{find_booking.top.from_email}}"]
    assert "{{find_booking.top.subject}}" in draft.params["subject"]
    assert "{{find_booking.top.subject}}" in draft.params["body"]

    assert plan.pending_action_template is not None
    assert plan.pending_action_template["params_from_step"] == "draft_cancellation"
    assert plan.pending_action_template["action"] == "send_email"


async def test_flight_cancellation_default_airline() -> None:
    plan = await _planner().plan(_intent("flight_cancellation"), None, _USER)
    booking = plan.steps[0]
    assert "flight" in booking.params["query"].lower()
    assert booking.fallback is not None


# --------------------------------------------------------------------------- #
# meeting_prep                                                                 #
# --------------------------------------------------------------------------- #
async def test_meeting_prep_structure() -> None:
    plan = await _planner().plan(_intent("meeting_prep", company="Acme Corp"), None, _USER)
    assert len(plan.steps) == 4
    by_id = {s.id: s for s in plan.steps}

    roots = [s.id for s in plan.steps if not s.depends_on]
    assert set(roots) == {"find_meeting", "find_emails", "find_files"}

    assert by_id["attendee_emails"].depends_on == ["find_meeting"]
    assert by_id["attendee_emails"].params["query"] == "{{find_meeting.top.attendees}}"

    # Everything in meeting prep is best-effort/optional.
    assert all(s.optional for s in plan.steps)

    # find_meeting defaults to a tomorrow full-day window when no range given.
    assert "starts_after" in by_id["find_meeting"].params
    assert "starts_before" in by_id["find_meeting"].params


def test_meeting_prep_agents_map_to_services() -> None:
    plan = PLAN_TEMPLATES["meeting_prep"](
        _intent("meeting_prep", topic="Q3 launch"), None, datetime.now(ZoneInfo(TZ))
    )
    by_id = {s.id: s for s in plan.steps}
    assert by_id["find_meeting"].agent == "calendar"
    assert by_id["find_emails"].agent == "gmail"
    assert by_id["find_files"].agent == "drive"


# --------------------------------------------------------------------------- #
# calendar_search                                                              #
# --------------------------------------------------------------------------- #
async def test_calendar_search_with_attendee_and_timerange() -> None:
    start = datetime(2026, 7, 6, 0, 0, tzinfo=ZoneInfo(TZ))
    end = datetime(2026, 7, 12, 23, 59, 59, tzinfo=ZoneInfo(TZ))
    tr = TimeRange(start=start, end=end, label="next week")
    plan = await _planner().plan(
        _intent("calendar_search", person_emails=["john@example.com"], topic="sync"),
        tr,
        _USER,
    )
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.id == "search_events"
    assert step.agent == "calendar"
    assert step.params["attendee"] == "john@example.com"
    assert step.params["starts_after"] == start.isoformat()
    assert step.params["starts_before"] == end.isoformat()
    assert step.params["query"] == "sync"


async def test_calendar_search_no_timerange_defaults_to_now() -> None:
    before = datetime.now(ZoneInfo(TZ))
    plan = await _planner().plan(_intent("calendar_search", topic="standup"), None, _USER)
    after = datetime.now(ZoneInfo(TZ))

    step = plan.steps[0]
    assert "starts_before" not in step.params
    starts_after = datetime.fromisoformat(step.params["starts_after"])
    assert before <= starts_after <= after


# --------------------------------------------------------------------------- #
# email_search                                                                 #
# --------------------------------------------------------------------------- #
async def test_email_search_maps_all_fields() -> None:
    start = datetime(2026, 6, 1, 0, 0, tzinfo=ZoneInfo(TZ))
    end = datetime(2026, 6, 30, 23, 59, 59, tzinfo=ZoneInfo(TZ))
    tr = TimeRange(start=start, end=end, label="last month")
    plan = await _planner().plan(
        _intent(
            "email_search",
            person_emails=["sarah@example.com"],
            label="INBOX",
            topic="budget",
        ),
        tr,
        _USER,
    )
    step = plan.steps[0]
    assert step.agent == "gmail"
    assert step.action == "search_emails"
    assert step.params["from_email"] == "sarah@example.com"
    assert step.params["after"] == start.isoformat()
    assert step.params["before"] == end.isoformat()
    assert step.params["label"] == "INBOX"
    assert step.params["query"] == "budget"


# --------------------------------------------------------------------------- #
# drive_search                                                                 #
# --------------------------------------------------------------------------- #
async def test_drive_search_pdf_hint_and_timerange() -> None:
    start = datetime(2026, 6, 1, 0, 0, tzinfo=ZoneInfo(TZ))
    end = datetime(2026, 6, 30, 23, 59, 59, tzinfo=ZoneInfo(TZ))
    tr = TimeRange(start=start, end=end, label="last month")
    plan = await _planner().plan(
        _intent("drive_search", file_hint="pdf", topic="roadmap"), tr, _USER
    )
    step = plan.steps[0]
    assert step.agent == "drive"
    assert step.action == "search_files"
    assert step.params["mime_type"] == "application/pdf"
    assert step.params["modified_after"] == start.isoformat()
    assert step.params["modified_before"] == end.isoformat()


def test_drive_search_mime_hint_mappings() -> None:
    now = datetime.now(ZoneInfo(TZ))
    xlsx = PLAN_TEMPLATES["drive_search"](
        _intent("drive_search", file_hint="the xlsx budget"), None, now
    )
    assert xlsx.steps[0].params["mime_type"] == "application/vnd.google-apps.spreadsheet"

    doc = PLAN_TEMPLATES["drive_search"](
        _intent("drive_search", file_hint="the roadmap doc"), None, now
    )
    assert doc.steps[0].params["mime_type"] == "application/vnd.google-apps.document"

    none = PLAN_TEMPLATES["drive_search"](
        _intent("drive_search", topic="anything"), None, now
    )
    assert "mime_type" not in none.steps[0].params


# --------------------------------------------------------------------------- #
# calendar_action                                                              #
# --------------------------------------------------------------------------- #
async def test_calendar_action_move_requires_confirmation() -> None:
    plan = await _planner().plan(
        _intent("calendar_action", event_title="Q3 sync", topic="move the Q3 sync meeting"),
        None,
        _USER,
    )
    assert len(plan.steps) == 2
    find_target, mutate = plan.steps

    assert find_target.id == "find_target"
    assert find_target.agent == "calendar"
    assert find_target.expect_single is True

    assert mutate.action == "update_event"
    assert mutate.requires_confirmation is True
    assert mutate.depends_on == ["find_target"]
    assert mutate.params["event_id"] == "{{find_target.top.id}}"


async def test_calendar_action_cancel_maps_to_delete() -> None:
    plan = await _planner().plan(
        _intent("calendar_action", event_title="Q3 sync", topic="cancel the Q3 sync meeting"),
        None,
        _USER,
    )
    mutate = plan.steps[1]
    assert mutate.action == "delete_event"
    assert mutate.requires_confirmation is True
    assert mutate.params["event_id"] == "{{find_target.top.id}}"


# --------------------------------------------------------------------------- #
# drive_action                                                                 #
# --------------------------------------------------------------------------- #
async def test_drive_action_share_requires_confirmation() -> None:
    plan = await _planner().plan(
        _intent("drive_action", file_hint="roadmap doc", person_emails=["priya@example.com"]),
        None,
        _USER,
    )
    assert len(plan.steps) == 2
    find_target, share = plan.steps

    assert find_target.expect_single is True
    assert share.action == "share_file"
    assert share.requires_confirmation is True
    assert share.depends_on == ["find_target"]
    assert share.params["file_id"] == "{{find_target.top.id}}"
    assert share.params["email"] == "priya@example.com"


# --------------------------------------------------------------------------- #
# empty-plan intents                                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("intent_name", ["chitchat", "confirm_action", "clarification_reply"])
async def test_conversational_intents_yield_empty_plan(intent_name: str) -> None:
    plan = await _planner().plan(_intent(intent_name), None, _USER)
    assert isinstance(plan, ExecutionPlan)
    assert plan.steps == []
    assert plan.pending_action_template is None


# --------------------------------------------------------------------------- #
# complex_multi_service (LLM-backed)                                           #
# --------------------------------------------------------------------------- #
async def test_complex_valid_llm_plan_is_parsed() -> None:
    output = LLMPlanOutput(
        steps=[
            LLMPlanStep(
                id="mail",
                agent="gmail",
                action="search_emails",
                params_json='{"query": "Q3 launch", "k": 3}',
            ),
            LLMPlanStep(
                id="thread",
                agent="gmail",
                action="get_email",
                params_json='{"email_id": "{{mail.top.id}}"}',
                depends_on=["mail"],
            ),
        ]
    )
    llm = _StubLLM(output)
    plan = await _planner(llm).plan(
        _intent("complex_multi_service", topic="Q3 launch"), None, _USER
    )
    assert llm.calls, "planner should call the LLM for complex_multi_service"
    assert [s.id for s in plan.steps] == ["mail", "thread"]

    mail = plan.steps[0]
    assert mail.agent == "gmail"
    assert mail.action == "search_emails"
    assert mail.params == {"query": "Q3 launch", "k": 3}  # json-decoded

    thread = plan.steps[1]
    assert thread.depends_on == ["mail"]
    assert thread.params == {"email_id": "{{mail.top.id}}"}


async def test_complex_unknown_action_falls_back_to_safe_plan() -> None:
    output = LLMPlanOutput(
        steps=[
            LLMPlanStep(
                id="x",
                agent="gmail",
                action="frobnicate",  # not a canonical action
                params_json="{}",
            )
        ]
    )
    plan = await _planner(_StubLLM(output)).plan(
        _intent("complex_multi_service", topic="launch"), None, _USER
    )
    assert [s.id for s in plan.steps] == ["search_emails", "search_events", "search_files"]
    assert [s.agent for s in plan.steps] == ["gmail", "calendar", "drive"]
    assert all(s.optional for s in plan.steps)


async def test_complex_bad_params_json_falls_back() -> None:
    output = LLMPlanOutput(
        steps=[
            LLMPlanStep(
                id="x",
                agent="gmail",
                action="search_emails",
                params_json="{not valid json",
            )
        ]
    )
    plan = await _planner(_StubLLM(output)).plan(
        _intent("complex_multi_service", topic="launch"), None, _USER
    )
    assert [s.id for s in plan.steps] == ["search_emails", "search_events", "search_files"]
    assert all(s.optional for s in plan.steps)


async def test_complex_unknown_dependency_falls_back() -> None:
    output = LLMPlanOutput(
        steps=[
            LLMPlanStep(
                id="a",
                agent="gmail",
                action="search_emails",
                params_json='{"query": "x"}',
                depends_on=["ghost"],  # references a non-existent step
            )
        ]
    )
    plan = await _planner(_StubLLM(output)).plan(
        _intent("complex_multi_service", topic="x"), None, _USER
    )
    assert [s.id for s in plan.steps] == ["search_emails", "search_events", "search_files"]


async def test_complex_llm_exception_falls_back() -> None:
    class _BoomLLM:
        async def complete_structured(self, system, user, response_model):
            raise RuntimeError("provider down")

        async def complete_text(self, system, user):  # pragma: no cover
            return ""

    plan = await _planner(_BoomLLM()).plan(
        _intent("complex_multi_service", topic="x"), None, _USER
    )
    assert [s.id for s in plan.steps] == ["search_emails", "search_events", "search_files"]


# --------------------------------------------------------------------------- #
# timezone fallback                                                            #
# --------------------------------------------------------------------------- #
async def test_plan_uses_default_tz_when_user_has_none() -> None:
    # user without a timezone attribute must not raise (falls back to settings).
    plan = await _planner().plan(_intent("calendar_search", topic="x"), None, object())
    assert "starts_after" in plan.steps[0].params
