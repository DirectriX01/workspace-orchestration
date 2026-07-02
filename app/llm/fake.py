"""Deterministic, dependency-free stand-in for a real LLM.

Selected when ``LLM_PROVIDER=fake`` (see :func:`app.llm.client.get_llm_client`).
Used by the unit test-suite and for running the demo without an API key. It
implements the :class:`~app.llm.client.LLMClient` protocol structurally.

``complete_structured`` dispatches on ``response_model.__name__`` and applies
plain keyword rules; ``complete_text`` returns a fixed-shape summary. Models
from :mod:`app.core.intent` / :mod:`app.core.planner` are imported lazily inside
the methods to avoid import cycles.
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

#: Temporal phrases recognised (matched positionally — first occurrence wins).
_TEMPORAL_RE = re.compile(
    r"next week|this week|tomorrow|today|next tuesday|last week|last month|next month"
)
#: Email address extractor.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
#: "<Word> Airlines"/"Airline".
_AIRLINE_RE = re.compile(r"([A-Za-z][A-Za-z0-9]*)\s+airlines?\b", re.IGNORECASE)
#: "<Word> Corp".
_CORP_RE = re.compile(r"([A-Za-z][A-Za-z0-9&']*)\s+corp\b", re.IGNORECASE)
#: A double-quoted span (used to detect an explicitly named event title).
_QUOTED_RE = re.compile(r'"([^"]+)"')


class FakeLLM:
    """Rule-based, deterministic :class:`LLMClient` implementation."""

    async def complete_structured(
        self, system: str, user: str, response_model: type[T]
    ) -> T:
        """Return a deterministic instance of ``response_model``."""
        name = response_model.__name__
        if name == "IntentResult":
            return self._fake_intent(user)  # type: ignore[return-value]
        if name == "LLMPlanOutput":
            return self._fake_plan(user)  # type: ignore[return-value]
        raise ValueError(f"FakeLLM cannot synthesize response_model {name!r}")

    async def complete_text(self, system: str, user: str) -> str:
        """Return a compact, deterministic summary of ``user``."""
        snippet = " ".join(user.split())[:800]
        if not snippet:
            return "Here is what I found:"
        return f"Here is what I found:\n- {snippet}"

    # ------------------------------------------------------------------ #
    # IntentResult                                                       #
    # ------------------------------------------------------------------ #
    def _fake_intent(self, query: str) -> BaseModel:
        # Local import avoids an app.core.intent <-> app.llm import cycle.
        from app.core.intent import Entities, IntentResult

        lowered = query.lower()
        # Punctuation-insensitive form so "yes, send it" matches "yes send".
        squished = re.sub(r"[^a-z0-9]+", " ", lowered).strip()

        temporal = self._temporal_phrase(lowered)
        emails = _EMAIL_RE.findall(query)
        references_prior = ("that " in lowered) or ("the proposal" in lowered)

        entities = Entities(person_emails=emails, topic=query)
        base = {
            "temporal_phrase": temporal,
            "references_prior_context": references_prior,
        }

        # Rules in strict priority order.
        if any(kw in squished for kw in ("confirm", "yes send", "go ahead")):
            return IntentResult(
                intent="confirm_action", services=[], entities=entities, **base
            )

        if "cancel" in squished and ("flight" in squished or "airline" in squished):
            entities.airline = self._extract_airline(query)
            return IntentResult(
                intent="flight_cancellation",
                services=["gmail", "calendar"],
                entities=entities,
                **base,
            )

        if ("prepare" in squished or "prep" in squished) and "meeting" in squished:
            entities.company = self._extract_company(query)
            return IntentResult(
                intent="meeting_prep",
                services=["gmail", "calendar", "drive"],
                entities=entities,
                **base,
            )

        if ("move" in squished or "reschedule" in squished) and "meeting" in squished:
            quoted = _QUOTED_RE.search(query)
            if quoted:
                entities.event_title = quoted.group(1)
                return IntentResult(
                    intent="calendar_action",
                    services=["calendar"],
                    entities=entities,
                    **base,
                )
            return IntentResult(
                intent="calendar_action",
                services=["calendar"],
                entities=entities,
                needs_clarification=True,
                clarification_question="Which meeting do you mean?",
                **base,
            )

        if any(kw in squished for kw in ("calendar", "schedule", "meeting", "event")):
            return IntentResult(
                intent="calendar_search",
                services=["calendar"],
                entities=entities,
                **base,
            )

        if any(kw in squished for kw in ("pdf", "file", "drive", "document", "doc")):
            return IntentResult(
                intent="drive_search",
                services=["drive"],
                entities=entities,
                **base,
            )

        if any(kw in squished for kw in ("email", "mail", "inbox")):
            return IntentResult(
                intent="email_search",
                services=["gmail"],
                entities=entities,
                **base,
            )

        return IntentResult(
            intent="chitchat", services=[], entities=entities, **base
        )

    @staticmethod
    def _temporal_phrase(lowered: str) -> str | None:
        match = _TEMPORAL_RE.search(lowered)
        return match.group(0) if match else None

    @staticmethod
    def _extract_airline(query: str) -> str | None:
        match = _AIRLINE_RE.search(query)
        if not match:
            return None
        return " ".join(word.capitalize() for word in match.group(0).split())

    @staticmethod
    def _extract_company(query: str) -> str | None:
        match = _CORP_RE.search(query)
        if not match:
            return None
        return " ".join(word.capitalize() for word in match.group(0).split())

    # ------------------------------------------------------------------ #
    # LLMPlanOutput                                                      #
    # ------------------------------------------------------------------ #
    def _fake_plan(self, query: str) -> BaseModel:
        # Local import: app.core.planner may import from app.llm at module load.
        from app.core.planner import LLMPlanOutput, LLMPlanStep

        return LLMPlanOutput(
            steps=[
                LLMPlanStep(
                    id="search_emails",
                    agent="gmail",
                    action="search_emails",
                    params_json=json.dumps({"query": query}),
                    depends_on=[],
                )
            ]
        )
