# ADR 0001 — Monorepo with a uv workspace, and where the seams go

**Status:** Accepted (Phase 1)

## Context

Five deployable or distributable things: an API, a worker, a frontend, a shared
domain layer, and a client SDK that other teams install into *their*
applications. They need to share a data model without sharing a release cycle.

## Decision

One repository, one `uv` workspace, one lockfile, with four Python members:

| Member          | Role                          | May import        |
| --------------- | ----------------------------- | ----------------- |
| `packages/core` | domain: models, schemas, evaluators, services | — |
| `apps/api`      | HTTP entrypoint               | `core`            |
| `apps/worker`   | job entrypoint                | `core`            |
| `packages/sdk`  | published client              | **nothing local** |

Two rules carry the weight:

**`api` and `worker` are thin entrypoints over `core`.** They share almost all
their logic but nothing about their runtime profile: the API is latency-bound and
scales on request rate; the worker is throughput-bound and scales on queue depth.
Separate images and separate deployments are what make those independent. Merging
them into one image is the shortcut, and it costs exactly the ability to scale
them apart.

**The SDK imports nothing from `core`.** It is installed into other teams'
applications, so it carries one runtime dependency (`httpx`) and supports Python
3.10+, wider than the platform's own 3.13 floor. Letting it import `core` would
drag SQLAlchemy, asyncpg and Alembic into a consumer's dependency tree and make
the platform's Python floor theirs. The boundary is enforced by dependency
declaration, not by convention.

## Alternatives considered

**Separate repositories per service.** Honest about release cycles, but every
schema change becomes a cross-repo version dance, and there is no single commit
that atomically updates a model plus the API plus the worker plus the migration.
At this size that cost dominates.

**A single flat package.** Fastest to start, and it forecloses independent
scaling and independent SDK publishing — the two things this project exists to
demonstrate.

## Consequences

- One `uv.lock` means every service resolves against identical versions; a
  dependency conflict surfaces at lock time rather than in a production image.
- Docker builds take the **repo root** as context and select a member with
  `uv sync --package <name>`, so each image installs only its own closure.
- CI must enforce the SDK boundary. A stray `import lo_core` in the SDK would
  pass tests locally and only fail when a consumer installs from PyPI.
