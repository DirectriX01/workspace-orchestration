# API Reference

This is the HTTP/WS surface of the Workspace Orchestrator, verified against a
live run of the app (`LLM_PROVIDER=fake EMBEDDINGS_PROVIDER=fake
MOCK_GOOGLE=true`, local Postgres on `:5433`, local Redis on `:6380`). Every
request/response example below is a real captured transcript, not a
hand-written guess - see `app/api/routes/*.py` and `app/api/schemas.py` for
the source of truth.

All routes are mounted under the `/api/v1` prefix (`app/main.py`), except
`GET /healthz`, which is at the root.

## Auth model

There is no session/token auth. Every request is attributed to a user via the
`X-User-Email` header:

```
X-User-Email: demo@example.com
```

`app/api/deps.py::get_current_user` reads that header; if it is absent, the
request falls back to `settings.demo_user_email` (`demo@example.com` by
default). The user row is created on first sight (auto-provisioned, no
sign-up step) and committed immediately. This means **omitting the header is
valid** - it just means every unauthenticated caller shares the demo user's
data. All example curls below use a dedicated `X-User-Email: docs-demo@example.com`
so results are reproducible without colliding with any other user's cache.

---

## `POST /api/v1/query`

Runs one turn of natural language through the full pipeline: classify →
(confirm | clarify) → plan → execute the DAG → synthesize an answer.
(`app/api/routes/query.py`, `app/core/pipeline.py`)

**Headers**: `X-User-Email` (optional, see above), `Content-Type: application/json`

**Request body** (`QueryRequest`, `app/api/schemas.py`):

| field             | type          | required | notes                                                      |
|-------------------|---------------|----------|-------------------------------------------------------------|
| `query`           | string        | yes      | min length 1 - empty string is rejected with 422            |
| `conversation_id` | string \| null| no       | omit/`null` to start a new conversation; the response echoes back a fresh UUID4 in that case |

```json
{"query": "What's on my calendar next week?", "conversation_id": null}
```

**Response body** (`QueryResponse`, 200):

| field                 | type                | notes |
|-----------------------|---------------------|-------|
| `answer`              | string              | natural-language answer from `ResponseSynthesizer` |
| `conversation_id`     | string              | the id to pass on the next turn (a new UUID4 if none was supplied) |
| `intent`              | object              | the full `IntentResult` (`app/core/intent.py`), dumped via `model_dump()` |
| `plan`                | array of objects    | one entry per `PlanStep`, each annotated with its settled `status`/`latency_ms`/`error` (see shape below); `[]` for chitchat/confirm/clarification turns |
| `results`             | object              | `{step_id: <raw agent payload dict>}` for every step that actually ran |
| `needs_clarification` | boolean             | `true` only when the classifier flagged an ambiguous mutation |
| `pending_action`      | object \| null      | a deferred mutation awaiting a `"yes, send it"`-style confirmation turn, or `null` |

Each `plan[]` entry:

```json
{
  "id": "search_events",
  "agent": "calendar",
  "action": "search_events",
  "params": { "...": "resolved or template params, see below" },
  "depends_on": [],
  "optional": false,
  "requires_confirmation": false,
  "status": "ok",
  "latency_ms": 23,
  "error": null
}
```

`status` is one of `ok | empty | failed | skipped | ambiguous | conflict |
pending_confirmation | null` (`null` only if the step never ran because the
pipeline short-circuited before planning, i.e. `plan` is `[]`). `params`
shown here are the plan's *authored* params (may still contain unresolved
`{{step_id.path}}` templates for steps deep in the DAG); the actually-used
resolved values are reflected in `results[step_id]`.

### Example: single-service search (real captured transcript)

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" \
  -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "What'"'"'s on my calendar next week?"}'
```

```json
{
  "answer": "Here is what I found:\n- User query: What's on my calendar next week? ...",
  "conversation_id": "e0819530-0aeb-4507-868d-203d8ba5f6a9",
  "intent": {
    "intent": "calendar_search",
    "services": ["calendar"],
    "entities": { "person_names": [], "person_emails": [], "company": null,
                  "airline": null, "event_title": null, "file_hint": null,
                  "label": null, "topic": "..." },
    "temporal_phrase": "next week",
    "needs_clarification": false,
    "clarification_question": null,
    "references_prior_context": false
  },
  "plan": [
    {
      "id": "search_events",
      "agent": "calendar",
      "action": "search_events",
      "params": {
        "query": "...",
        "starts_after": "2026-07-06T00:00:00+05:30",
        "starts_before": "2026-07-12T23:59:59.999999+05:30"
      },
      "depends_on": [],
      "optional": false,
      "requires_confirmation": false,
      "status": "ok",
      "latency_ms": 23,
      "error": null
    }
  ],
  "results": {
    "search_events": {
      "status": "ok",
      "results": [
        { "id": "evt_004", "source": "gcal", "title": "API integration kickoff",
          "description": "...", "location": "Conference Room A",
          "organizer_email": "demo@example.com",
          "attendees": ["demo@example.com", "john@company.com"],
          "start": "2026-07-11T09:30:00+00:00", "end": "2026-07-11T10:30:00+00:00",
          "status": "confirmed", "score": 0.2265 }
      ]
    }
  },
  "needs_clarification": false,
  "pending_action": null
}
```

(`answer` and every `body_preview`/`content_preview` field are truncated
above for readability; full transcripts for 14 more scenarios - including the
multi-turn confirm flow and the WS event stream - are in
`docs/sample_queries.md`.)

### The `results` payload shape per agent action

`results[step_id]` is whatever the agent returned, unwrapped from its
`StepResult`. The two shapes you'll see:

* **Search** (`search_*`): `{"status": "ok"|"empty", "results": [<row>, ...]}`. Row
  shape depends on the service - see `app/search/hybrid.py::_serialize_gmail
  / _serialize_gcal / _serialize_gdrive` for the exact fields (id, subject/
  title/name, from_email/attendees/owner_email, received_at/start+end/
  modified_at, `score`). `score` is `0.8*cosine_similarity + 0.2*recency_decay`,
  rounded to 4 decimals, or `null` for a direct cache lookup (`get_*`) that
  bypassed the ranked query.
* **Mutation** (everything else, e.g. `draft_email`, `update_labels`,
  `create_event`): `{"status": "ok", ...service-specific fields}` - see
  `app/agents/*.py::execute`. **Note**: `BaseAgent._ok()` always stamps
  `status: "ok"` onto the payload, even when the underlying mock client
  reports something more specific internally (e.g. `MockGmailClient.
  send_message` returns `{"status": "sent", ...}` - by the time it reaches
  the API response, `status` reads `"ok"`; the fact that it was a send vs. a
  draft is only visible from the `action` field on the plan step).
  `create_event`/`update_event` can instead settle as `{"status": "conflict",
  "conflicts": [<overlapping event row>, ...]}` - see the conflict-detection
  demo in `sample_queries.md`.

### Pending-action confirm contract

Any step with `requires_confirmation: true` in the plan (every mutation
except `draft_email`, which is a harmless local write) never actually calls
the agent. It settles as `status: "pending_confirmation"` and the pipeline
promotes exactly one such action per turn into the top-level
`pending_action` field:

```json
"pending_action": {
  "description": "send the drafted cancellation email",
  "agent": "gmail",
  "action": "send_email",
  "params": { "to": ["..."], "subject": "...", "body": "..." }
}
```

`pending_action` is also stored server-side in Redis
(`app/core/context.py::ConversationStore`, key `u:{user_id}:conv:{cid}:pending`,
30-minute TTL) - it does **not** need to be replayed by the client. To
execute it, send a follow-up turn on the **same `conversation_id`** whose
text the intent classifier recognizes as an approval (`"yes, send it"`,
`"confirm"`, `"go ahead"`, `"do it"` under the real LLM; the fake classifier
matches `"confirm"`, `"yes send"`, `"go ahead"` after punctuation is
stripped):

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "yes, send it", "conversation_id": "98dbd8d1-596e-4b8b-9bce-d10f660a38a0"}'
```

On confirmation the pipeline runs the stored action for real, clears the
pending slot (`pending_action` becomes `null` in the response), and returns a
short synthesized confirmation in `answer`; `results` contains a single
`"confirmation"` key with the raw execution payload. **Confirming again with
nothing pending** does not error - it returns
`{"answer": "There is no pending action to confirm.", ..., "plan": [], "results": {}}`
(a hardcoded string, not LLM-generated - `app/core/pipeline.py::_handle_confirmation`).
Pending actions are scoped per-user (`ConversationStore(scope=str(user.id))`),
so a caller cannot fire another user's pending action even by guessing their
`conversation_id`.

### Clarification contract

When the classifier sets `needs_clarification: true` (only for an
underspecified *mutation*, e.g. "Move the meeting with John" - no target
time/meeting given), the pipeline does not plan or execute anything:

```json
{
  "answer": "Which meeting do you mean?",
  "plan": [],
  "results": {},
  "needs_clarification": true,
  "pending_action": null
}
```

Any prior pending action on that conversation is cleared, so a stale "yes"
from an earlier, unrelated turn can never fire once a clarification round has
started.

### Chitchat contract

An intent with no matching plan template (`chitchat`, and empty-plan
branches of `confirm_action`/`clarification_reply`) also short-circuits: `plan: []`,
`results: {}`, `needs_clarification: false`, and `answer` is a friendly,
LLM-generated capabilities blurb.

### Error responses

* **422 Unprocessable Content** - Pydantic validation, e.g. missing or
  empty `query`:

  ```json
  {"detail":[{"type":"missing","loc":["body","query"],"msg":"Field required","input":{}}]}
  ```
  ```json
  {"detail":[{"type":"string_too_short","loc":["body","query"],
              "msg":"String should have at least 1 character",
              "input":"","ctx":{"min_length":1}}]}
  ```
* A step failing inside the DAG (bad template reference, agent exception,
  DB error) does **not** surface as an HTTP error - the endpoint still
  returns 200 with that step's `status: "failed"` and an `error` string on
  the plan entry. The only way `/query` itself returns non-200 is a body
  validation failure or an unhandled exception in the pipeline plumbing
  (500).

---

## `POST /api/v1/sync/trigger`

Kicks off a full or incremental sync of the current user's Gmail/Calendar/
Drive into the local pgvector cache. (`app/api/routes/sync.py`)

**Headers**: `X-User-Email` (optional)

**Query params**: `inline` (bool, default `false`)

* `inline=true` - runs all three syncs synchronously, in-process (each via
  `asyncio.to_thread`), and returns only once all three have finished. This
  is what you want for local/offline demos and is what the integration test
  suite uses to seed data before every query test.
* `inline=false` (default) - dispatches a Celery `group` of the three sync
  tasks and returns immediately with task/group ids. **Requires a running
  Celery worker** (`celery -A app.sync.celery_app worker`) - see
  `docker-compose.yml`.

```bash
curl -s -X POST "http://localhost:8000/api/v1/sync/trigger?inline=true" \
  -H "X-User-Email: docs-demo@example.com"
```

Inline response (`SyncTriggerResponse`):

```json
{"mode": "inline", "task_ids": {"gmail": null, "calendar": null, "drive": null}}
```

Celery-dispatched response shape (`task_ids` populated with real Celery task
ids plus a `"group"` id) - not runnable in this environment since no worker
was started for these docs:

```json
{"mode": "celery", "task_ids": {"gmail": "<task-id>", "calendar": "<task-id>", "drive": "<task-id>", "group": "<group-id>"}}
```

## `GET /api/v1/sync/status`

Per-service sync bookkeeping for the current user (`SyncState` row, or a
synthesized `idle`/0-items default if the user has never synced that
service). (`app/api/routes/sync.py`)

```bash
curl -s http://localhost:8000/api/v1/sync/status -H "X-User-Email: docs-demo@example.com"
```

```json
{
  "statuses": [
    {"service": "gmail",    "status": "idle", "last_synced_at": "2026-07-02T17:16:59.985805+00:00", "items_synced": 18, "error": null},
    {"service": "calendar", "status": "idle", "last_synced_at": "2026-07-02T17:17:00.127786+00:00", "items_synced": 12, "error": null},
    {"service": "drive",    "status": "idle", "last_synced_at": "2026-07-02T17:17:00.158925+00:00", "items_synced": 10, "error": null}
  ]
}
```

`items_synced` counts **items fetched in the most recent run**, not a
cumulative cache size (an incremental/`full=false` sync with nothing new
since the watermark reports `0` even though the cache is fully populated).
`status` is `"running"` while a sync is in flight and `"error"` (with
`error` populated) if the last run raised.

---

## `GET /api/v1/auth/google`

Redirects to Google's OAuth consent screen. Only meaningful when
`MOCK_GOOGLE=false` and real Google API access is desired - the mock/demo
path never needs this. (`app/api/routes/auth.py`)

**400 Bad Request** - real, captured - when OAuth env vars aren't set (the
default in this offline/demo configuration):

```bash
curl -s -i http://localhost:8000/api/v1/auth/google
```

```
HTTP/1.1 400 Bad Request
content-type: application/json

{"detail":"Google OAuth not configured; set GOOGLE_CLIENT_ID/SECRET. Mock mode (MOCK_GOOGLE=true) needs no auth."}
```

When `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` **are** set, this instead
responds `307 Temporary Redirect` to Google's consent URL
(`app/services/google/oauth.py::build_auth_url`).

## `GET /api/v1/auth/google/callback`

The OAuth redirect target: exchanges `?code=...` for tokens, looks up the
authenticated Google account's email via the userinfo endpoint, and
upserts/creates the local `User` row with the resulting access/refresh
tokens.

**Query params**: `code` (string, required - Google's auth code)

```json
{"status": "authenticated", "email": "someone@gmail.com"}
```

Not exercised in this environment (would require a real Google OAuth app and
a live consent flow); documented from source
(`app/api/routes/auth.py::google_callback`).

---

## `GET /healthz`

Liveness probe, no prefix, no auth.

```bash
curl -s http://localhost:8000/healthz
```
```json
{"status": "ok"}
```

---

## `WS /api/v1/ws/{conversation_id}`

A raw WebSocket (no sub-protocol) that forwards every step-progress event
published for `conversation_id` while the socket is open
(`app/api/routes/ws.py`). It is a pure fan-out of Redis pub/sub channel
`conv:{conversation_id}:events` - it does not replay history, so **connect
before** issuing the `POST /api/v1/query` call whose progress you want to
watch.

**Message format** - one JSON text frame per event, always:

```json
{"type": "step_update", "step": "<plan step id>", "status": "<status>"}
```

`status` is `"running"` when a step starts, then exactly one more message
with its settled status (`ok | empty | failed | skipped | ambiguous |
conflict | pending_confirmation`) - every step in the plan gets exactly two
messages, in that order (`app/core/dag.py::DAGExecutor._emit`, wired to
`app/core/pipeline.py::QueryPipeline._make_event_publisher`).

### Real captured transcript

Connecting to `ws://localhost:8000/api/v1/ws/94f950af-a19c-4f7b-976c-ceba56ba5f91`
and then POSTing `"Prepare for tomorrow's meeting with Acme Corp"` with that
`conversation_id` (a 4-step `meeting_prep` plan: 3 independent root steps in
wave 1, `attendee_emails` depending on `find_meeting` in wave 2) produced,
in order:

```json
[
  {"type": "step_update", "step": "find_emails",  "status": "running"},
  {"type": "step_update", "step": "find_meeting", "status": "running"},
  {"type": "step_update", "step": "find_files",   "status": "running"},
  {"type": "step_update", "step": "find_meeting", "status": "ok"},
  {"type": "step_update", "step": "find_emails",  "status": "ok"},
  {"type": "step_update", "step": "find_files",   "status": "ok"},
  {"type": "step_update", "step": "attendee_emails", "status": "running"},
  {"type": "step_update", "step": "attendee_emails", "status": "ok"}
]
```

Note the interleaving within wave 1: all three `"running"` events fire
before any `"ok"` (they're launched together via `asyncio.gather`), but the
*order* of the three `"running"`s and of the three `"ok"`s is not
guaranteed - it reflects real concurrent scheduling, not step-list order.
Wave 2 (`attendee_emails`) only starts once wave 1 has fully settled.

### Best-effort delivery

Event publishing is fire-and-forget (`app/core/pipeline.py`: a Redis
`publish` failure is swallowed, never raised into the request). A client
that connects *after* a step has already run will simply miss that event:
there is no buffering or replay. The HTTP response body always has the
complete, authoritative final state regardless of what the socket saw.

---

## Fake vs. real provider differences (applies to every endpoint above)

Set via `LLM_PROVIDER` / `EMBEDDINGS_PROVIDER` / `MOCK_GOOGLE` (`app/config.py`).
All examples in this document were captured with all three in their fake/mock
mode. See `docs/sample_queries.md` for a detailed, per-query account of where
this changes behavior (query-text pollution, unreachable intents, meaningless
similarity ranking, no real context carry-over). The short version:

* **`MOCK_GOOGLE=true`** swaps the Google API clients for fixture-backed
  in-memory ones (`app/services/mock/clients.py`) - no network calls, no
  quota. Data lives in `app/services/mock/fixtures/*.json` and is loaded
  once per process (mutations like a sent draft persist for the process's
  lifetime but are lost on restart).
* **`EMBEDDINGS_PROVIDER=fake`** replaces `text-embedding-3-small` with a
  SHA256-seeded pseudo-random unit vector per exact input string
  (`app/search/embeddings.py::_fake_vector`). It is deterministic for a
  fixed input string, but carries **no real semantic signal** - cosine
  similarity between two fake vectors is effectively noise. In practice this
  means hybrid-search ranking under the fake provider is driven almost
  entirely by the 0.2-weighted recency term and by metadata filters
  (`from_email`, `label`, date ranges, `attendee`, `mime_type`, `owner`), not
  by real relevance. `docs/sample_queries.md` has a concrete example where
  this causes the "find my Turkish Airlines booking" step to rank an
  unrelated email above the actual booking confirmation.
* **`LLM_PROVIDER=fake`** replaces both the intent classifier and the
  `complex_multi_service` planner with `app/llm/fake.py::FakeLLM`, a small
  keyword-rule engine. It can only ever produce 8 of the 12 `IntentResult.intent`
  values - it never emits `complex_multi_service`, `email_action`,
  `drive_action`, or a plain (non-move/cancel) `calendar_action`, because no
  keyword rule targets them. `docs/sample_queries.md` shows how those four
  are demonstrated instead (driving `QueryPlanner`/`DAGExecutor` directly
  with a hand-constructed `IntentResult`, exactly mirroring what the real
  classifier would hand the pipeline). `FakeLLM.complete_text` (response
  synthesis) also never invents data, but is a fixed template
  (`"Here is what I found:\n- {truncated input}"`), not real prose.
