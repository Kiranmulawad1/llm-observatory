# ADR 0003 — TimescaleDB for spans, from day one

**Status:** Accepted (Phase 1) · revisit at Phase 9 (GCP deployment)

## Context

This platform stores two workloads with almost nothing in common:

| | Control plane | Telemetry |
|---|---|---|
| Tables | prompts, datasets, eval runs, projects, review queue | traces, spans |
| Access | read/write, transactional, FK-heavy | append-only, time-ordered |
| Volume | thousands of rows | millions to billions |
| Query | "which prompt version produced run X?" | "p95 latency by model, 5-min buckets, last 24h" |
| Retention | indefinite | tiered, then dropped |

Putting spans in an ordinary Postgres table works until it abruptly does not:
percentile queries over a large unpartitioned table degrade badly, and deleting
old rows produces bloat that autovacuum struggles to reclaim.

## Decision

**TimescaleDB**, plus two logical schemas — `control` and `telemetry` — in one
physical database.

TimescaleDB is a Postgres extension, not a separate database. Same wire protocol,
same asyncpg driver, same SQLAlchemy models, same Alembic history. Adoption cost
is close to zero: one line in `docker-compose.yml` and a `create_hypertable()`
call in one migration. In exchange, the spans table gets automatic time
partitioning, native compression on older chunks, and continuous aggregates that
maintain the p50/p95/p99 rollups incrementally instead of recomputing them per
dashboard refresh.

Adopting it *from day one* is the point. Retrofitting partitioning onto a
populated table means a migration with downtime — exactly the kind of thing that
does not get done.

## Alternatives considered

**Plain Postgres, Timescale later.** Simplest GCP story (Cloud SQL, fully
managed). Rejected because it defers the migration to when it is expensive, and
leaves percentile rollups to be hand-written.

**ClickHouse alongside Postgres.** Genuinely the right answer at high enough
volume — columnar storage and vectorised execution beat Timescale for wide
analytical scans. Rejected for now: a second data store means a second driver,
dual writes, no foreign-key integrity between spans and the projects/prompt
versions they reference, and consistency reconciliation between the two. That is
a large operational burden to carry for volume this project will not reach.

## Known constraint: managed Timescale does not exist on GCP

**Cloud SQL for PostgreSQL does not support the `timescaledb` extension.** The
Phase 9 deployment therefore forks three ways:

1. **Timescale Cloud** — managed, straightforward, but a second vendor.
2. **Self-hosted on GKE** (StatefulSet + PersistentVolume) — no extra vendor,
   but backups, failover and upgrades become ours.
3. **Cloud SQL with native declarative partitioning** — fully managed, and we
   hand-write the rollups Timescale would have maintained.

Mitigation, applied from the first migration: Timescale-specific SQL is confined
to a single migration plus a small set of named query functions. The schema is
designed to degrade to vanilla Postgres declarative partitioning, so the
deployment target can change without a rewrite. Isolating the vendor-specific
surface is the actual engineering decision here.

## Consequences

- Local dev runs `timescale/timescaledb:latest-pg17` rather than stock `postgres`.
- `telemetry` is separated at the schema level from the first migration, so
  moving it to its own instance later is a connection-string and repository
  change rather than a schema rewrite.
- The two schemas can be given different backup, retention and resource policies
  — which is the real reason they cannot share one forever.
