# Sample Queries

14 scenarios, each run for real against the local stack (`LLM_PROVIDER=fake
EMBEDDINGS_PROVIDER=fake MOCK_GOOGLE=true`, Postgres `:5433`, Redis `:6380`),
seeded via `POST /api/v1/sync/trigger?inline=true` for a dedicated user
`docs-demo@example.com`. "Now" for every transcript below is
**2026-07-02T22:5x:xx+05:30 (Asia/Kolkata)**, a Thursday - dates in the
resolved plans/results are relative to that. Four of the fourteen scenarios
(12–14, plus a bonus on #6) can't be reached through the HTTP API under the
fake LLM at all; those are driven directly through `QueryPlanner` +
`DAGExecutor` with a hand-built `IntentResult`, and it's explained why for
each. Every JSON block below is copy-pasted from a real run, trimmed for
length where noted.

Where a step's `params.query` shows `"..."`, the real value was the entire
assembled classifier prompt (conversation history + timezone + now + the
literal query) - see the standing note at the bottom of this file; it's
called out again inline wherever it changes behavior.

---

## 1. Single-service - calendar search ("next week")

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "What'"'"'s on my calendar next week?"}'
```

**Plan shape**: 1 step, no parallelism - `search_events` (agent `calendar`),
`depends_on: []`.

`PLAN_TEMPLATES["calendar_search"]` (`app/core/planner.py::_build_calendar_search`)
turns `temporal_phrase="next week"` into `starts_after`/`starts_before` via
`resolve_temporal`. Resolved window (Monday–Sunday of the calendar week
starting 4 days after "now"): `2026-07-06T00:00:00+05:30` →
`2026-07-12T23:59:59.999999+05:30`.

**Expected answer sketch**: a short list of what's on the calendar in that
window, grounded in the search rows (titles + dates), since 7 of the 12
fixture events fall inside it.

**Real result**: `status: "ok"`, 7 events returned (`evt_004`, `evt_010`,
`evt_012`, `evt_008`, `evt_007`, `evt_003`, `evt_006`), each with the usual
`id/title/description/location/organizer_email/attendees/start/end/status/score`
fields.

**Fake vs real**: `intent.entities.topic` (which becomes `params.query`) is
polluted with the whole classifier prompt rather than just "next week" (see
the standing note). It doesn't change the *filtered* set here (the date
range does the real work), but it does mean the semantic-ranking component
of `score` is meaningless - see #4 for a case where that matters. Under the
real classifier, `topic` would be `null`/omitted and `query` wouldn't be set
at all for a bare "what's on my calendar" ask.

---

## 2. Single-service - email search ("from X about the budget")

Two variants, both run for real, because the assignment's literal example
domain doesn't exist in the fixtures (`app/services/mock/fixtures/emails.json`
only has `sarah@acmecorp.com`, never `sarah@company.com`).

### 2a. Assignment's literal query

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "Find emails from sarah@company.com about the budget"}'
```

**Plan shape**: 1 step, `search_emails` (agent `gmail`),
`params: {"query": "...", "from_email": "sarah@company.com"}`.

**Real result**: `status: "empty"`, `"results": []`. `from_email` is an
`ILIKE '%sarah@company.com%'` filter (`app/search/hybrid.py::search_gmail`)
against real fixture data, and no fixture email is from that domain, so this
is correctly, deterministically empty - not a bug, just a mismatch between
the example prompt and the seeded data.

### 2b. Fixture-accurate query

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "Find emails from sarah@acmecorp.com about the budget"}'
```

**Real result**: `status: "ok"`, 3 rows - `msg_005` ("Agenda for tomorrow's
Quarterly Review"), `msg_004` ("Re: Acme Corp partnership - Q3 proposal"),
`msg_003` ("Excited to kick off our partnership") - i.e. every email from
`sarah@acmecorp.com` in the fixture set (there's no separate `label`/date
filter here to narrow further, so all 3 come back, ranked by the blended
score).

**Expected answer sketch**: names the 3 matching threads by subject and
date, grounded in `results.search_emails.results`.

**Fake vs real**: `entities.person_emails` extraction (a plain regex over
the whole prompt) works identically under fake and real for either variant:
this is one of the few entity fields FakeLLM gets exactly right. The
divergence is entirely about the *fixture data*, not the provider: use 2b
when demoing against this repo's mock backend; 2a is realistic against a
real Gmail account where a `sarah@company.com` might actually exist.

---

## 3. Single-service - Drive search ("PDFs ... last month")

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "Show me the PDFs I got last month"}'
```

**Plan shape**: 1 step, `search_files` (agent `drive`),
`params: {"query": "...", "modified_after": "2026-06-01T00:00:00+05:30", "modified_before": "2026-06-30T23:59:59.999999+05:30"}`.

**Real result**: `status: "ok"`, 5 rows (the default `k`), sorted by blended
score: `file_003` (Q3 Budget.xlsx), `file_001` (Acme Corp Proposal v3.docx),
`file_005` (Istanbul trip itinerary.pdf), `file_002` (Acme Meeting Notes
2026-06.docx), `file_004` (Out of Office - July.docx).

**This is not actually filtered to PDFs.** Only 1 of the 5 rows returned
(`file_005`) is an actual PDF. This is a real, verified fake-mode gap, not a
docs error: `_build_drive_search` only adds a `mime_type` filter when
`intent.entities.file_hint` is set
(`app/core/planner.py::_mime_for_hint(intent.entities.file_hint)`), and
`FakeLLM._fake_intent` **never populates `file_hint`** - it only ever sets
`topic` and `person_emails` (see `app/llm/fake.py::_fake_intent`, which
constructs `Entities(person_emails=emails, topic=query)` and nothing else).
So under the fake classifier, "PDFs" is just inert text inside the
(polluted) `query` string; the search is really "everything modified last
month". **Under the real OpenAI classifier**, system-prompt rule 7 ("extract
a file description into `file_hint`") would populate `file_hint="PDF"`,
`_mime_for_hint` would map it to `application/pdf`, and the query would
correctly filter to just `file_005` (the only PDF modified in the resolved
June 2026 window; `file_008`, the other PDF, was modified in May and falls
outside it).

**Expected answer sketch (fake mode, accurately)**: lists 5 files modified
last month, most of which are not PDFs - a synthesizer answer that's honest
about the step results would not claim these are "the PDFs", since the
underlying data doesn't support that filter; this is a good illustration of
why grounding the answer strictly in step results (as `_SYNTHESIS_SYSTEM`
instructs) matters when the *plan* itself is imperfect.

---

## 4. Multi-service - flight cancellation, with the confirm turn

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "Cancel my Turkish Airlines flight"}'
```

**Plan shape** - 3 steps in 2 waves (`app/core/planner.py::_build_flight_cancellation`):

* wave 1: `find_booking` (gmail `search_emails`, query `"Turkish Airlines
  flight booking confirmation"`, `k=5`, has a broader `find_booking_broad`
  fallback if it comes back empty)
* wave 2 (both depend only on `find_booking`, so they run **concurrently**):
  `find_flight_event` (calendar `search_events`, optional) and
  `draft_cancellation` (gmail `draft_email`, templated from
  `find_booking.top`)
* `pending_action_template`: `{"agent": "gmail", "action": "send_email",
  "params_from_step": "draft_cancellation"}` - this is what turns the drafted
  email into a `pending_action` in the response.

**Expected answer sketch**: identifies the booking, summarizes the drafted
cancellation email, and explicitly asks the user to confirm before sending
(`_SYNTHESIS_SYSTEM` requires this whenever `pending_action` is set).

**Real result - and a genuinely important fake-vs-real gotcha**: `find_booking`
came back `status: "ok"` with 5 rows, but the **top-ranked** row was
`msg_007` ("Re: API integration timeline", from `john@company.com`) - not
`msg_001`, the actual Turkish Airlines booking confirmation, which was
included but ranked **5th of 5**. The draft that got produced (and the
`pending_action` offered) was therefore:

```json
"pending_action": {
  "description": "send the drafted cancellation email",
  "agent": "gmail", "action": "send_email",
  "params": {
    "to": ["john@company.com"],
    "subject": "Cancellation request - Re: API integration timeline",
    "body": "Hello,\n\nI would like to cancel my reservation associated with \"Re: API integration timeline\". ..."
  }
}
```

This is exactly the fake-embeddings caveat from `API.md` made concrete:
`EMBEDDINGS_PROVIDER=fake` produces a SHA256-seeded pseudo-random unit vector
per input string with no real semantic content, so cosine similarity between
"Turkish Airlines flight booking confirmation" and the corpus is
approximately noise, and the 0.2-weighted recency term isn't enough to
correct for it here. **Under real embeddings** (`text-embedding-3-small`),
`msg_001` would correctly rank first and the draft would go to
`reservations@turkishairlines.com` referencing the real PNR (`TK-ABC123`).
This is a good demo of *why* the plan's `fallback` mechanism exists (a
completely empty primary triggers `find_booking_broad`) but also of its
limit - a *wrong but non-empty* top result never triggers the fallback,
because `fallback` only fires on `status in ("empty", "failed")`
(`app/core/dag.py::_execute_core`).

### Confirm turn (same `conversation_id`)

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "yes, send it", "conversation_id": "98dbd8d1-596e-4b8b-9bce-d10f660a38a0"}'
```

**Plan shape**: none - `confirm_action` always yields an empty plan
(`PLAN_TEMPLATES["confirm_action"] = _build_empty`); the pipeline branches
into `_handle_confirmation` before planning ever runs.

**Real result**: `pending_action: null`, `plan: []`,
`results: {"confirmation": {"id": "sent_001", "thread_id": "thr_sent_001",
"status": "ok", "to": ["john@company.com"], "subject": "Cancellation request
- Re: API integration timeline", ..., "labels": ["SENT"]}}`, and the answer
states the action was executed. Note `status: "ok"` here even though
`MockGmailClient.send_message` itself returns `{"status": "sent", ...}`
internally - `BaseAgent._ok()` always overwrites `status` to `"ok"` on the
way out (see `API.md`).

**Fake vs real**: the confirm keyword match itself is one of the more
robust FakeLLM rules - `"confirm" in squished or "yes send" in squished or
"go ahead" in squished` after punctuation-stripping, checked *before* any
other rule, so `"yes, send it"` reliably becomes `confirm_action` regardless
of provider.

---

## 5. Multi-service - meeting prep ("tomorrow's meeting with Acme Corp")

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "Prepare for tomorrow'"'"'s meeting with Acme Corp"}'
```

**Plan shape** - 4 steps in 2 waves, **all optional** (`_build_meeting_prep`):

* wave 1 (3 independent, run concurrently): `find_meeting` (calendar,
  `starts_after`/`starts_before` = tomorrow's full day since no explicit
  range was given), `find_emails` (gmail), `find_files` (drive) - all
  `query="Acme Corp"` (from `entities.company`, extracted by both FakeLLM's
  `_CORP_RE` regex and a real classifier)
* wave 2: `attendee_emails` (gmail), `depends_on: ["find_meeting"]`,
  `params.query = "{{find_meeting.top.attendees}}"`

This was independently confirmed live over the WebSocket (`ws://.../ws/{cid}`,
connected before the query was fired):

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

All 3 wave-1 `"running"` events precede any `"ok"`, exactly as
`asyncio.gather` would produce, and `attendee_emails` only starts once wave 1
fully settles.

**Real result**: `find_meeting` correctly found exactly `evt_002` ("Acme
Corp - Quarterly Review", 2026-07-03) - a clean single match here because
the date-window filter (not semantic luck) narrows it to the one fixture
event on that day. `find_emails` and `find_files` returned 5 rows each,
several genuinely Acme-relevant (`msg_006`, `msg_005`, file `file_004` "Out
of Office - July.docx", `file_001` "Acme Corp Proposal v3.docx") mixed with
clearly-irrelevant ones (a password-reset email, a GitHub notification):
again the fake-embeddings noise from #3/#4, just less visible here because
the top favorite (`evt_002`) didn't depend on semantic ranking at all.
`attendee_emails`'s query templated to `"{{find_meeting.top.attendees}}"`
resolved via `_query_text()`'s list-join special-case
(`app/search/hybrid.py::_query_text`) to `"demo@example.com sarah@acmecorp.com"`.

**Expected answer sketch**: names the Acme Corp Quarterly Review meeting
(time + attendees), and best-effort surfaces relevant emails/files, noting
that all four steps are optional (a partial result still produces a usable
answer if e.g. Drive were down).

**Fake vs real**: `entities.company="Acme Corp"` extraction is solid under
both providers (FakeLLM's `_CORP_RE` regex handles this specific pattern);
the divergence is again purely in ranking quality of `find_emails`/
`find_files`, not in plan shape or entity extraction.

---

## 6. Multi-service - "find events next week conflicting with my out-of-office doc"

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "Find events next week that conflict with my out-of-office doc"}'
```

**This never reaches `complex_multi_service` under the fake classifier.**
`FakeLLM._fake_intent`'s rule 5 (`app/llm/fake.py`) is
`any(kw in squished for kw in ("calendar", "schedule", "meeting", "event"))`,
and `"event"` is a **substring** of `"events"`, so this query is classified
as plain `calendar_search`, exactly like #1, before the classifier ever
considers anything more elaborate. Real transcript:

```json
{
  "intent": { "intent": "calendar_search", "services": ["calendar"],
              "temporal_phrase": "next week", "references_prior_context": true, "..." },
  "plan": [{ "id": "search_events", "agent": "calendar", "action": "search_events",
             "params": {"starts_after": "2026-07-06T00:00:00+05:30",
                         "starts_before": "2026-07-12T23:59:59.999999+05:30", "..." },
             "status": "ok" }]
}
```

(`references_prior_context: true` here is itself a minor false positive:
FakeLLM sets it whenever `"that "` appears anywhere in the prompt, and "next
week **that** conflict" trips it even though it's not a pronoun reference.)

There is no Drive step at all, and no actual conflict computation - "the
Drive file" and "conflict" are both silently ignored.

**Why this is a `complex_multi_service` case with the real classifier**:
none of `email_search / calendar_search / drive_search / meeting_prep /
flight_cancellation` covers "cross-reference a Drive document's date range
against calendar events" - there is no deterministic `PLAN_TEMPLATES` entry
that reads a Drive file's content to constrain a calendar search. Per the
system prompt's taxonomy, this is exactly what `complex_multi_service` is
for: "an open-ended request spanning services that no single template
covers." With a real OpenAI classification landing on
`intent="complex_multi_service"`, `QueryPlanner._plan_complex`
(`app/core/planner.py`) asks the LLM to decompose it into a DAG restricted to
the canonical action surface (`_CANONICAL_ACTIONS_DOC` - `gmail.*`,
`calendar.*`, `drive.*`, no bespoke "detect conflicts" action exists). A
plausible real plan: `find_ooo_doc` (`drive.search_files`, query "out of
office") → `find_events` (`calendar.search_events`, `starts_after`/
`starts_before` = the resolved "next week" range), with `find_events`
possibly `depends_on: ["find_ooo_doc"]` if the LLM chooses to read the OOO
doc's own stated date range rather than using "next week" verbatim (the
fixture's `file_004` literally says "away ... starting in 6 days through 8
days from now", which the synthesizer, not any code path, would have to
reconcile against `find_events`' rows in prose - there is no code-level
"overlap" check outside `CalendarAgent`'s create/update conflict detection,
which only runs on a mutation, never on a pair of search results). The exact
decomposition is genuinely LLM-dependent and not reproducible byte-for-byte;
what's guaranteed by validation
(`QueryPlanner._validate_llm_plan`) is that every step uses only
`KNOWN_ACTIONS`, every `depends_on` resolves, and any step whose action
isn't `search_*`/`get_*`/`draft_email` is forced `requires_confirmation=True`
- irrelevant here since nothing about this request should mutate anything.

**Direct proof of the FakeLLM's degenerate `complex_multi_service` behavior**
(driving `QueryPlanner.plan()` directly with a hand-built
`IntentResult(intent="complex_multi_service", ...)`, bypassing only the
classifier call): `QueryPlanner._plan_complex` calls
`self._llm.complete_structured(..., LLMPlanOutput)`, and
`FakeLLM._fake_plan` (`app/llm/fake.py`) **ignores its input entirely** and
always returns the same single step:

```json
[{"id": "search_emails", "agent": "gmail", "action": "search_emails",
  "params": {"query": "{\"entities\": {...}, \"temporal_phrase\": \"next week\", \"time_range\": {...}, \"now\": \"...\"}"},
  "optional": false, "requires_confirmation": false}]
```

Note that `params.query` is the *entire JSON envelope* the planner sent the LLM
(entities + temporal phrase + time range + now), not real text, since
`_fake_plan` just echoes a canned `LLMPlanStep` regardless of `query`. This
step passed `_validate_llm_plan` (single known action, no bad deps) and ran
for real, returning 5 gmail rows ranked by fake-embedding noise against that
JSON blob - a concrete illustration that **any** `complex_multi_service`
request degenerates to the identical single Gmail search under
`LLM_PROVIDER=fake`, regardless of what was actually asked.

---

## 7. Hard case - "Move the meeting with John" → clarification

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "Move the meeting with John"}'
```

**Plan shape**: none. `needs_clarification=true` short-circuits before
planning (`QueryPipeline.handle` checks `intent.needs_clarification` right
after classification).

**Real result**:

```json
{
  "answer": "Which meeting do you mean?",
  "intent": { "intent": "calendar_action", "needs_clarification": true,
              "clarification_question": "Which meeting do you mean?", "..." },
  "plan": [], "results": {}, "needs_clarification": true, "pending_action": null
}
```

Why it's ambiguous: there are **two** fixture events titled "Sync with John"
(`evt_005`, `evt_006`) plus no destination time was given - either would
independently justify clarification. `FakeLLM`'s rule for this
(`"move"/"reschedule"` + `"meeting"`, no quoted title) is a hardcoded
clarifying question, not a query against the data - it doesn't actually know
about the two John events; it just knows "no quoted event title" means
ambiguous by convention.

**Contrast, and a real, more fundamental finding**: `'Move the "Design
review" meeting to next Tuesday'` (a quoted title present) does skip the
FakeLLM clarification step and reaches `calendar_action` →
`_build_calendar_action`'s real two-step plan. It's tempting to assume
`find_target` then resolves cleanly, since "Design review" (`evt_007`) is
the only event with that title - but run live, it does not:

```json
{
  "id": "find_target", "agent": "calendar", "action": "search_events",
  "params": {"query": "Design review"},
  "status": "ambiguous"
}
```

`find_target`'s result contains 10 rows, and `evt_007` isn't even among the
first several shown. The reason is structural, not a fake-embeddings
ranking accident: `_build_calendar_action`'s `find_params` only ever sets
`query` (from `event_title`/`topic`) and, if present, `attendee` - **no
date-window filter** - so the SQL `WHERE` clause is just
`user_id = :uid`, matching essentially every cached event for the user
(here, more than the calendar's default `k=10`, so it's truncated to 10).
`expect_single`'s ambiguity check (`app/core/dag.py::_map_result`) is purely
`len(rows) > 1` - it does not look at whether the *top-ranked* row is a
strong match, only how many rows came back at all. That means **this would
be ambiguous under the real embeddings provider too**: better embeddings
would correctly rank `evt_007` first, but the search step still returns up
to `k=10` rows regardless of ranking confidence, and `expect_single` only
inspects the count. This particular query (no person named) never supplies
`entities.person_emails`, so `_build_calendar_action` never adds the
optional `attendee` filter at all - the `WHERE` clause is just `user_id =
:uid` and every cached event is a candidate. `attendee` *is* an exact
metadata filter and, for a person who only shows up on one event
(`sarah@acmecorp.com`, only on `evt_002`), it would narrow `find_target` to
a clean single match regardless of embeddings quality - but the fixture's
most commonly-referenced named person, `john@company.com`, still appears on
6 events, so the concrete "meeting with John" case in this section would
stay ambiguous **even with** an attendee filter applied. The underlying
design gap in `_build_calendar_action` is real either way: it has no way to
narrow on the free-text title itself (no exact/`ILIKE` title match, only
semantic ranking that doesn't affect the row *count*), so any target
identified purely by a title phrase - with a person who attends more than
one event, or no person at all - depends on `k` happening to be `1` or on
titles being one-of-a-kind *and* on the row count already being ≤ `k`,
neither of which this template arranges.

**Fake vs real**: the *classification* half is fake-vs-real as described
above (quoted-vs-unquoted heuristic vs. real content-aware clarification,
and a real classifier could ask a more specific question referencing the
actual two "Sync with John" events instead of the generic canned string).
The *planning/execution* half - `find_target` resolving `ambiguous` even for
a uniquely-titled quoted event - is provider-independent, as shown above.

---

## 8. Hard case - "that email about the proposal" → conversation context

**Turn 1** (establishes context):

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "Find emails from sarah@acmecorp.com about the proposal"}'
```

Real result: `email_search`, top row `msg_004` ("Re: Acme Corp partnership -
Q3 proposal"). The pipeline appends this turn to Redis
(`ConversationStore.append_turn`) with `resolved_entities:
{"search_emails": "msg_004"}` - the id of the first result of the one
`search_*`-prefixed step (`QueryPipeline._resolved_entities`).

**Turn 2** (same `conversation_id`, references it):

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "What'"'"'s the status of that email about the proposal?", "conversation_id": "ba9fe8fe-6ee2-465b-b01e-2db7c0b8fe04"}'
```

**Plan shape**: 1 step, `search_emails`, identical shape to turn 1
(`email_search` intent again).

**Real result**: `references_prior_context: true`, and - apparently
correctly - `entities.person_emails == ["sarah@acmecorp.com"]` and the same
top row `msg_004` came back. **This "worked" for an incidental reason, not a
real one**: `IntentClassifier._build_user_message` embeds turn 1's *raw
query text* verbatim into the prompt for turn 2 (`"1. query: Find emails
from sarah@acmecorp.com about the proposal | intent: ... | resolved_entities:
{...}"`), and `FakeLLM`'s `_EMAIL_RE.findall(query)` regex-scans the *entire*
prompt blob, not just the current turn - so it picked up
`sarah@acmecorp.com` from the echoed history line, not from any genuine
context-resolution logic. `FakeLLM` never looks at `resolved_entities` at
all; it doesn't even know `msg_004` exists. Two things would break this
"accidental" carry-over: (a) if turn 1's query hadn't contained the email
address literally (e.g. "emails from Sarah about the proposal"), or (b) if 5
more turns happened first and turn 1 aged out of the capped 5-turn window
(`app/core/context.py::MAX_TURNS`).

**What the real classifier is actually supposed to do** (system prompt rule
5): "when the user says 'that email', ..., set `references_prior_context =
true` and COPY the concrete email address / event id / entity from the most
recent relevant turn's `resolved_entities`" - i.e. read the *structured*
`resolved_entities: {"search_emails": "msg_004"}` line and either put
`"msg_004"` somewhere usable, or (more usefully) recognize this as
`email_action`/a "get status of X" ask on that specific message rather than
re-running an unconstrained search. `FakeLLM` implements none of this; it
only sets the boolean flag.

---

## 9. Hard case - "next Tuesday" temporal convention

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "What'"'"'s on my calendar next Tuesday?"}'
```

**Plan shape**: 1 step, `search_events`, `starts_after:
"2026-07-07T00:00:00+05:30"`, `starts_before: "2026-07-07T23:59:59.999999+05:30"`.

**Real result**: `status: "empty"` - no fixture event falls on 2026-07-07.

**The convention, precisely** (`app/core/temporal.py::resolve_temporal`,
weekday branch): "now" is Thursday 2026-07-02; `week_start` (Monday of the
*current* week) is 2026-06-29.

* `"{weekday}"` alone or `"this {weekday}"` → `week_start + weekday_offset`
  - i.e. **this calendar week's** occurrence, even if it has already passed
  ("this Tuesday" here would resolve to 2026-06-30, two days in the past).
* `"next {weekday}"` → `week_start + 7 + weekday_offset` - **unconditionally
  the occurrence in next week's Mon–Sun block**, not "the closest upcoming
  {weekday}". For "next Tuesday" from a Thursday, both readings happen to
  agree (2026-07-07), because this week's Tuesday has already passed either
  way. The convention only becomes visible on days where it diverges from
  casual speech - e.g. if "now" were **Monday** 2026-06-29, "next Tuesday"
  would still jump to 2026-07-07 (next week's Tuesday), even though a
  colloquial reading might mean "tomorrow" (this week's Tuesday, one day
  away). The code has no such shortcut: `"next"` in the phrase always adds a
  full 7-day offset before applying the weekday index.
* `"last {weekday}"` → `week_start - 7 + weekday_offset`.

This is pure code (`app/core/temporal.py`), invoked identically regardless
of `LLM_PROVIDER` - **the only provider-dependent part is whether
`temporal_phrase` gets extracted correctly in the first place.**
`FakeLLM._TEMPORAL_RE` explicitly special-cases `"next tuesday"` (one of
only 8 recognized phrases - `next week|this week|tomorrow|today|next
tuesday|last week|last month|next month`); anything not on that list (e.g.
"next Wednesday", "in 10 days", "this weekend") is invisible to the fake
classifier, so `temporal_phrase` stays `null` and `resolve_temporal` never
runs even though the *code* fully supports those phrases. The real
classifier is instructed (system prompt rule 1) to copy **any** temporal
wording verbatim, so all of `resolve_temporal`'s branches (weekends, day
windows like "in 3 days", whole months, and a `dateutil` fuzzy-parse
fallback for explicit dates) become reachable.

---

## 10. Confirm-turn - confirming with nothing pending

Continuing the exact conversation from #4 (third turn, after the pending
action was already confirmed once):

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "yes, send it", "conversation_id": "98dbd8d1-596e-4b8b-9bce-d10f660a38a0"}'
```

**Plan shape**: none (`confirm_action` → empty plan, same as #4).

**Real result**:

```json
{
  "answer": "There is no pending action to confirm.",
  "plan": [], "results": {}, "needs_clarification": false, "pending_action": null
}
```

This string is hardcoded in `QueryPipeline._handle_confirmation` - it is
**not** synthesized by the LLM (the early-return happens before
`ResponseSynthesizer` is ever called), so it reads identically under
`LLM_PROVIDER=fake` or `openai`. This matters because the pending slot is
cleared unconditionally on every non-confirm turn too (`_handle_clarification`
and `_handle_chitchat` both explicitly `set_pending_action(cid, None)`), so
a stale pending action from several turns ago can never be accidentally
fired by a later, unrelated "yes".

---

## 11. Chitchat

```bash
curl -s -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" -H "X-User-Email: docs-demo@example.com" \
  -d '{"query": "Hi there, thanks for your help!"}'
```

**Plan shape**: none (`PLAN_TEMPLATES["chitchat"] = _build_empty`, and
`QueryPipeline.handle` special-cases an empty plan into `_handle_chitchat`
regardless of intent name - so `confirm_action`/`clarification_reply` with
nothing to confirm/clarify would land here too if they weren't already
intercepted earlier in `handle()`).

**Real result** (fake mode):

```json
{
  "answer": "Here is what I found:\n- The user said: Hi there, thanks for your help! Respond warmly and briefly describe what you can help them with.",
  "plan": [], "results": {}, "needs_clarification": false, "pending_action": null
}
```

**Fake vs real**: this is the starkest fake-vs-real gap in the whole system.
`FakeLLM.complete_text` (`app/llm/fake.py`) doesn't generate prose at all:
it returns `f"Here is what I found:\n- {snippet}"` where `snippet` is just
the input system+user text, truncated to 800 chars. It's deliberately inert
so tests can assert on plan/status shape without depending on LLM
creativity. Under `LLM_PROVIDER=openai`, `_CHITCHAT_SYSTEM` would produce an
actual warm, one-to-two-sentence reply mentioning it can search/act across
Gmail, Calendar, and Drive.

---

## 12. Conflict-detection `create_event` demo

**Not reachable via the query endpoint at all**, under either provider: no
`PLAN_TEMPLATES` builder ever emits a `create_event` step - `_build_calendar_action`
only builds `update_event`/`delete_event` against an *existing* found event.
`create_event` is only ever proposed via the LLM-backed
`complex_multi_service` path (and even then it would always be forced
`requires_confirmation=True`, since it isn't `search_*`/`get_*`/
`draft_email` - see `QueryPlanner._validate_llm_plan`). This demo instead
calls `CalendarAgent.execute("create_event", ...)` directly - the exact
method that runs once such a pending action is confirmed - against the real
seeded cache:

```python
agents = await build_agents(user, session, redis_async)  # real DB + mock client
await agents["calendar"].run("create_event", {
    "title": "Client sync (conflict demo)",
    "start": overlap_start.isoformat(),  # 15 min into evt_007's window
    "end": overlap_end.isoformat(),
    "attendees": ["demo@example.com"], "description": "...", "location": "Google Meet",
})
```

`evt_007` ("Design review") is cached at `2026-07-08T08:30:00+00:00` →
`09:30:00+00:00`; the new event's `[08:45, 09:15)` window overlaps it (and
also `evt_008`, "1:1 with manager", `08:30`–`09:00` the same day - the
fixtures deliberately include this overlapping pair). Real result:

```json
{
  "status": "conflict",
  "conflicts": [
    {"id": "evt_007", "title": "Design review", "start": "2026-07-08T08:30:00+00:00", "end": "2026-07-08T09:30:00+00:00", "..." },
    {"id": "evt_008", "title": "1:1 with manager", "start": "2026-07-08T08:30:00+00:00", "end": "2026-07-08T09:00:00+00:00", "..." }
  ]
}
```

`CalendarAgent._create_event` (`app/agents/calendar_agent.py`) runs
`HybridSearcher.find_overlapping` (a plain SQL range-overlap query, no
vector search involved - no ranking noise here at all) *before* calling the
client, and refuses to write, returning every conflicting cached event.
Retrying with a non-overlapping window succeeds and write-throughs into
`gcal_cache` (re-embedded, upserted on `(user_id, event_id)`):

```json
{"id": "evt_new_001", "title": "Client sync (no conflict)", "start": "2026-10-16T08:30:00+00:00", "end": "2026-10-16T09:00:00+00:00", "status": "ok"}
```

**Fake vs real**: none of this is provider-dependent - conflict detection is
plain date-range SQL, not semantic search, so it behaves identically under
fake and real embeddings. The only fake/real gap is upstream of this demo:
reaching `create_event` through the actual `/query` endpoint requires the
real OpenAI classifier to choose `complex_multi_service` and the real LLM to
propose `calendar.create_event` in its plan - genuinely nondeterministic,
which is why this demo drives the agent directly instead.

---

## 13. Label update (`email_action`)

**Not reachable via the query endpoint under the fake classifier**: any
query containing "email" hits `FakeLLM`'s rule 8 (`email_search`) before any
action-vs-search distinction is made - there's no keyword rule that ever
returns `email_action`. Demo drives `QueryPlanner.plan()` with a
hand-constructed `IntentResult` (what a real classifier would produce for
e.g. *"Label the Q3 budget approval email from finance as Important"*):

```python
intent = IntentResult(intent="email_action", services=["gmail"],
    entities=Entities(person_emails=["finance@company.com"], label="Important",
                       topic="Q3 budget approval"))
plan = await planner.plan(intent, None, user)   # real _build_email_action template
```

**Plan shape** - 2 steps, sequential (`_build_email_action`):

* `find_target` (gmail `search_emails`, `from_email="finance@company.com"`,
  `expect_single=True`)
* `apply_labels` (gmail `update_labels`, `depends_on: ["find_target"]`,
  `requires_confirmation=True`, `params: {"email_id": "{{find_target.top.id}}",
  "add": ["Important"], "remove": []}`) - the `label` entity being set is
  what routes to `apply_labels` instead of `draft_reply`
  (`_build_email_action`'s `if intent.entities.label:` branch).

**Real result, run through `DAGExecutor` against the seeded cache**:
`find_target` cleanly resolved to exactly `msg_008` ("Q3 Budget approval
needed", from `finance@company.com` - the only fixture email from that
address, so `from_email`'s exact metadata filter does the narrowing, not
semantic luck). `apply_labels` settled `pending_confirmation` with resolved
params `{"email_id": "msg_008", "add": ["Important"], "remove": []}` - the
agent (`MockGmailClient.update_labels`) was **not** called yet. Confirming
(replaying exactly what `QueryPipeline._handle_confirmation` does:
`agents["gmail"].run("update_labels", {...})`) produced:

```json
{"id": "msg_008", "labels": ["INBOX", "Important"], "status": "ok"}
```

**Expected answer sketch**: first turn summarizes "I found the Q3 budget
approval email from Finance and will add the 'Important' label - confirm?";
confirm turn states the label was added.

**Fake vs real**: identical caveat to #12 - the deterministic
`_build_email_action` template and `DAGExecutor` mechanics are 100%
provider-independent; only *reaching* `email_action` from a raw NL query
requires the real classifier.

---

## 14. Share file, `requires_confirmation` (`drive_action`)

**Also unreachable via the query endpoint under the fake classifier** for
the same reason as #13 (`"doc"`/`"file"`/`"drive"` keywords all route to
`drive_search`, never `drive_action`). Two things are worth showing here,
both real:

**(a) The deterministic template, run as a real classifier would build it**
for *"Share the Q3 budget spreadsheet with sarah@acmecorp.com"*:

```python
intent = IntentResult(intent="drive_action", services=["drive"],
    entities=Entities(file_hint="the Q3 budget spreadsheet", person_emails=["sarah@acmecorp.com"]))
plan = await planner.plan(intent, None, user)   # real _build_drive_action template
```

produces `find_target` with `mime_type="application/vnd.google-apps.spreadsheet"`
(`_mime_for_hint`'s mapping for the word "spreadsheet") - and **this returns
0 rows**, `status: "empty"`, because the fixture's "Q3 Budget.xlsx"
(`file_003`) actually carries the real Office Open XML mime type
(`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`), not
Google Sheets' native type. This is a genuine, verified mismatch between
`_mime_for_hint`'s hardcoded Google-native mapping and this fixture set (no
file in `app/services/mock/fixtures/files.json` ever carries
`application/vnd.google-apps.spreadsheet`) - a real would-be bug to flag if
this were being graded on the mock data working end-to-end for every mime
hint.

**(b) The same shape, using `owner` instead** (a valid `drive.search_files`
param per the canonical action surface - just not one `_build_drive_action`'s
specific template happens to wire up) - to demonstrate the
`requires_confirmation` → confirm mechanics on a clean single match:

```python
find_target = PlanStep(id="find_target", agent="drive", action="search_files",
    params={"query": "Q3 budget", "owner": "finance@company.com"}, expect_single=True)
share = PlanStep(id="share_file", agent="drive", action="share_file",
    params={"file_id": "{{find_target.top.id}}", "email": "sarah@acmecorp.com"},
    depends_on=["find_target"], requires_confirmation=True)
```

`find_target` resolved to exactly `file_003` (the only file owned by
`finance@company.com`). `share_file` settled `pending_confirmation` with
`{"file_id": "file_003", "email": "sarah@acmecorp.com"}`, agent **not**
called yet. Confirming produced:

```json
{"id": "file_003", "permission_id": "perm_001", "email": "sarah@acmecorp.com", "role": "reader", "status": "ok"}
```

**Expected answer sketch**: "I found Q3 Budget.xlsx and will share it with
sarah@acmecorp.com as a reader - confirm?"; confirm turn states it was
shared.

**Fake vs real**: (a) is a fixture/heuristic mismatch, not a provider
difference - it would fail identically under the real LLM classifier, since
the bug is in `_mime_for_hint`'s mapping and this mock fixture's mime type,
neither of which involve the LLM at all. (b)'s mechanics (search → single
match → `pending_confirmation` → confirm → real `share_file` call) are
provider-independent; only the initial NL→`drive_action` classification step
needs the real LLM to be reachable from a live `/query` call.

---

## Standing note: `entities.topic` pollution under `LLM_PROVIDER=fake`

Referenced throughout above. `IntentClassifier.classify` always calls
`self._llm.complete_structured(SYSTEM_PROMPT, user_message, IntentResult)`
where `user_message` is the *entire* assembled prompt - conversation history
+ timezone + ISO "now" + `f"Current user query: {query}"`
(`app/core/intent.py::_build_user_message`). The real `OpenAILLM` correctly
treats this as one input to reason over and extracts only the current
query's actual topic into `entities.topic`. `FakeLLM._fake_intent`, however,
receives that same assembled string as its `query` argument and, for intents
that set `entities.topic` (`Entities(person_emails=emails, topic=query)`),
stores the **whole prompt** verbatim as `topic` - this is why every
`params.query` shown as `"..."` above was, in the real transcript, several
hundred characters of conversation-history/timezone/now boilerplate followed
by the actual question. Unit tests never see this
(`tests/unit/test_intent.py` calls `FakeLLM` directly with just the bare
query string, which is why `test_fake_email_search_extracts_email` correctly
asserts `entities.topic == query`) - it only appears once `IntentClassifier`
is in the loop, i.e. through the real pipeline/HTTP API, which is why it's
easy to miss without running the whole stack end-to-end as this document
does. It doesn't change which *intent* gets chosen (the keyword checks are
substring checks against the same polluted blob, and the actual query text
is always present somewhere in it), but it does feed a much noisier string
into the embedding step for any search whose `query` param is sourced from
`entities.topic`. That's every deterministic search template except
`flight_cancellation` (`_build_flight_cancellation` builds its own literal
`"{airline} flight booking confirmation"` string and never touches
`entities.topic`) - including `meeting_prep`, whose `find_meeting`/
`find_emails`/`find_files` query is `_first(entities.company, entities.topic,
entities.event_title)`: it only *avoided* the pollution in scenario #5
above because `entities.company="Acme Corp"` was extracted and took
priority; a meeting-prep request where FakeLLM can't extract a company
(no `"<Word> Corp"` pattern in the text) would fall through to the same
polluted `entities.topic` as its search query.
