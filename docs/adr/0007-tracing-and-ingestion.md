# ADR 0007 — Tracing: span model, ingestion, and the SDK contract

**Status:** Accepted (Phase 5)

## Context

Everything through Phase 4 wrote to `control`: low volume, transactional,
foreign keys everywhere. Production traces are the opposite — append-only, high
volume, high cardinality — and they are produced by code running inside
*someone else's application*, which changes what "correct" means.

Three requirements shape this:

1. A request is a **tree** (retrieval → rerank → generation), not a log line.
2. Ingestion is the one endpoint exposed to the public internet.
3. The SDK runs inside a customer's request path and **must not be able to break
   it**.

## Decision

### The span model is OpenTelemetry's

Flat rows carrying `trace_id`, `span_id` and a nullable `parent_span_id`; the
tree is reconstructed by following parent pointers. Ids follow W3C Trace
Context — 32 hex characters for a trace, 16 for a span.

Adopting the standard costs nothing and buys interoperability: a team already
running OTel has ids that join up with ours, and can export elsewhere later. A
UUID scheme of our own would foreclose that for no benefit.

Flat beats nested JSON because it makes a span *queryable*. "p95 latency of every
retrieval span across the fleet" is `WHERE kind = 'retrieval'` with an index, not
a JSON traversal of every trace ever recorded.

### Spans are a TimescaleDB hypertable, and pay a real cost for it

Partitioned on `started_at` in one-day chunks. Time partitioning lets old chunks
be compressed or dropped wholesale, and bounds any query with a time filter to
the chunks that can contain matches.

**The cost:** Timescale requires the partitioning column in every unique index,
so spans have a composite primary key `(started_at, span_id)` rather than the
plain UUID key every `control` table uses. That inconsistency is the price of
partitioning, and it is precisely why ADR 0003 argued for separating the schemas
rather than letting telemetry live beside the control plane.

**Two consequences worth knowing:**

- Timescale creates its own descending index on the time column. Alembic's
  autogenerate sees an index the database has and the models do not, and proposes
  dropping it — on every migration, forever. `migrations/env.py` excludes it.
- Retention and compression policies are deliberately **not** in the migration.
  Both are per-environment policy, and encoding "delete after 90 days" in a
  migration would silently destroy a customer's data on deploy.

### Spans carry no foreign key to `control.projects`

`project_id` is denormalised onto every span. An append-only table taking
thousands of inserts a second should not pay a referential-integrity check per
row — and ADR 0003 already committed to `telemetry` being movable to its own
instance, where a cross-database foreign key could not exist at all.

### A `traces` rollup table, recomputed rather than patched

Duration, cost, token totals and status live on a `traces` row, refreshed from
the spans on ingest. Deriving them per dashboard read would aggregate raw spans
on every page view of the table that grows without bound.

**Recomputed, not incremented.** Spans arrive out of order and across separate
batches — children finish before parents, so they are flushed first, and a late
span can change the totals after the root has been written. Incremental deltas
would drift from the truth with nothing to detect it. Recomputing costs one
aggregate query per touched trace per batch, which is cheap because a trace has
tens of spans.

**Any errored span makes the trace an error.** A request whose retrieval step
failed but which still returned something is not a success. Counting it as one is
exactly how error-rate dashboards end up lying.

### API keys, pulled forward from Phase 8

Ingestion is a public write endpoint. Shipping it unauthenticated even
temporarily would mean anyone could forge traces into any project, and the SDK's
auth contract would have to change later on every application that had already
integrated.

- The plaintext key is returned **once**, at creation. Only a hash is stored.
- **SHA-256 with a server-side pepper, not bcrypt.** Slow hashing exists to make
  guessing a low-entropy human secret expensive. An API key here is 256 bits from
  the OS CSPRNG — guessing is not a threat model, and the hash is verified on the
  hottest endpoint in the system, where a 100 ms KDF would cap throughput at ten
  spans per second per core. The pepper covers what a salt would: a stolen
  database alone cannot verify guesses offline.
- Lookup is by a stored clear **prefix**, so verification is one indexed read
  rather than hashing every key in the table.
- Comparison uses `hmac.compare_digest`. String equality short-circuits on the
  first differing byte, leaking through timing how much of a guess was right.
- **The project comes from the key, never from the request body.** A client
  cannot write into a project it does not hold a key for.
- Revocation is a timestamp, not a delete — traces stay attributable to a
  credential that can still be accounted for.

### Rate limiting is a sliding window, and fails open

A fixed window resets on a boundary, so a client can send a full quota either
side of it and achieve twice the intended rate at the worst possible moment. The
sliding window holds at every instant.

Cost is the **span count**, not one per request: otherwise 500-span batches sent
a thousand times a minute stay inside a request-count limit while writing half a
million rows.

**If Redis is down, requests are allowed through.** A rate limiter protects
against abuse; it is not a correctness mechanism. Refusing all telemetry because
the limiter is unavailable turns a degraded dependency into an outage, and losing
observability data during an incident is precisely the wrong failure.

### The SDK's one hard requirement

> Instrumentation must never break, block, or slow the application it instruments.

An observability tool that takes down the service it observes is worse than none.
Every choice follows:

- **A bounded queue.** If the platform is unreachable, spans accumulate. An
  unbounded buffer grows until the host process is OOM-killed — the tracing
  library would have caused the outage. The queue caps, drops oldest-first, and
  counts the loss so it is visible rather than silent.
- **`put_nowait`, never `put`.** Blocking would push our backpressure into the
  caller's request handler.
- **A daemon thread, not asyncio.** The host may be sync or async; a thread and a
  plain queue work identically in both. An asyncio flusher would need a running
  loop and simply would not work in half of them. Daemon, so a hung flush cannot
  stop the process exiting; shutdown gets a *bounded* drain window.
- **Every public call is wrapped.** Network failure, malformed payload, a bug in
  this library — none of it escapes into the caller's stack.
- **Inert without an API key.** No thread, no network, no error.
- **Payloads are truncated.** A span's input can be an entire retrieved corpus;
  nobody wants their observability tool to be why a request body is 50 MB.

**Parent tracking is a `ContextVar`, not a thread-local.** A ContextVar is
inherited by asyncio tasks, so nesting survives an `await` and concurrent tasks
stay separate. A thread-local would give every coroutine on one event-loop thread
the same parent — producing a trace tree that is confidently wrong, which is
worse than no tree at all.

**Auto-instrumentation is a proxy, not a subclass.** `instrument(Anthropic())`
delegates by attribute lookup, so the vendor SDK's surface can change without
breaking us and anything unwrapped passes straight through. Response parsing is
defensive throughout: a renamed field must degrade to a span with less detail,
never raise into the caller's model call.

### `instrument()` covers two client shapes, and refuses the rest

Anthropic (`.messages.create`) and OpenAI-compatible (`.chat.completions.create`)
— the second of which is Groq, Together, OpenRouter, vLLM and Ollama too, since
they share a client. Detection is duck-typed on attributes rather than
`isinstance`, because importing the vendor SDKs to identify a client would put
them in this package's dependency floor, and ADR 0001 keeps that at httpx alone.

An unrecognised client **raises**. The never-raise rule governs the request
path: a span that cannot be recorded is dropped, because telemetry must not
break someone's application. Setup is a different question, and returning a
proxy that silently traces nothing leaves an engineer staring at an empty
dashboard with no error to search for — which is exactly what passing an OpenAI
client to this function used to do.

Sync and async are decided from the wrapped callable at call time, not baked
into separate method names. An async client's method is also called `create`; it
just returns a coroutine. The previous version defined `create` as sync and
`acreate` as async, which no vendor SDK is shaped like, so every async call
produced a span with no token counts and a duration near zero while the real
call went untraced. Wrong data is worse than missing data, because nobody
investigates a dashboard that looks fine.

### Tree assembly is property-tested, and that found three bugs

Span ids and parent pointers are **client-supplied** — W3C Trace Context ids are
generated by the instrumented application — so the shapes reaching `build_tree`
are bounded by what a buggy or hostile SDK can emit, not by what our SDK does.
Example-based tests prove the cases someone thought of; that is the wrong tool
for an input space defined by what an adversary might send.

Hypothesis generates arbitrary parent pointers, including a span's own id, and
asserts invariants rather than outputs. Three properties, three bugs:

**Cycles caused silent data loss.** A span naming itself as its parent, or two
naming each other, belonged to no root and to no orphan list — they were linked
only to each other and fell out of the response entirely. Four spans in, two
out, no error. That directly contradicted this function's own promise that a
span is returned "rather than dropped" because "hiding those spans would make a
trace look complete when it is not". Hypothesis shrank it to the minimum case: a
single self-parenting span, which is what one off-by-one in an instrumentation
library produces.

**Duplicate span ids were emitted twice.** The spans table is keyed
`(started_at, span_id)`, because Timescale requires the partitioning column in
every unique index — so the same span id arriving with a different timestamp is
two rows, and both were appended to the parent's children.

**Trees deeper than 255 could not be returned at all.** Pydantic's serialiser
refuses to descend further, so the endpoint raised and the whole trace became
unreadable rather than merely deep. A recursive agent or a runaway loop produces
one, and ingest accepts whatever nesting a client sends. Depth is now bounded at
200 and deeper subtrees are detached as orphans, so nothing is lost and the
response always serialises.

The properties are worth more than the fixes. "Every span appears exactly once"
holds for every input, whereas each example test restates a shape someone
already imagined — and each fix, disabled, fails exactly its own property.

### No per-framework integrations, deliberately

There are no LangChain or LlamaIndex callback handlers, and there should not be.
Both frameworks already have OpenTelemetry instrumentation, and ADR 0012 made
this platform speak OTLP — so a user of either points an exporter at it with two
environment variables and no code change.

A bespoke handler would deliver the same coverage through a *worse* path, one
that requires editing the application, while taking on a maintenance treadmill
every time a framework revises its callback API. Implementing the protocol is
what makes the per-framework integrations unnecessary; shipping both would be
admitting the protocol work did not land.

## Consequences

- **Ingest is idempotent** on `(started_at, span_id)`, which is what makes the
  SDK's retries safe — it cannot know whether a failed request actually landed.
  Duplicates are counted and reported, so a client with misfiring retries can see
  it rather than silently doubling its writes.
- **Orphan spans are surfaced, not dropped.** A span whose parent has not arrived
  is returned separately. Hiding it would make a partial trace look complete.
- **The `traces` rollup can be briefly wrong.** Between a child arriving and its
  root arriving, the trace shows `(root pending)` and a duration derived from the
  observed window. It self-corrects on the next batch.
- **A time bound is effectively mandatory.** A hypertable query without one scans
  every chunk ever written, so the list endpoint supplies a 24-hour default
  rather than leaving it optional and letting an innocent `GET /traces` scan a
  year.
- **Alerting is deferred to Phase 6.** It is threshold evaluation over metrics
  queries that do not exist yet; building it before the dashboard's aggregations
  would mean writing those queries twice.
