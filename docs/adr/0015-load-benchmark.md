# 0015 — Measuring ingest throughput

Status: accepted
Date: 2026-08-22

## Context

Every claim about this platform so far has been about correctness — what it
stores, what it refuses, what it recomputes. None has been about capacity. "It
ingests spans" invites the obvious question, *how many*, and the honest answer
was that nobody had measured.

## Decision

A k6 script (`bench/ingest.js`) against the local compose stack, with the
numbers and their caveats in the README.

### k6, not Locust

Locust is Python and would match the stack, which is precisely the objection: a
Python load generator saturating a Python server measures the generator as much
as the target, and the failure is silent — the throughput ceiling looks like
the server's.

k6's virtual users are goroutines, so one laptop can hold enough concurrency to
saturate the API without competing with it for the GIL. Locust would win if the
load needed real application logic — stateful journeys, conditional flows —
which span ingest does not.

### Spans per second, not requests per second

Batch size is a free variable. Doubling it halves the request rate while doing
strictly more work, so a request-rate figure is uninterpretable without
publishing the batch size alongside it — and is trivially inflatable by
shrinking batches. Spans are the unit the platform stores, indexes and bills.

### The rate limit is raised, and that is why it became configurable

The shipped default is 6000 spans/min per project. That is a **tenant-fairness**
ceiling, not a capacity ceiling, and benchmarking against it would have
measured the rate limiter and reported its configured value as the platform's
throughput — a number that is worse than no number, because it looks real.

It was a module constant. Making it `Settings.ingest_rate_limit_per_minute` is
a change the benchmark forced but which stands on its own: a single-tenant
internal deployment and a public one want different ceilings, and neither
should need a code change.

### Both ingest paths, separately

OTLP does decoding and GenAI attribute mapping the native path skips. Mixing
them in one scenario would produce an average attributable to neither. Run back
to back, the difference is visible and turns out to be 10–20% — which is a
useful thing to know before someone proposes deleting the native SDK.

## What the measurement actually showed

Throughput is flat at roughly 3,250 spans/sec across 5, 10, 20 and 40
concurrent clients, while p95 latency rises from 94 ms to 830 ms.

That is **saturation**, and Little's Law is the reason: `L = λW`, so if the
arrival rate `λ` is pinned by the server, increasing concurrency `L` can only
increase residence time `W`. Every extra client is queueing, not working.

The consequence for how the number is reported: the honest headline is the
throughput *plus the concurrency at which latency is still healthy* —
~3,250 spans/sec at p95 under 100 ms — rather than the largest figure a higher
`BENCH_VUS` can be made to print. Both are the same throughput; only one of
them describes a system anyone would want to run.

The bottleneck is the single uvicorn process. That is a deliberate constraint
rather than an oversight: the API runs one process per pod because
`prometheus_client` keeps counters in process memory (ADR 0014), so the platform
scales out on replicas and an HPA rather than up on `--workers`. Measuring that
properly needs a multi-node cluster, which this project does not pay for.

### Thresholds, so it is a guard and not a report

The script fails on error rate, on p95 for each path, and on **throughput** —
`spans_ingested` rate below 2000/sec fails the run. Latency thresholds alone
would pass a build that quietly did less work. They are calibrated at the
documented default concurrency with roughly 2x headroom: tight enough to catch a
real regression, loose enough not to fire on the variance of a laptop that is
also running Postgres.

### Cleanup is part of the tool

A run writes about 380 bytes per span, so a few hundred thousand spans is tens
of megabytes each time. `make bench-clean` removes them. Spans carry no foreign
key to `control.projects` (ADR 0003, deliberately), so deleting the project does
not cascade and the telemetry rows have to go explicitly — which is exactly the
kind of thing that is obvious in the schema and forgotten in a script.

## Consequences

- The numbers in the README are from a laptop with Postgres, Redis and the API
  on one machine, no network hop, one replica of everything, recorded while the
  host filesystem was at 100% capacity. They are a floor for that hardware, not
  a capacity claim about the software, and the README says so where the numbers
  are rather than in a footnote nobody reaches.
- Re-running on different hardware will produce different numbers. The
  methodology, not the figures, is the part worth keeping.

## What production at scale would add

- **A load generator that is not the developer's laptop**, on separate hardware
  from the target, so the two do not contend.
- **Multi-replica measurement**, which is the number that actually matters given
  the platform scales horizontally.
- **A soak test.** Fifteen seconds says nothing about connection pool
  exhaustion, hypertable chunk boundaries, or memory growth over hours.
- **Read benchmarks.** Dashboard queries against a table with a billion spans
  are a completely different question from ingest, and the more likely one to
  disappoint.
- **CI regression tracking**, storing each run so a trend is visible rather than
  a single pass/fail against a static threshold.
