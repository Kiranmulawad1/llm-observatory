"""Prometheus metrics for the platform's own health.

### The line this module draws

This platform already stores latency, cost and error rates — per project, per
prompt version, per model — in TimescaleDB, and serves them at
`/projects/{slug}/metrics`. So why a second metrics system?

Because they answer different questions for different people:

| | `telemetry` in TimescaleDB | Prometheus, here |
| --- | --- | --- |
| asks | "how is *my application* behaving?" | "how is *the platform* behaving?" |
| read by | the tenant, in the dashboard | whoever operates the platform |
| retention | months, queryable per tenant | days, aggregate |
| cardinality | high, and that is the point | low, deliberately |

Conflating them is the classic mistake, and it fails in a specific way: you add
`project_id` as a Prometheus label, and now every tenant creates a permanent
time series in every metric they touch — including tenants who churned a year
ago, because Prometheus has no idea a project was deleted. A few hundred
tenants times a handful of metrics is a memory problem; a few thousand is an
outage in the monitoring system you installed to prevent outages.

**So nothing here is labelled by project, model, prompt, or user.** If a
question needs per-tenant resolution, it is a TimescaleDB question and the
answer is already in the dashboard. This module is for "is the queue backing
up", "is one provider slow", "are we dropping spans" — fleet questions, where
the answer is a single number for the whole deployment.

That constraint is also what makes it safe to serve `/metrics` without
authentication (see the route). There is no tenant data in here to leak,
by construction.

### Process model

Counters live in the process that increments them. The API is deployed one
uvicorn process per pod, so a pod's counters are that pod's, and Prometheus
sums across pods. **Adding `--workers N` would silently break that** — each
worker process would keep its own counters and the scrape would return
whichever one answered. `prometheus_client` has a multiprocess mode for that
case; adopting it means a shared directory and a different registry, so it is a
deliberate change rather than something that survives a flag being added.
"""

from __future__ import annotations

# The default registry is used deliberately: it also carries the process and
# GC collectors that `prometheus_client` installs, and "how much memory is this
# pod using" is a question worth being able to answer without extra wiring.
from prometheus_client import REGISTRY as REGISTRY
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST as OPENMETRICS_TYPE

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# --- Ingest -----------------------------------------------------------------

spans_ingested = Counter(
    "lo_spans_ingested_total",
    "Spans accepted and stored.",
    # `source` separates the native SDK from OTLP, which is the one split worth
    # having: it answers "is anyone actually using the OTLP endpoint" and, when
    # something breaks, which path it broke on.
    ["source"],
)

spans_rejected = Counter(
    "lo_spans_rejected_total",
    "Spans refused before storage.",
    # Bounded set of reasons, defined in code — never a raw exception message,
    # which is user-influenced and therefore unbounded.
    ["source", "reason"],
)

ingest_batches = Counter(
    "lo_ingest_batches_total",
    "Ingest requests received.",
    ["source"],
)

# --- Queue ------------------------------------------------------------------

queue_depth = Gauge(
    "lo_queue_depth",
    "Jobs waiting in the arq queue.",
    ["queue"],
)

# --- Eval runs --------------------------------------------------------------

eval_run_duration = Histogram(
    "lo_eval_run_duration_seconds",
    "Wall-clock duration of a completed eval run.",
    ["status"],
    # An eval run is a fan-out over a dataset, so the interesting range is
    # seconds to tens of minutes — the default buckets top out at 10s and would
    # put almost every real run in +Inf.
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
)

eval_examples = Counter(
    "lo_eval_examples_total",
    "Examples scored, by outcome.",
    ["outcome"],
)

# --- Providers --------------------------------------------------------------

provider_duration = Histogram(
    "lo_provider_request_duration_seconds",
    "Latency of a single model call.",
    # Provider but *not* model. Model names come from prompt versions, which are
    # user-authored, so labelling by model hands tenants a way to create
    # unbounded series in the platform's own monitoring.
    ["provider", "operation"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)

provider_errors = Counter(
    "lo_provider_errors_total",
    "Failed model calls.",
    # `retryable` is the split that matters operationally: a wall of retryable
    # errors is a rate limit to negotiate, a wall of permanent ones is a bug.
    ["provider", "retryable"],
)

# --- HTTP -------------------------------------------------------------------

http_requests = Counter(
    "lo_http_requests_total",
    "HTTP requests served.",
    # The route *template* (`/projects/{project_slug}/traces`), never the
    # resolved path. Using the raw path would mint a new series per project and
    # per trace id, which is the same cardinality trap in a different costume.
    ["method", "path", "status"],
)

http_duration = Histogram(
    "lo_http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "path"],
)

# --- Build ------------------------------------------------------------------

build_info = Gauge(
    "lo_build_info",
    "Always 1. Labels carry the identity of the running process.",
    ["service", "environment"],
)


def render() -> bytes:
    """Serialise the registry in the Prometheus text exposition format."""
    return generate_latest(REGISTRY)


__all__ = [
    "CONTENT_TYPE",
    "OPENMETRICS_TYPE",
    "REGISTRY",
    "CollectorRegistry",
    "build_info",
    "eval_examples",
    "eval_run_duration",
    "http_duration",
    "http_requests",
    "ingest_batches",
    "provider_duration",
    "provider_errors",
    "queue_depth",
    "render",
    "spans_ingested",
    "spans_rejected",
]
