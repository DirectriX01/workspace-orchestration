# Workspace Orchestrator

An agentic backend that turns one natural-language sentence, such as *"Cancel my Turkish Airlines flight"*, *"What's on my calendar next week?"*, or *"Prepare me for tomorrow's meeting with Acme"*, into a classified intent, a dependency-aware execution plan across Gmail/Calendar/Drive, a wave-by-wave orchestrated run with fallbacks and confirmation gates, and a grounded natural-language answer. All orchestration (intent routing, planning, DAG execution, hybrid retrieval) is hand-built on top of FastAPI, Postgres/pgvector, Redis, and Celery, with no agent framework (LangChain, LlamaIndex, CrewAI, etc.) used anywhere in the pipeline.

The service runs two ways: against real Gmail/Calendar/Drive via Google OAuth, or fully offline against a bundled mock Google backend and deterministic fake LLM/embedding providers, so it can be graded without any API keys or Google account.

## Architecture

```
                              POST /api/v1/query
                                     |
                                     v
                          +----------------------+
                          |     QueryPipeline     |   app/core/pipeline.py
                          +----------------------+
                                     |
                 +-------------------+--------------------+
                 |                   |                     |
                 v                   v                     v
        1. IntentClassifier   2. resolve_temporal    (confirm_action /
        (LLM structured out)   (pure code, no LLM)    clarification_reply
        app/core/intent.py     app/core/temporal.py    short-circuit here)
                 |                   |
                 +---------+---------+
                           v
                  3. QueryPlanner                        app/core/planner.py
                  deterministic PLAN_TEMPLATES per intent, or a validated
                  LLM plan for complex_multi_service (mutations from the
                  LLM path are always forced requires_confirmation)
                           |
                           v
                  4. DAGExecutor                              app/core/dag.py
                  wave-based topological asyncio.gather:
                    - resolves {{step.path}} templates from upstream results
                    - runs each wave's ready steps concurrently
                    - fallback step runs inline on empty/failed primary
                    - optional steps never block their dependents
                    - expect_single step with >1 result -> "ambiguous"
                    - requires_confirmation -> "pending_confirmation" (not run)
                    - emits step "running"/status -> Redis pub/sub -> WS
                           |
                 +---------+----------+-----------+
                 v                    v            v
           GmailAgent           CalendarAgent   DriveAgent      app/agents/*
           searches hit          (+ overlap      (search / get /
           pgvector via           conflict        share / move /
           HybridSearcher;        check on        create_folder)
           writes go to           create/update)
           mock or Google
           client + a
           write-through
           cache update
                 |                    |            |
                 +---------+----------+-----------+
                           v
                  5. ResponseSynthesizer                app/core/synthesizer.py
                  grounds the answer ONLY in step-result digests;
                  asks for confirmation when a pending action exists
                           |
                           v
                    QueryResponse JSON
              (answer, plan, results, pending_action,
               needs_clarification, conversation_id)

     Side channel: ConversationStore (Redis) keeps the last 5 turns and any
     single pending action per conversation, scoped to the user, so a later
     "yes, send it" / "the one with John" resolves correctly.
```

Sync path (independent of the query path):

```
POST /api/v1/sync/trigger?inline=true   ->  runs sync_gmail/sync_calendar/sync_drive
                                             in-process (thread pool)
POST /api/v1/sync/trigger               ->  dispatches the same three tasks as a
                                             Celery group via Redis broker
each sync task: fetch since watermark (SyncState) -> embed -> upsert into
                gmail_cache / gcal_cache / gdrive_cache (pgvector)
```

## Feature checklist

| Assignment capability | Where it lives |
|---|---|
| Intent classification (structured LLM output) | `app/core/intent.py`: 12-way intent taxonomy, entity extraction, temporal-phrase-verbatim rule, clarification/confirmation rules |
| DAG orchestration, parallel, sequential, fallback | `app/core/dag.py`: wave-based `asyncio.gather`, `depends_on`, per-step `fallback`, `optional` steps that don't block dependents |
| Hybrid pgvector search | `app/search/hybrid.py`: `score = 0.8*cosine + 0.2*exp(-age/90d)`, metadata filters (from/label/date/attendee/mime/owner), pure-metadata queries skip embedding entirely |
| Response synthesis | `app/core/synthesizer.py`: grounds answers strictly in step-result digests, never invents data, explicitly asks for confirmation when a pending action exists |
| Conversation context + pending-action confirm flow | `app/core/context.py` (Redis, last 5 turns + 1 pending action, 30 min TTL) + `QueryPipeline._handle_confirmation`: a mutation never runs until a later "yes, send it" turn |
| Ambiguity clarification | Intent classifier sets `needs_clarification` for underspecified mutations (e.g. "Move the meeting with John"); DAG also downgrades multi-row `expect_single` steps to `"ambiguous"` |
| Temporal reasoning | `app/core/temporal.py`: resolves "next week"/"this weekend"/"in 3 days"/explicit dates to a tz-aware `TimeRange`, independent of the LLM |
| Conflict detection | `app/agents/calendar_agent.py`: `create_event`/`update_event` check cached events for overlap before writing and return `status: "conflict"` instead of double-booking |
| WebSocket step streaming | `GET /api/v1/ws/{conversation_id}`: forwards `{"type": "step_update", "step": ..., "status": ...}` events published by the executor over Redis pub/sub |
| Celery background sync | `app/sync/tasks.py` + `app/sync/celery_app.py`: per-service `SyncState` watermark cursors, `sync_all` group fan-out, also runnable inline without a worker |
| Mock mode (no Google/OpenAI account needed) | `MOCK_GOOGLE=true` (18 emails / 12 events / 10 files fixtures in `app/services/mock/fixtures/`) + `LLM_PROVIDER=fake` / `EMBEDDINGS_PROVIDER=fake` |

## Quickstart

### 1. Docker Compose (recommended)

```bash
cp .env.example .env
# edit .env and set OPENAI_API_KEY for real LLM/embeddings, or leave it and
# use fully offline mode instead (see below); either way MOCK_GOOGLE=true
# needs no Google account.

docker compose up --build
```

This starts Postgres+pgvector (`localhost:5433`), Redis (`localhost:6380`), the API (`localhost:8000`, runs `alembic upgrade head` on boot), and a Celery worker. Health check: `curl localhost:8000/healthz`. FastAPI's interactive docs are at `localhost:8000/docs`.

**Fully offline, no API key at all:**

```bash
LLM_PROVIDER=fake EMBEDDINGS_PROVIDER=fake docker compose up --build
```

Intent classification, planning fallback, and synthesis become deterministic rule-based/templated output instead of real LLM calls. This is exactly what the test suite runs on, and it is what confirms the orchestration logic is correct independent of any model.

### 2. Local dev (no Docker for the app itself)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

docker compose up db redis          # just the two stateful services
cp .env.example .env                # defaults already point at :5433 / :6380

alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

### 3. Seed data and run the showcase queries

Either trigger an inline sync over HTTP:

```bash
curl -X POST "localhost:8000/api/v1/sync/trigger?inline=true" \
  -H "X-User-Email: demo@example.com"
```

or run the standalone seed script (creates the demo user, syncs all three services with `full=True`, prints row counts and one sample hybrid search):

```bash
EMBEDDINGS_PROVIDER=fake MOCK_GOOGLE=true python scripts/seed_and_sync.py
```

Requests are attributed via the `X-User-Email` header; omit it and requests fall back to the configured demo user (`demo@example.com`).

**Calendar search:**

```bash
curl -X POST localhost:8000/api/v1/query \
  -H "Content-Type: application/json" \
  -H "X-User-Email: demo@example.com" \
  -d '{"query": "What is on my calendar next week?"}'
```

**Flight cancellation -> pending confirmation -> "yes, send it":**

```bash
# 1. Ask - this finds the Turkish Airlines booking (msg_001, PNR TK-ABC123),
#    optionally the matching calendar event, drafts a cancellation email, and
#    returns it as a pending_action awaiting confirmation. Save conversation_id.
curl -X POST localhost:8000/api/v1/query \
  -H "Content-Type: application/json" \
  -H "X-User-Email: demo@example.com" \
  -d '{"query": "Cancel my Turkish Airlines flight"}'

# 2. Confirm in the SAME conversation -> the drafted email actually sends.
curl -X POST localhost:8000/api/v1/query \
  -H "Content-Type: application/json" \
  -H "X-User-Email: demo@example.com" \
  -d '{"query": "yes, send it", "conversation_id": "<conversation_id from step 1>"}'
```

**Ambiguous mutation -> clarification (no plan executed):**

```bash
curl -X POST localhost:8000/api/v1/query \
  -H "Content-Type: application/json" \
  -H "X-User-Email: demo@example.com" \
  -d '{"query": "Move the meeting with John"}'
# -> needs_clarification: true, plan: [] (there are two "Sync with John" events
#    in the mock fixtures (evt_005/evt_006), so the query is genuinely ambiguous)
```

All three flows above were run against the local stack while writing this doc and behave as shown. Note: with `LLM_PROVIDER=fake` the `answer` field is a deterministic digest string rather than fluent prose (that's the point: it makes the pipeline's correctness testable without an API key); set `OPENAI_API_KEY` and `LLM_PROVIDER=openai` for natural-language answers. Likewise `EMBEDDINGS_PROVIDER=fake` produces deterministic pseudo-random vectors, so semantic ranking in fully offline mode is structurally correct but not semantically meaningful; set `EMBEDDINGS_PROVIDER=openai` for real ranking quality.

**Watch step-by-step execution over the WebSocket** (in another terminal, before or while issuing a query with a known `conversation_id`):

```bash
# any ws client works, e.g. websocat:
websocat ws://localhost:8000/api/v1/ws/<conversation_id>
```

## Testing

```bash
pytest
```

91 tests, all green against the local stack. Unit tests (`tests/unit/`) cover the DAG executor, intent classifier, planner, and temporal resolver in isolation and need no external services. Integration tests (`tests/integration/`) drive the full ASGI app end to end (hybrid search, the full query pipeline including the confirm/clarify flows shown above) and require Postgres on `:5433` and Redis on `:6380`. `tests/conftest.py` force-pins `LLM_PROVIDER=fake` / `EMBEDDINGS_PROVIDER=fake` / `MOCK_GOOGLE=true` before importing `app`, so the suite is fully deterministic and needs no OpenAI key or Google account.

## Evaluation

Retrieval quality for the hybrid search layer is measured by `run_eval.py` against a labeled query set (see `app/search/eval/`). Run it with:

```bash
OPENAI_API_KEY=sk-... EMBEDDINGS_PROVIDER=openai python -m app.search.eval.run_eval
```

Meaningful precision/recall numbers require real embeddings (`EMBEDDINGS_PROVIDER=openai`); the fake provider's pseudo-random vectors are only for functional testing, not for eval scoring. See `docs/eval_results.md` for the full per-query table of the last recorded run.

Latest recorded run (text-embedding-3-small, 15 labeled queries, k=5):

| Metric | Result |
| --- | ---: |
| Mean P@5 (capped) | **1.000** |
| Mean MRR | **1.000** |
| Cold search latency (incl. embedding round trip), p50 / p95 | 422 ms / 524 ms |
| Warm search latency (query-embedding cache hit), p50 / p95 | **7.8 ms / 11.9 ms** |

Each query is measured twice in one run: the cold pass pays the OpenAI embedding round trip, the warm pass hits the 24h Redis query-embedding cache, which is what repeated interactive traffic sees. Both beat the assignment's targets (P@5 > 0.8, warm search well under 500 ms).

## Repo layout

```
app/
  agents/            gmail / calendar / drive adapters over HybridSearcher + service clients
  api/               routes (query, sync, auth, ws), request/response schemas, deps
  core/              pipeline, intent classifier, planner, DAG executor, synthesizer,
                     temporal resolver, conversation context store
  db/                SQLAlchemy models + session factories (async + sync)
  llm/               LLM client protocol, OpenAI client, FakeLLM
  search/            embeddings service, hybrid pgvector searcher, embed-text builders
  services/          mock Google clients + fixtures, real Google API clients, factory
  sync/              Celery app + per-service sync tasks
migrations/          Alembic migrations (0001_initial: users, conversations,
                     gmail/gcal/gdrive_cache with ivfflat cosine indexes, sync_state)
scripts/             seed_and_sync.py: seed demo user + full sync + sample search
tests/
  unit/              DAG executor, intent classifier, planner, temporal resolver
  integration/       hybrid search + full query-pipeline end-to-end tests
docker-compose.yml   db (pgvector/pg16, :5433) + redis (:6380) + api (:8000) + worker
Dockerfile           single image shared by the api and worker services
```

## Further docs

- [`DESIGN.md`](DESIGN.md): design rationale and tradeoffs
- [`API.md`](API.md): endpoint reference
- [`docs/er_diagram.md`](docs/er_diagram.md): database schema diagram
- [`docs/sample_queries.md`](docs/sample_queries.md): more example queries and expected behavior
- [`docs/postman_collection.json`](docs/postman_collection.json): importable Postman collection
- [`docs/openapi.json`](docs/openapi.json): exported OpenAPI schema
- [`docs/eval_results.md`](docs/eval_results.md): hybrid search eval numbers

## macOS Celery note

Celery's default `prefork` pool is unreliable on macOS because of Objective-C fork safety in system frameworks pulled in transitively (crashes with `objc[...]: +[NSObject initialize] may have been in progress...`). If you run the worker directly on macOS (outside Docker) rather than via `docker compose up worker`:

```bash
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES celery -A app.sync.celery_app worker --loglevel=info --pool=solo
```

`--pool=solo` runs tasks sequentially in a single process, which is fine for this project's low task volume. Running the worker inside Docker (the default `docker compose up` path) avoids this class of issue entirely since the container is Linux, so it's the recommended path on macOS.

## Mock vs. real Google

By default `MOCK_GOOGLE=true` and every Gmail/Calendar/Drive call is served from the fixtures in `app/services/mock/fixtures/` (18 emails, 12 events, 10 files, including a demo user `demo@example.com`, a Turkish Airlines booking with PNR `TK-ABC123`, an Acme email thread, two identically-titled "Sync with John" events for testing ambiguity, and two overlapping events for testing conflict detection) via `app/services/mock/clients.py`. Mutations (send/create/update/delete) are applied in-memory against those fixtures for the lifetime of the process; nothing external is touched.

To run against real Google APIs instead:

1. Create a Google Cloud project and enable the Gmail, Calendar, and Drive APIs.
2. Create an OAuth 2.0 Client ID (Web application) and add `http://localhost:8000/api/v1/auth/google/callback` as an authorized redirect URI.
3. Add yourself as a test user under the OAuth consent screen (the app will stay in "Testing" status, which is fine for local use).
4. Set `MOCK_GOOGLE=false`, `GOOGLE_CLIENT_ID`, and `GOOGLE_CLIENT_SECRET` in `.env`.
5. Visit `GET /api/v1/auth/google` in a browser, complete consent; the callback stores the resulting access/refresh tokens on the matched (or newly created) `User` row by email.

Requested scopes: `gmail.modify`, `calendar`, `drive` (see `app/services/google/oauth.py`). With `MOCK_GOOGLE=false`, sync and agent writes go through `app/services/google/{gmail,calendar,drive}_client.py` against the real APIs instead of the fixtures.
