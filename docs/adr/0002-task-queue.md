# ADR 0002 — Arq over Celery for async eval jobs

**Status:** Accepted (Phase 1)

## Context

An eval run is a fan-out: *N* dataset examples × *M* evaluators, where nearly
every unit of work is an outbound network call (an LLM completion, an embedding,
a judge invocation). A 500-example run with three evaluators is 1,500 mostly-idle
requests. The job must survive a worker restart, retry transient provider
failures, and never disappear silently.

## Decision

**Arq**, backed by the same Redis instance used for rate limiting.

The workload is I/O-bound, not CPU-bound. Arq is async-native, so one process
holds hundreds of concurrent in-flight calls with `asyncio.gather` plus a
semaphore, and — the part that actually matters day to day — it reuses the exact
same asyncpg engine, SQLAlchemy session and repository code as the API. There is
one data access layer in this codebase, not a sync one and an async one.

## Alternatives considered

**Celery.** More mature, larger ecosystem, Flower for monitoring, built-in
dead-letter support. But it is sync-first, and every way of bridging that is bad
here:

- `asyncio.run()` per task — throws away connection pooling, opening a fresh
  Postgres connection per job.
- prefork with high concurrency — spends a process per concurrent request on
  work that is waiting on a socket.
- gevent monkey-patching — conflicts with `httpx` and `asyncpg`, both of which
  assume a real asyncio loop.

**A `BackgroundTasks` / bare `asyncio.create_task` approach.** Explicitly
rejected: work would be lost on pod eviction, there is no retry, no visibility,
and no back-pressure. Precisely the silent-failure mode this platform is supposed
to detect in *other* people's systems.

## Consequences

- **We own the dead-letter path.** Arq has no built-in DLQ, so jobs exhausting
  `max_tries` are written to `control.dead_letter_jobs` with their payload and
  final exception (Phase 3). This is a feature, not a workaround — a failed eval
  run must be inspectable and replayable, which a generic broker-level DLQ does
  not give you anyway.
- Observability is ours to build: Arq has no Flower equivalent, so job state is
  surfaced through our own API and dashboard. Acceptable, since the project is an
  observability platform.
- Redis is now on the critical path for job durability, hence `appendonly yes`
  in local dev and a managed Redis with persistence in production.

## When this decision would change

At multi-tenant scale with per-customer priority queues and fair scheduling, or
if eval runs grow into durable multi-step workflows needing mid-flight recovery
and human-in-the-loop steps, the answer is **Temporal**, not Celery. Arq's
ceiling is "reliable fan-out of independent units of work," which is exactly the
shape of the problem today.
