"""Real Google Workspace client implementations.

The Google Python SDK is synchronous, so each client wraps every network call
in :func:`asyncio.to_thread` to satisfy the async client protocols. This module
also hosts the shared :func:`_with_retry` helper used by all three clients.
"""

from __future__ import annotations

import asyncio
from typing import Callable, TypeVar

from googleapiclient.errors import HttpError

T = TypeVar("T")

# HTTP statuses worth retrying: rate limiting and transient server errors.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


async def _with_retry(
    call: Callable[[], T], *, attempts: int = 3, base_delay: float = 1.0
) -> T:
    """Run a sync Google API ``call`` in a thread with retry on transient errors.

    Makes up to ``attempts`` tries (default 3), sleeping with exponential
    backoff (1s, 2s, 4s, ...) between retries. Only :class:`HttpError` responses
    with a retryable status (429 or 5xx) are retried; anything else propagates
    immediately.
    """
    delay = base_delay
    for attempt in range(attempts):
        try:
            return await asyncio.to_thread(call)
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            status = int(status) if status is not None else None
            if status in _RETRYABLE_STATUS and attempt < attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable: retry loop exited without return or raise")
