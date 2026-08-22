# Ingest benchmark

```bash
make up                 # Postgres + Redis
make bench-api          # terminal 2: API with the rate limit raised
make bench              # terminal 3
make bench-clean        # remove the rows it wrote
```

## What it measures, and why in these units

**Spans per second, not requests per second.** Batch size is a free variable —
doubling `BENCH_BATCH` halves the request rate while doing strictly more work,
so a request-rate figure says nothing without it. Spans are what the platform
stores, bills, and indexes.

**The two ingest paths separately, not mixed.** OTLP does protobuf-shaped
decoding and GenAI attribute mapping that the native path skips. Running them
in one scenario would average those costs into a number attributable to
neither; running them back to back makes the difference visible.

**Realistic trees, not flat lists.** Each batch is one root with children that
carry parent pointers, model names and token counts. A flat list of identical
spans would skip the parent-pointer writes and the rollup recompute, which is
most of the per-span cost.

## Why the rate limit is raised

The shipped default is 6000 spans/min per project — a **tenant-fairness**
ceiling, not a capacity one. Benchmarking against it measures the rate limiter
and reports its configured value as the platform's throughput, which is worse
than not measuring at all because the number looks real.

`make bench-api` starts the API with `LO_INGEST_RATE_LIMIT_PER_MINUTE` raised.
That setting exists because of this benchmark; it was a hardcoded constant
before, which is production-hostile for its own reasons.

## k6 rather than Locust

Locust is Python and would match the stack, which is exactly the problem: a
Python load generator saturating a Python server tends to measure the
generator. k6's VUs are goroutines, so a laptop can hold enough concurrency to
saturate the API without competing with it for the GIL.

Locust would be the better choice if the load pattern needed real application
logic — stateful user journeys, complex conditional flows — which ingest does
not.

## Reading the result

Throughput is flat across concurrency levels while latency scales linearly with
it. That is the signature of a **saturated** system, not a headroom problem:
by Little's Law (`L = λW`), if arrival rate `λ` is pinned by the server then
adding concurrency `L` can only increase residence time `W`. Every additional
virtual user waits in a queue rather than getting work done sooner.

Saturation here is at or below 5 concurrent clients, so the honest headline is
the throughput figure plus the concurrency at which latency is still healthy —
not the largest number a bigger `BENCH_VUS` can be made to print.

The bottleneck is the single uvicorn process. That is deliberate rather than an
oversight: the API is deployed one process per pod because `prometheus_client`
keeps counters in process memory (ADR 0014), so the platform scales out on
replicas and an HPA rather than up on `--workers`. A meaningful multi-replica
number needs a cluster, which this project does not pay for.

## Caveats that apply to every number here

- A **laptop**, with Postgres and Redis in Docker Desktop on the same machine,
  competing for the same CPU and the same virtualised disk.
- Recorded while the host filesystem was **at 100% capacity**, which depresses
  write throughput by an amount not measured here. Treat the figures as a
  floor.
- No network hop: client, API and database are all on localhost, so real
  latency will be higher and real throughput may be higher with a dedicated
  database host.
- One API replica, one worker, one Postgres — no replication, no connection
  pooler, no load balancer.

These are not reasons to distrust the numbers. They are the reasons the numbers
are a *floor for this hardware* rather than a capacity statement about the
software.
