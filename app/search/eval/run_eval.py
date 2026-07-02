#!/usr/bin/env python3
"""Search-relevance evaluation for :class:`app.search.hybrid.HybridSearcher`.

Standalone runner (works via ``python -m app.search.eval.run_eval`` or as a
direct script). It:

1. bootstraps ``sys.path`` so ``import app...`` resolves when run directly;
2. forces ``MOCK_GOOGLE=true`` (ground truth is the mock fixture ids) and
   honours whatever ``EMBEDDINGS_PROVIDER`` is set in the environment;
3. ensures a dedicated eval user (``eval@example.com``) and full-syncs Gmail,
   Calendar and Drive into that user's pgvector cache by calling the three sync
   task functions directly (``full=True``);
4. runs every labelled query (``labeled_queries.json``) through the searcher at
   ``k=5``, timing each call and scoring it against the fixture ground truth;
5. prints a GitHub-markdown table (query, source, P@5, MRR, ms) plus aggregate
   means and p50/p95 latency, and writes the same report to
   ``docs/eval_results.md``.

Metrics per query:

* ``P@5 (raw)``    = hits / 5
* ``P@5 (capped)`` = hits / min(5, |relevant|)   (so a query with a single
  relevant doc can still score 1.0)
* ``MRR``          = 1 / rank of the first relevant hit in the top-5 (else 0)

When ``EMBEDDINGS_PROVIDER=fake`` the vectors are deterministic random noise, so
a LOUD banner is printed: relevance numbers are meaningless and only the
mechanics and latency are trustworthy. The process always exits 0.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# sys.path bootstrap: make ``import app...`` work when run as a direct script. #
# __file__ = app/search/eval/run_eval.py -> parents[3] is the project root.    #
# --------------------------------------------------------------------------- #
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# The eval is defined against the mock fixtures, so the ground-truth ids only
# make sense with the fixture-backed clients. Force mock Google before anything
# reads settings; do NOT touch EMBEDDINGS_PROVIDER (that knob is honoured).
os.environ["MOCK_GOOGLE"] = "true"

#: Dedicated, isolated user so eval rows never collide with the demo user.
EVAL_EMAIL = "eval@example.com"
#: Path to the labelled query set (co-located with this module).
_QUERIES_PATH = Path(__file__).with_name("labeled_queries.json")
#: Where the rendered markdown report is written.
_RESULTS_PATH = _PROJECT_ROOT / "docs" / "eval_results.md"
#: Top-k retrieved per query.
_K = 5


# --------------------------------------------------------------------------- #
# Metric helpers                                                              #
# --------------------------------------------------------------------------- #
def _score_query(returned_ids: list[str], relevant_ids: list[str]) -> dict[str, float]:
    """Compute P@5 (raw + capped) and MRR for one query's top-k result ids."""
    relevant = set(relevant_ids)
    top = returned_ids[:_K]
    hits = sum(1 for rid in top if rid in relevant)

    p5_raw = hits / float(_K)
    denom = min(_K, len(relevant)) or 1
    p5_capped = hits / float(denom)

    mrr = 0.0
    for rank, rid in enumerate(top, start=1):
        if rid in relevant:
            mrr = 1.0 / rank
            break

    return {"hits": float(hits), "p5_raw": p5_raw, "p5_capped": p5_capped, "mrr": mrr}


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (``pct`` in [0, 1]); 0.0 on empty input."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #
def _render_report(
    results: list[dict[str, Any]],
    provider: str,
    fake: bool,
    timestamp: str,
) -> str:
    """Build the full markdown report (header, table, aggregates)."""
    lines: list[str] = []
    lines.append("# Search relevance eval")
    lines.append("")
    lines.append(f"- Embeddings provider: `{provider}`")
    lines.append(f"- Generated: {timestamp}")
    lines.append(f"- User: `{EVAL_EMAIL}` | k={_K} | queries: {len(results)}")
    lines.append("")
    if fake:
        lines.append(
            "> **FAKE embeddings - relevance numbers are meaningless. "
            "Only the mechanics and latency are trustworthy.**"
        )
        lines.append("")

    lines.append("| Query | Source | P@5 (capped) | P@5 (raw) | MRR | ms |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: |")
    for row in results:
        lines.append(
            f"| {row['query']} | {row['source']} "
            f"| {row['p5_capped']:.2f} | {row['p5_raw']:.2f} "
            f"| {row['mrr']:.2f} | {row['ms']:.1f} |"
        )

    latencies = [row["ms"] for row in results]
    lines.append("")
    lines.append("## Aggregate")
    lines.append("")
    lines.append(f"- Mean P@5 (capped): {_mean([r['p5_capped'] for r in results]):.3f}")
    lines.append(f"- Mean P@5 (raw): {_mean([r['p5_raw'] for r in results]):.3f}")
    lines.append(f"- Mean MRR: {_mean([r['mrr'] for r in results]):.3f}")
    lines.append(f"- Latency p50: {_percentile(latencies, 0.50):.1f} ms")
    lines.append(f"- Latency p95: {_percentile(latencies, 0.95):.1f} ms")
    lines.append(f"- Latency mean: {_mean(latencies):.1f} ms")
    lines.append("")
    return "\n".join(lines)


def _print_banner(provider: str, fake: bool) -> None:
    """Print a provider banner; a LOUD one when embeddings are fake."""
    bar = "=" * 74
    print(bar)
    if fake:
        print("  FAKE embeddings - relevance numbers meaningless, "
              "latency/mechanics only")
    else:
        print(f"  Embeddings provider: {provider}")
    print(bar)


# --------------------------------------------------------------------------- #
# Setup + execution                                                           #
# --------------------------------------------------------------------------- #
async def _ensure_user() -> uuid.UUID:
    """Return the eval user's id, creating the row on first run."""
    from sqlalchemy import select

    from app.db.models import User
    from app.db.session import get_async_session_factory

    factory = get_async_session_factory()
    async with factory() as session:
        user = (
            await session.execute(select(User).where(User.email == EVAL_EMAIL))
        ).scalar_one_or_none()
        if user is None:
            user = User(email=EVAL_EMAIL, timezone="Asia/Kolkata")
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user.id


def _clear_query_embedding_cache() -> None:
    """Drop cached query embeddings so a provider switch cannot leak vectors.

    The sync path caches embeddings in Redis keyed only by ``model:text`` (not
    by provider). Clearing the ``emb:q:*`` namespace forces this run's sync to
    recompute vectors with the currently-selected provider. Best-effort: any
    failure is non-fatal (the eval still runs, just without the guard).
    """
    try:
        import redis as sync_redis

        from app.config import get_settings
        from app.search.embeddings import CACHE_PREFIX

        client = sync_redis.from_url(get_settings().redis_url)
        keys = list(client.scan_iter(match=f"{CACHE_PREFIX}*"))
        if keys:
            client.delete(*keys)
        client.close()
        print(f"[setup] cleared {len(keys)} cached query embedding(s)")
    except Exception as exc:  # noqa: BLE001 - cache clearing is best-effort
        print(f"[setup] skipped embedding-cache clear ({exc})")


async def _sync_all(user_id: str) -> None:
    """Full-sync all three services for ``user_id`` (fixtures -> pgvector cache).

    The task functions internally call ``asyncio.run``, which would explode on a
    live event loop, so each is dispatched to a worker thread.
    """
    from app.sync.tasks import sync_calendar, sync_drive, sync_gmail

    gmail = await asyncio.to_thread(sync_gmail, user_id, full=True)
    calendar = await asyncio.to_thread(sync_calendar, user_id, full=True)
    drive = await asyncio.to_thread(sync_drive, user_id, full=True)
    print(f"[setup] synced gmail={gmail} calendar={calendar} drive={drive} items")


async def _run_queries(user_id: uuid.UUID, queries: list[dict]) -> list[dict[str, Any]]:
    """Run each labelled query through the searcher and score it."""
    from app.db.session import get_async_session_factory
    from app.search.embeddings import EmbeddingService
    from app.search.hybrid import HybridSearcher

    # No query-side Redis cache: keeps latency honest and avoids reading a
    # vector cached under a different provider for the same model:text key.
    factory = get_async_session_factory()
    results: list[dict[str, Any]] = []
    async with factory() as session:
        searcher = HybridSearcher(session, EmbeddingService(redis_async=None))
        method = {
            "gmail": searcher.search_gmail,
            "gcal": searcher.search_gcal,
            "gdrive": searcher.search_gdrive,
        }
        for entry in queries:
            source = entry["source"]
            search = method.get(source)
            if search is None:
                raise ValueError(f"unknown source in labeled_queries.json: {source!r}")
            params = {"query": entry["query"], "k": _K, **(entry.get("filters") or {})}

            started = time.perf_counter()
            rows = await search(user_id, params)
            elapsed_ms = (time.perf_counter() - started) * 1000.0

            returned_ids = [row["id"] for row in rows]
            scored = _score_query(returned_ids, entry["relevant_ids"])
            results.append(
                {
                    "query": entry["query"],
                    "source": source,
                    "ms": elapsed_ms,
                    "returned_ids": returned_ids,
                    **scored,
                }
            )
    return results


async def _amain() -> None:
    from app.config import get_settings

    # A fresh process reads the current env on first access, but clear the cache
    # defensively in case something imported settings during module import.
    get_settings.cache_clear()
    settings = get_settings()
    provider = settings.embeddings_provider
    fake = provider == "fake"

    _print_banner(provider, fake)
    print(f"[setup] mock_google={settings.mock_google} db={settings.database_url}")

    queries = json.loads(_QUERIES_PATH.read_text(encoding="utf-8"))
    print(f"[setup] loaded {len(queries)} labelled queries from {_QUERIES_PATH.name}")

    _clear_query_embedding_cache()

    user_id = await _ensure_user()
    await _sync_all(str(user_id))

    results = await _run_queries(user_id, queries)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    report = _render_report(results, provider, fake, timestamp)

    print()
    print(report)

    _RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _RESULTS_PATH.write_text(report, encoding="utf-8")
    print(f"[done] wrote report to {_RESULTS_PATH}")

    if fake:
        print()
        _print_banner(provider, fake)

    # Dispose the async engine so we don't emit an unclosed-pool warning.
    from app.db.session import get_async_engine

    await get_async_engine().dispose()


def main() -> None:
    """Entry point: run the eval, always exit 0 (print any failure loudly)."""
    try:
        asyncio.run(_amain())
    except Exception:  # noqa: BLE001 - report but never fail the process
        print("\n[error] eval run failed:", file=sys.stderr)
        traceback.print_exc()
    sys.exit(0)


if __name__ == "__main__":
    main()
