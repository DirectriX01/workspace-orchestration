"""Embedding generation with Redis caching.

:class:`EmbeddingService` exposes both async methods (``embed`` /
``embed_batch``, used by the FastAPI layer) and sync methods (``embed_sync`` /
``embed_batch_sync``, used by Celery workers). Both share the same cache format
and fake-provider logic.

Embeddings come from OpenAI (``embeddings_provider == "openai"``) in batches of
100, or from a deterministic stdlib pseudo-random generator when the provider
is ``"fake"`` (used in tests without network access).

Cached vectors are stored as little/native-endian float32 bytes under the key
``emb:q:{sha256(model + ":" + text)}`` with a 24h TTL. Redis clients passed in
MUST be binary-safe (``decode_responses=False``) since values are raw bytes.
"""

from __future__ import annotations

import hashlib
import math
import random
from array import array
from collections.abc import Iterator, Sequence
from typing import Any

from openai import AsyncOpenAI, OpenAI

from app.config import get_settings

#: Cache TTL in seconds (24 hours).
CACHE_TTL = 86400
#: Redis key prefix for cached query embeddings.
CACHE_PREFIX = "emb:q:"
#: Maximum number of inputs per OpenAI embeddings request.
_BATCH_SIZE = 100


class EmbeddingService:
    """Produce (and cache) text embeddings for API and worker code paths.

    Args:
        redis_async: Optional ``redis.asyncio`` client used by the async
            methods. ``None`` disables caching on the async path.
        redis_sync: Optional synchronous ``redis`` client used by the sync
            methods. ``None`` disables caching on the sync path.
    """

    def __init__(self, redis_async: Any | None = None, redis_sync: Any | None = None) -> None:
        settings = get_settings()
        self._provider = settings.embeddings_provider
        self._model = settings.embedding_model
        self._dim = settings.embedding_dim
        self._api_key = settings.openai_api_key
        self._redis_async = redis_async
        self._redis_sync = redis_sync
        self._async_client: AsyncOpenAI | None = None
        self._sync_client: OpenAI | None = None

    # ------------------------------------------------------------------ #
    # Async API                                                          #
    # ------------------------------------------------------------------ #
    async def embed(self, text: str) -> list[float]:
        """Embed a single string (async)."""
        (vector,) = await self.embed_batch([text])
        return vector

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of strings (async), preserving input order."""
        if not texts:
            return []
        keys = [self._cache_key(text) for text in texts]
        results: list[list[float] | None] = [None] * len(texts)

        if self._redis_async is not None:
            cached = await self._redis_async.mget(keys)
            for index, raw in enumerate(cached):
                if raw:
                    results[index] = self._decode(raw)

        miss_indices = [i for i, value in enumerate(results) if value is None]
        if miss_indices:
            miss_texts = [texts[i] for i in miss_indices]
            vectors = await self._embed_raw_async(miss_texts)
            for offset, index in enumerate(miss_indices):
                results[index] = vectors[offset]
            if self._redis_async is not None:
                for index in miss_indices:
                    await self._redis_async.set(
                        keys[index],
                        self._encode(results[index]),  # type: ignore[arg-type]
                        ex=CACHE_TTL,
                    )

        return [self._finalize(vector) for vector in results]

    async def _embed_raw_async(self, texts: Sequence[str]) -> list[list[float]]:
        """Compute embeddings for cache-miss texts (async, no caching)."""
        if self._provider == "fake":
            return [self._fake_vector(text) for text in texts]
        client = self._get_async_client()
        out: list[list[float]] = []
        for chunk in self._chunks(texts, _BATCH_SIZE):
            response = await client.embeddings.create(model=self._model, input=list(chunk))
            out.extend(item.embedding for item in sorted(response.data, key=lambda d: d.index))
        return out

    # ------------------------------------------------------------------ #
    # Sync API                                                           #
    # ------------------------------------------------------------------ #
    def embed_sync(self, text: str) -> list[float]:
        """Embed a single string (sync)."""
        (vector,) = self.embed_batch_sync([text])
        return vector

    def embed_batch_sync(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of strings (sync), preserving input order."""
        if not texts:
            return []
        keys = [self._cache_key(text) for text in texts]
        results: list[list[float] | None] = [None] * len(texts)

        if self._redis_sync is not None:
            cached = self._redis_sync.mget(keys)
            for index, raw in enumerate(cached):
                if raw:
                    results[index] = self._decode(raw)

        miss_indices = [i for i, value in enumerate(results) if value is None]
        if miss_indices:
            miss_texts = [texts[i] for i in miss_indices]
            vectors = self._embed_raw_sync(miss_texts)
            for offset, index in enumerate(miss_indices):
                results[index] = vectors[offset]
            if self._redis_sync is not None:
                for index in miss_indices:
                    self._redis_sync.set(
                        keys[index],
                        self._encode(results[index]),  # type: ignore[arg-type]
                        ex=CACHE_TTL,
                    )

        return [self._finalize(vector) for vector in results]

    def _embed_raw_sync(self, texts: Sequence[str]) -> list[list[float]]:
        """Compute embeddings for cache-miss texts (sync, no caching)."""
        if self._provider == "fake":
            return [self._fake_vector(text) for text in texts]
        client = self._get_sync_client()
        out: list[list[float]] = []
        for chunk in self._chunks(texts, _BATCH_SIZE):
            response = client.embeddings.create(model=self._model, input=list(chunk))
            out.extend(item.embedding for item in sorted(response.data, key=lambda d: d.index))
        return out

    # ------------------------------------------------------------------ #
    # Shared helpers                                                     #
    # ------------------------------------------------------------------ #
    def _get_async_client(self) -> AsyncOpenAI:
        if self._async_client is None:
            self._async_client = AsyncOpenAI(api_key=self._api_key)
        return self._async_client

    def _get_sync_client(self) -> OpenAI:
        if self._sync_client is None:
            self._sync_client = OpenAI(api_key=self._api_key)
        return self._sync_client

    def _cache_key(self, text: str) -> str:
        # Provider is part of the key: fake and real vectors must never
        # share cache entries for the same model:text pair.
        digest = hashlib.sha256(
            f"{self._provider}:{self._model}:{text}".encode()
        ).hexdigest()
        return f"{CACHE_PREFIX}{digest}"

    def _fake_vector(self, text: str) -> list[float]:
        """Deterministic unit-length pseudo-embedding seeded from ``text``."""
        seed = int(hashlib.sha256(text.encode()).hexdigest(), 16)
        rng = random.Random(seed)
        vector = [rng.gauss(0.0, 1.0) for _ in range(self._dim)]
        norm = math.sqrt(sum(component * component for component in vector))
        if norm == 0.0:
            vector[0] = 1.0
            norm = 1.0
        return [component / norm for component in vector]

    def _finalize(self, vector: list[float] | None) -> list[float]:
        """Assert the dimension contract on every return path."""
        assert vector is not None, "embedding was never resolved"
        assert len(vector) == self._dim, (
            f"embedding dim mismatch: {len(vector)} != {self._dim}"
        )
        return vector

    @staticmethod
    def _encode(vector: list[float]) -> bytes:
        return array("f", vector).tobytes()

    @staticmethod
    def _decode(raw: bytes) -> list[float]:
        buffer = array("f")
        buffer.frombytes(raw)
        return buffer.tolist()

    @staticmethod
    def _chunks(seq: Sequence[str], size: int) -> Iterator[Sequence[str]]:
        for start in range(0, len(seq), size):
            yield seq[start : start + size]
