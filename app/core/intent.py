"""Intent classification.

Turns a natural-language user query (plus recent conversation context) into a
structured :class:`IntentResult` via an :class:`~app.llm.client.LLMClient`.

The classifier is intentionally *thin*: it owns a carefully engineered system
prompt and the assembly of the user message, then delegates the actual JSON
generation to the injected LLM. All temporal reasoning is deferred to
``app.core.temporal`` downstream — the model is instructed to copy temporal
phrases *verbatim* rather than compute concrete dates.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.llm.client import LLMClient


class Entities(BaseModel):
    """Structured entities extracted from a query."""

    person_names: list[str] = Field(default_factory=list)
    person_emails: list[str] = Field(default_factory=list)
    company: str | None = None
    airline: str | None = None
    event_title: str | None = None
    file_hint: str | None = None
    label: str | None = None
    topic: str | None = None


class IntentResult(BaseModel):
    """The classifier's structured verdict for a single user turn."""

    intent: Literal[
        "email_search",
        "email_action",
        "calendar_search",
        "calendar_action",
        "drive_search",
        "drive_action",
        "meeting_prep",
        "flight_cancellation",
        "complex_multi_service",
        "confirm_action",
        "clarification_reply",
        "chitchat",
    ]
    services: list[Literal["gmail", "calendar", "drive"]] = Field(default_factory=list)
    entities: Entities = Field(default_factory=Entities)
    temporal_phrase: str | None = None
    needs_clarification: bool = False
    clarification_question: str | None = None
    references_prior_context: bool = False


#: The system prompt. Kept as a module-level constant so it is trivial to unit
#: test and to diff across prompt-engineering iterations.
SYSTEM_PROMPT = """\
You are the intent router for a personal workspace assistant that operates over \
a user's Gmail, Google Calendar, and Google Drive. Classify the user's latest \
message into exactly one intent and extract the entities needed to act on it. \
Respond ONLY with the requested structured JSON.

INTENT TAXONOMY (choose exactly one):
- email_search: read-only lookup of emails. e.g. "Find emails from Sarah about the budget".
- email_action: a mutating email operation such as labelling, forwarding, or drafting/sending a reply. e.g. "Archive the newsletter from Stripe".
- calendar_search: read-only lookup that needs CALENDAR DATA ONLY. e.g. "What's on my calendar next week?".
- calendar_action: a mutating calendar operation (create / move / reschedule / cancel an event). e.g. "Move my 3pm to Friday".
- drive_search: read-only lookup of Drive files. e.g. "Show me the PDFs I got last month".
- drive_action: a mutating Drive operation (share, move, create folder). e.g. "Share the roadmap doc with Priya".
- meeting_prep: gather everything needed before an upcoming meeting (the event, related emails, related files). e.g. "Prepare me for tomorrow's meeting with Acme Corp".
- flight_cancellation: find a flight booking and draft a cancellation. e.g. "Cancel my Turkish Airlines flight".
- complex_multi_service: a request that needs data from MORE THAN ONE service combined (and is not meeting_prep or flight_cancellation), even when phrased as a simple lookup. If answering requires reading a document AND checking the calendar, or cross-referencing mail with files, it belongs here. e.g. "Summarize everything about the Q3 launch across my mail, calendar, and drive"; "Find events next week that conflict with my out-of-office doc".
- confirm_action: the user is approving a previously offered pending action. e.g. "Yes, send it" / "Go ahead" / "Confirm".
- clarification_reply: the user is answering a clarifying question you asked on the previous turn. e.g. (after "Which meeting?") "The one with John".
- chitchat: greetings, thanks, or anything unrelated to the workspace. e.g. "Thanks!".

RULES:
1. TEMPORAL PHRASES: never compute dates. Copy the user's temporal wording \
VERBATIM into `temporal_phrase` (e.g. "next week", "tomorrow", "last month", \
"next Tuesday", "this weekend", "in 3 days"). A downstream resolver converts it \
using the provided timezone and current time. Use null when there is no temporal \
wording.
2. COMPOSITE INTENTS TAKE PRECEDENCE: if the request matches flight_cancellation \
or meeting_prep, choose that over the plain search/action intents even though it \
also touches search.
3. CLARIFICATION FOR MUTATIONS: set `needs_clarification` = true and populate \
`clarification_question` ONLY when a MUTATING action (calendar_action, \
email_action, drive_action) refers to an entity that is ambiguous or \
underspecified — e.g. "Move the meeting with John" does not say which meeting or \
to when. Read-only searches never need clarification.
4. CONFIRMATION: use confirm_action when the user is approving something you \
offered earlier ("yes send it", "confirm", "go ahead", "do it"). Do not extract \
new entities for a confirmation.
5. CONTEXT CARRY-OVER: when the user says "that email", "the meeting", "it", or \
similar, set `references_prior_context` = true and COPY the concrete email \
address / event id / entity from the most recent relevant turn's \
resolved_entities into `entities`.
6. SERVICES: list every Google service the intent will touch \
(gmail / calendar / drive). meeting_prep -> [gmail, calendar, drive]; \
flight_cancellation -> [gmail, calendar]; a plain email search -> [gmail]; \
chitchat / confirm_action -> [].
7. Extract emails into person_emails, human names into person_names, a company \
into company, an airline into airline, a quoted or named event title into \
event_title, a file description into file_hint, a Gmail label into label, and \
the free-text subject of the request into topic.
"""


class IntentClassifier:
    """Classify a user query into a structured :class:`IntentResult`."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def classify(
        self,
        query: str,
        turns: list[dict],
        user_tz: str,
        now: datetime,
    ) -> IntentResult:
        """Classify ``query`` given recent ``turns`` and temporal context."""
        user = self._build_user_message(query, turns, user_tz, now)
        return await self._llm.complete_structured(SYSTEM_PROMPT, user, IntentResult)

    @staticmethod
    def _build_user_message(
        query: str,
        turns: list[dict],
        user_tz: str,
        now: datetime,
    ) -> str:
        """Assemble the user message: numbered turns, tz, ISO now, then query."""
        lines: list[str] = []

        if turns:
            lines.append("Recent conversation turns (oldest first):")
            for index, turn in enumerate(turns, start=1):
                turn_query = turn.get("query", "")
                turn_intent = turn.get("intent", "")
                resolved = turn.get("resolved_entities", turn.get("entities", {}))
                entities_json = json.dumps(resolved, default=str, sort_keys=True)
                lines.append(
                    f"{index}. query: {turn_query} | intent: {turn_intent} | "
                    f"resolved_entities: {entities_json}"
                )
        else:
            lines.append("Recent conversation turns: (none)")

        lines.append("")
        lines.append(f"Timezone: {user_tz}")
        lines.append(f"Now (ISO): {now.isoformat()}")
        lines.append(f"Current user query: {query}")
        return "\n".join(lines)
