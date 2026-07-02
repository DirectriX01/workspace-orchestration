"""LLM client abstraction.

Provides an async interface (:class:`LLMClient`) with two capabilities:

* ``complete_structured`` — constrained JSON generation validated into a
  Pydantic model using OpenAI structured outputs (strict ``json_schema``).
* ``complete_text`` — plain free-form text completion.

The concrete :class:`OpenAILLM` implementation talks to the OpenAI Chat
Completions API. :func:`get_llm_client` returns the provider configured via
settings (``"openai"`` or ``"fake"``).
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any, Protocol, TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import get_settings

T = TypeVar("T", bound=BaseModel)

#: Number of retries (in addition to the initial attempt) for transient errors.
_MAX_RETRIES = 3


class LLMError(RuntimeError):
    """Raised when the LLM provider fails after retries or returns no content."""


class LLMClient(Protocol):
    """Structural interface implemented by every LLM provider."""

    async def complete_structured(
        self, system: str, user: str, response_model: type[T]
    ) -> T:
        """Generate JSON constrained to ``response_model`` and validate it."""
        ...

    async def complete_text(self, system: str, user: str) -> str:
        """Generate a free-form text completion."""
        ...


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Build an OpenAI strict-mode JSON schema for ``model``.

    Takes ``model.model_json_schema()`` and, for every object node anywhere in
    the tree (including entries under ``$defs``), sets
    ``additionalProperties=False`` and marks *every* declared property as
    required. OpenAI's strict structured-output mode requires both.
    """
    schema = copy.deepcopy(model.model_json_schema())
    _enforce_strict(schema)
    return schema


def _enforce_strict(node: Any) -> None:
    """Recursively apply strict-mode constraints to a JSON-schema fragment."""
    if isinstance(node, dict):
        if node.get("type") == "object":
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties.keys())
            node["additionalProperties"] = False
        for value in list(node.values()):
            _enforce_strict(value)
    elif isinstance(node, list):
        for item in node:
            _enforce_strict(item)


class OpenAILLM:
    """OpenAI-backed :class:`LLMClient` implementation."""

    def __init__(self) -> None:
        settings = get_settings()
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.llm_model

    async def complete_structured(
        self, system: str, user: str, response_model: type[T]
    ) -> T:
        completion = await self._create(
            model=self._model,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": strict_schema(response_model),
                },
            },
        )
        content = completion.choices[0].message.content
        if not content:
            raise LLMError("OpenAI returned empty content for structured completion")
        return response_model.model_validate_json(content)

    async def complete_text(self, system: str, user: str) -> str:
        completion = await self._create(
            model=self._model,
            temperature=0.3,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = completion.choices[0].message.content
        if not content:
            raise LLMError("OpenAI returned empty content for text completion")
        return content

    async def _create(self, **kwargs: Any) -> Any:
        """Call the Chat Completions API, retrying transient failures.

        Retries up to ``_MAX_RETRIES`` times with exponential backoff
        (1s, 2s, 4s) on :class:`openai.RateLimitError` and on
        :class:`openai.APIStatusError` with a 5xx status. Non-5xx status errors
        propagate immediately; exhausting retries raises :class:`LLMError`.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return await self._client.chat.completions.create(**kwargs)
            except openai.RateLimitError as exc:
                last_exc = exc
            except openai.APIStatusError as exc:
                if exc.status_code < 500:
                    raise
                last_exc = exc
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(2**attempt)
        raise LLMError(
            f"OpenAI request failed after {_MAX_RETRIES + 1} attempts"
        ) from last_exc


def get_llm_client() -> LLMClient:
    """Return the configured LLM client.

    ``llm_provider == "openai"`` yields :class:`OpenAILLM`. ``"fake"`` lazily
    imports :class:`app.llm.fake.FakeLLM`, which is introduced in a later phase.
    """
    provider = get_settings().llm_provider
    if provider == "fake":
        try:
            from app.llm.fake import FakeLLM
        except ImportError as exc:
            raise RuntimeError(
                "LLM_PROVIDER=fake requires app.llm.fake — added in core phase"
            ) from exc
        return FakeLLM()
    return OpenAILLM()
