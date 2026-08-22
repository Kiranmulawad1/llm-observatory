# 0012 — OTLP ingest and the GenAI semantic conventions

Status: accepted
Date: 2026-08-22

## Context

The platform has a tracing SDK of its own (ADR 0007). It is small, it never
raises, and it is three lines to adopt — but it is still *our* SDK, and adopting
it means instrumenting an application a second time if that application already
emits OpenTelemetry. Most serious candidates do: directly, or through
OpenLLMetry, Logfire, or an OpenTelemetry Collector already in the path.

Asking those teams to re-instrument is asking them to do work they have already
done, and it is the single largest reason a self-hosted observability tool never
gets evaluated at all.

The span model was designed against OpenTelemetry from the beginning — W3C
Trace Context ids, a flat parent-pointer tree, the same status vocabulary — so
this is a mapping exercise, not a second data model.

## Decision

### The endpoint is `POST /otlp/v1/traces`

OTLP's standard path is `/v1/traces`, and this platform already serves its
native SDK there with a different JSON body. Sharing one path means guessing
which schema arrived, and OTLP's own JSON encoding makes that guess genuinely
ambiguous rather than merely inelegant.

`OTEL_EXPORTER_OTLP_ENDPOINT` is a **base** URL — every OTLP/HTTP exporter
appends `/v1/traces` to it. Mounting under `/otlp` therefore gives exporters
precisely the URL they already construct:

```
OTEL_EXPORTER_OTLP_ENDPOINT=https://<host>/otlp
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer lo_live_...
```

Two settings, both of which a user configuring any backend is setting anyway,
and no code change. The native endpoint keeps working unchanged.

The rejected alternative was content-type sniffing on `/v1/traces`. Protobuf
would be unambiguous, but OTLP-JSON and our native JSON are both
`application/json`, so disambiguation would come down to inspecting the body for
a `resourceSpans` key. That is a guess dressed as a protocol.

### Both wire encodings, because the default is protobuf

The OpenTelemetry SDKs send protobuf over HTTP by default. Supporting only JSON
would turn "change one setting" into "change one setting and override the
encoding", which is the exact friction this endpoint exists to remove. JSON is
also accepted, since Collectors and hand-rolled exporters use it.

The response is encoded to match the request. Answering a protobuf export with
JSON makes an exporter log a parse failure on every *successful* export, which
looks indistinguishable from data loss to whoever is reading those logs.

`opentelemetry-proto` is a dependency of `apps/api`, not `packages/core`. OTLP
is a wire protocol and ADR 0001 keeps `core` to the domain; the worker has no
business decoding protobuf, and each image installs only its own closure.

### Three generations of attribute are read

The GenAI conventions are young and every instrumentation library sits at a
different point in their history. All three spellings are live right now:

| meaning | current | earlier | OpenLLMetry |
| --- | --- | --- | --- |
| input tokens | `gen_ai.usage.input_tokens` | `gen_ai.usage.prompt_tokens` | `llm.usage.prompt_tokens` |
| model | `gen_ai.response.model` | `gen_ai.request.model` | `llm.request.model` |

Reading all of them is a few lines and spares every user from patching their
instrumentation. Where an exporter emits several, the most current spelling
wins: an exporter emitting both is mid-upgrade, and the new key is the one it
keeps.

Response model beats request model, because the request says `gpt-4` while the
response says `gpt-4-0613`, and the question worth answering is what actually
served the call.

### Span kind is decided by attributes, never by name

Order: explicit `traceloop.span.kind`, then `gen_ai.operation.name`, then a
vector store in `db.system`, then the presence of any model attribute, and
finally a parentless span defaulting to `chain`.

Span *names* are deliberately never consulted. They are free text chosen by
whoever wrote the instrumentation, and a classifier keyed off them silently
changes behaviour when somebody renames a function.

### Nothing is discarded

Attributes not promoted to a column land in `metadata`, with resource
attributes namespaced under `resource` so a `service.name` on the resource
cannot be confused with one on the span. An unrecognised convention degrades to
"still searchable" rather than "silently dropped" — which matters because the
conventions will keep moving.

### Partial success rather than rejection

Spans that cannot be mapped are counted into `partialSuccess.rejectedSpans` and
the batch still lands. An exporter that receives a 4xx retries the identical
payload indefinitely, so failing a 500-span batch over one malformed span would
cost the other 499 on every attempt, forever.

## What this exposed

Writing the out-of-order tests found a **pre-existing data-corruption bug** in
the rollup, unrelated to OTLP.

`traces.started_at` is `MIN(span.started_at)` and is part of the primary key,
because Timescale requires the partitioning column in every unique index. When a
span that *starts* earlier than the current minimum arrives in a later batch —
routine, since spans complete innermost-first while a batching exporter flushes
on a timer — the minimum moves, the upsert's conflict target no longer matches
the stored row, and a **second rollup row is inserted for the same trace**. The
read path then raises `MultipleResultsFound`: the trace becomes unreadable, and
it is late data rather than bad data that breaks it.

`refresh_trace` now reads the existing rows first, carries `flagged_at` forward
(it is set by guardrail sampling rather than derived from spans, so re-keying
without it would silently drop a trace out of the human review queue), upserts,
and then deletes any superseded rows. There is a regression test on the native
ingest path, so the guard does not depend on OTLP existing.

The native SDK never triggered this because it buffers a trace and tends to
flush the root alongside its children. OTLP exporters batch on a timer and do
not.

## What production at scale would add

- **Protobuf over gRPC.** `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` is the other half
  of OTLP and is what most Collector deployments speak internally. HTTP first
  because it traverses proxies and needs no second port.
- **Compression.** Exporters send `Content-Encoding: gzip` by default over gRPC
  and optionally over HTTP; this endpoint does not yet decompress.
- **Metrics and logs.** `/v1/metrics` and `/v1/logs` are the same shape of
  problem. Traces first, because they are what this platform is about.
- **A dead-letter path for rejected spans.** They are counted today; the ones
  that failed to map are not kept, so a mapping bug is invisible after the fact.
- **Convention drift monitoring.** An unrecognised `gen_ai.*` attribute landing
  in `metadata` is silent. Counting them would show when the conventions have
  moved on and this mapping needs updating.
