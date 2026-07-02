"""Search-relevance evaluation harness.

A standalone, offline-capable harness that measures the quality of
:class:`app.search.hybrid.HybridSearcher` against a fixed set of labelled
queries whose ground-truth answers are the mock fixture ids
(``app/services/mock/fixtures``).

Run it with::

    python -m app.search.eval.run_eval

See :mod:`app.search.eval.run_eval` for the metrics (P@5 raw + capped, MRR,
per-query latency) and the labelled query set in ``labeled_queries.json``.
"""
