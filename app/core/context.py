"""Conversation context store backed by Redis.

Persists the recent turn history and any single "pending action" (an operation
that was offered to the user and is awaiting their confirmation) for a
conversation. Turn history is capped at the five most recent turns and expires
after 24h; a pending action expires after 30 minutes.

Values are JSON-encoded. ``get_turns`` returns turns oldest-first even though
they are stored newest-first (via ``LPUSH``), so callers can feed them to the
classifier in chronological order.
"""

from __future__ import annotations

import json
from typing import Any

#: Redis clients return ``str`` when ``decode_responses=True`` and ``bytes``
#: otherwise. ``json.loads`` accepts both, so no explicit decode is needed.

#: Maximum turns retained per conversation.
MAX_TURNS = 5
#: TTL (seconds) for the turn-history list — 24 hours.
TURNS_TTL = 86400
#: TTL (seconds) for a pending action — 30 minutes.
PENDING_TTL = 1800


class ConversationStore:
    """Redis-backed store for per-conversation turn history and pending action.

    Args:
        redis: A ``redis.asyncio.Redis`` client (or any object exposing the
            same async ``lpush``/``lrange``/``ltrim``/``get``/``set``/
            ``delete``/``expire`` methods).
    """

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    @staticmethod
    def _turns_key(conversation_id: str) -> str:
        return f"conv:{conversation_id}:turns"

    @staticmethod
    def _pending_key(conversation_id: str) -> str:
        return f"conv:{conversation_id}:pending"

    async def get_turns(self, conversation_id: str) -> list[dict]:
        """Return up to the five most recent turns, oldest-first."""
        key = self._turns_key(conversation_id)
        raw = await self._redis.lrange(key, 0, MAX_TURNS - 1)
        # Stored newest-first via LPUSH; reverse to chronological order.
        return [json.loads(item) for item in reversed(raw)]

    async def append_turn(self, conversation_id: str, turn: dict) -> None:
        """Append a turn, trimming to the five most recent and refreshing TTL."""
        key = self._turns_key(conversation_id)
        await self._redis.lpush(key, json.dumps(turn))
        await self._redis.ltrim(key, 0, MAX_TURNS - 1)
        await self._redis.expire(key, TURNS_TTL)

    async def get_pending_action(self, conversation_id: str) -> dict | None:
        """Return the pending action awaiting confirmation, or ``None``."""
        raw = await self._redis.get(self._pending_key(conversation_id))
        if raw is None:
            return None
        return json.loads(raw)

    async def set_pending_action(
        self, conversation_id: str, action: dict | None
    ) -> None:
        """Set (with TTL) or, when ``action`` is ``None``, clear the pending action."""
        key = self._pending_key(conversation_id)
        if action is None:
            await self._redis.delete(key)
        else:
            await self._redis.set(key, json.dumps(action), ex=PENDING_TTL)
