"""Agent base class and shared dependency container.

Every service agent (gmail / calendar / drive) is a thin adapter over the
:class:`~app.search.hybrid.HybridSearcher` (reads) and a normalized service
client (writes). :class:`BaseAgent.run` dispatches a canonical action string to
one of three handlers:

* ``search_*``  -> :meth:`BaseAgent.search`      (vector + recency search)
* ``get_*``     -> :meth:`BaseAgent.get_context`  (cache-first single-doc fetch)
* everything else -> :meth:`BaseAgent.execute`    (mutations; unknown -> ValueError)

Results are plain, JSON-serializable dicts so that the DAG executor can persist
them and resolve ``{{step.path}}`` templates from later steps.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from app.search.embeddings import EmbeddingService
from app.search.hybrid import HybridSearcher


@dataclass
class AgentDeps:
    """Per-user, per-request dependency bundle shared by all agents."""

    user: Any
    client: Any
    searcher: HybridSearcher
    embedder: EmbeddingService


class BaseAgent(ABC):
    """Common dispatch/serialization behaviour for the service agents."""

    name: ClassVar[str]

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def run(self, action: str, params: dict) -> dict:
        """Dispatch a canonical action to search / get_context / execute."""
        if action.startswith("search_"):
            return await self.search(params)
        if action.startswith("get_"):
            return await self.get_context(params)
        return await self.execute(action, params)

    @abstractmethod
    async def search(self, params: dict) -> dict:
        """Return ``{"status": "ok"|"empty", "results": [...]}``."""

    @abstractmethod
    async def get_context(self, params: dict) -> dict:
        """Return ``{"status": "ok", "results": [full_doc]}`` (cache-first)."""

    @abstractmethod
    async def execute(self, action: str, params: dict) -> dict:
        """Perform a mutation; unknown actions raise :class:`ValueError`."""

    # ------------------------------------------------------------------ #
    # Shared helpers                                                     #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _search_result(rows: list[dict]) -> dict:
        """Wrap search rows in the standard status envelope."""
        return {"status": "ok" if rows else "empty", "results": rows}

    @staticmethod
    def _as_list(value: Any) -> list:
        """Coerce ``None`` / a bare string / an iterable into a list."""
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)

    @classmethod
    def _jsonify(cls, value: Any) -> Any:
        """Recursively convert datetimes to ISO strings for JSON safety."""
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: cls._jsonify(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._jsonify(item) for item in value]
        return value

    @classmethod
    def _ok(cls, payload: dict) -> dict:
        """JSON-serialize a client payload and stamp an ``ok`` status."""
        result = cls._jsonify(dict(payload))
        result["status"] = "ok"
        return result
