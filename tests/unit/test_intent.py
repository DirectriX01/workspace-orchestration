"""Unit tests for intent classification, the fake LLM, and the context store.

These tests are dependency-free: no Postgres, Redis, or OpenAI. The LLM is
either the deterministic :class:`~app.llm.fake.FakeLLM` or a hand-written
recording stub, and Redis is a tiny in-memory fake defined below.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.context import ConversationStore
from app.core.intent import Entities, IntentClassifier, IntentResult
from app.llm.fake import FakeLLM

IST = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------------- #
# (a) IntentClassifier assembles the user message and returns the model as-is #
# --------------------------------------------------------------------------- #
class RecordingLLM:
    """Stub LLM: records the system/user strings and returns a canned result."""

    def __init__(self, canned: IntentResult) -> None:
        self._canned = canned
        self.system: str | None = None
        self.user: str | None = None
        self.response_model: type | None = None

    async def complete_structured(self, system, user, response_model):
        self.system = system
        self.user = user
        self.response_model = response_model
        return self._canned

    async def complete_text(self, system, user):  # pragma: no cover - unused here
        return "unused"


async def test_classifier_builds_user_message_and_returns_model_unchanged():
    canned = IntentResult(
        intent="email_search",
        services=["gmail"],
        entities=Entities(topic="budget"),
    )
    llm = RecordingLLM(canned)
    classifier = IntentClassifier(llm)

    now = datetime(2026, 7, 2, 9, 30, 0, tzinfo=IST)
    turns = [
        {
            "query": "show my emails",
            "intent": "email_search",
            "resolved_entities": {"topic": "emails"},
        },
        {
            "query": "any from bob?",
            "intent": "email_search",
            "resolved_entities": {"person_names": ["bob"]},
        },
    ]
    query = "what about the budget email?"

    result = await classifier.classify(query, turns, "Asia/Kolkata", now)

    # Returns the model unchanged.
    assert result is canned
    assert llm.response_model is IntentResult

    user = llm.user
    assert user is not None
    # Numbered turns, oldest first.
    assert "1." in user
    assert "2." in user
    assert "show my emails" in user
    assert "any from bob?" in user
    # Timezone, ISO now, and the query are all present.
    assert "Asia/Kolkata" in user
    assert now.isoformat() in user
    assert query in user
    # A non-empty system prompt was passed too.
    assert llm.system and "intent" in llm.system.lower()


async def test_classifier_handles_no_prior_turns():
    canned = IntentResult(intent="chitchat", services=[], entities=Entities())
    llm = RecordingLLM(canned)
    classifier = IntentClassifier(llm)
    now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=IST)

    await classifier.classify("hello there", [], "Asia/Kolkata", now)

    assert llm.user is not None
    assert "hello there" in llm.user
    assert now.isoformat() in llm.user


# --------------------------------------------------------------------------- #
# (b) FakeLLM keyword rules                                                    #
# --------------------------------------------------------------------------- #
async def _classify(query: str) -> IntentResult:
    result = await FakeLLM().complete_structured("system", query, IntentResult)
    assert isinstance(result, IntentResult)
    return result


async def test_fake_flight_cancellation():
    result = await _classify("Cancel my Turkish Airlines flight")
    assert result.intent == "flight_cancellation"
    assert result.services == ["gmail", "calendar"]
    assert result.entities.airline == "Turkish Airlines"


async def test_fake_meeting_prep_with_temporal():
    result = await _classify("Prepare for tomorrow's meeting with Acme Corp")
    assert result.intent == "meeting_prep"
    assert result.services == ["gmail", "calendar", "drive"]
    assert result.temporal_phrase == "tomorrow"
    assert result.entities.company == "Acme Corp"


async def test_fake_calendar_search_with_temporal():
    result = await _classify("What's on my calendar next week?")
    assert result.intent == "calendar_search"
    assert result.temporal_phrase == "next week"


async def test_fake_move_meeting_needs_clarification():
    result = await _classify("Move the meeting with John")
    assert result.intent == "calendar_action"
    assert result.needs_clarification is True
    assert result.clarification_question == "Which meeting do you mean?"


async def test_fake_move_meeting_with_quoted_title_skips_clarification():
    result = await _classify('Move the "Weekly Sync" meeting to Friday')
    assert result.intent == "calendar_action"
    assert result.needs_clarification is False
    assert result.entities.event_title == "Weekly Sync"


async def test_fake_confirm_action_with_punctuation():
    result = await _classify("yes, send it")
    assert result.intent == "confirm_action"
    assert result.services == []


async def test_fake_email_search_extracts_email():
    result = await _classify("Find emails from sarah@company.com about the budget")
    assert result.intent == "email_search"
    assert result.entities.person_emails == ["sarah@company.com"]
    assert result.entities.topic == "Find emails from sarah@company.com about the budget"


async def test_fake_drive_search_with_temporal():
    result = await _classify("Show me PDFs in Drive from last month")
    assert result.intent == "drive_search"
    assert result.temporal_phrase == "last month"


async def test_fake_chitchat_default():
    result = await _classify("Thanks so much!")
    assert result.intent == "chitchat"
    assert result.temporal_phrase is None


async def test_fake_references_prior_context():
    result = await _classify("Forward that email to my manager")
    assert result.references_prior_context is True


async def test_fake_unknown_model_raises():
    class Mystery(IntentResult):
        pass

    # A model whose __name__ the FakeLLM does not recognise.
    Mystery.__name__ = "TotallyUnknownModel"
    with pytest.raises(ValueError):
        await FakeLLM().complete_structured("s", "u", Mystery)


async def test_fake_complete_text_shape():
    text = await FakeLLM().complete_text("system", "line one\nline two")
    assert text.splitlines()[0] == "Here is what I found:"
    assert "line one line two" in text


# --------------------------------------------------------------------------- #
# (c) ConversationStore against an in-memory fake Redis                        #
# --------------------------------------------------------------------------- #
class FakeAsyncRedis:
    """Minimal async Redis fake supporting the ops ConversationStore uses."""

    def __init__(self) -> None:
        self._lists: dict[str, list[str]] = {}
        self._kv: dict[str, str] = {}

    @staticmethod
    def _resolve_end(length: int, end: int) -> int:
        return length - 1 if end < 0 else end

    async def lpush(self, key: str, value: str) -> int:
        self._lists.setdefault(key, []).insert(0, value)
        return len(self._lists[key])

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = self._lists.get(key, [])
        stop = self._resolve_end(len(items), end)
        return items[start : stop + 1]

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        items = self._lists.get(key, [])
        stop = self._resolve_end(len(items), end)
        self._lists[key] = items[start : stop + 1]
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        return key in self._lists or key in self._kv

    async def get(self, key: str) -> str | None:
        return self._kv.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._kv[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self._kv.pop(key, None) is not None else 0


async def test_get_turns_returns_last_five_oldest_first():
    store = ConversationStore(FakeAsyncRedis())
    conversation_id = "conv-1"

    for i in range(7):
        await store.append_turn(conversation_id, {"query": f"q{i}"})

    turns = await store.get_turns(conversation_id)
    assert len(turns) == 5
    # Oldest-first: the 7 appends keep the newest 5 (q2..q6) in chronological order.
    assert [turn["query"] for turn in turns] == ["q2", "q3", "q4", "q5", "q6"]


async def test_get_turns_empty_when_no_history():
    store = ConversationStore(FakeAsyncRedis())
    assert await store.get_turns("never-seen") == []


async def test_pending_action_set_get_clear_roundtrip():
    store = ConversationStore(FakeAsyncRedis())
    conversation_id = "conv-2"

    assert await store.get_pending_action(conversation_id) is None

    action = {"description": "send the drafted email", "step": "draft_cancellation"}
    await store.set_pending_action(conversation_id, action)
    assert await store.get_pending_action(conversation_id) == action

    await store.set_pending_action(conversation_id, None)
    assert await store.get_pending_action(conversation_id) is None
