# 0014 — Self-observability: Prometheus, and the cardinality line

Status: accepted
Date: 2026-08-22

## Context

This platform collects latency, cost and error rates for other people's
applications and has, until now, collected none for itself. If the queue backs
up, a provider degrades, or the OTLP endpoint starts rejecting spans, the only
signal is a log line somebody happens to read.

That is an uncomfortable gap in an observability tool specifically, and it is
also the obvious interview question: *does your monitoring platform monitor
itself?*

## Decision

`prometheus-client`, a `/metrics` endpoint on the API, and a metrics-only HTTP
server on the worker. Metric definitions live in `packages/core` so the two
services cannot drift; exposition is each app's own concern.

### The line: two metrics systems, on purpose

The platform now has two, and the distinction is the whole decision:

| | `telemetry` in TimescaleDB | Prometheus |
| --- | --- | --- |
| answers | "how is *my application* behaving?" | "how is *the platform* behaving?" |
| read by | the tenant, in the dashboard | whoever operates the platform |
| served at | `/projects/{slug}/metrics` | `/metrics` |
| retention | months, per tenant | days, aggregate |
| cardinality | high, and that is the point | low, deliberately |

Conflating them is the standard mistake, and it fails in a specific way: you add
`project_id` as a Prometheus label, and every tenant becomes a permanent time
series in every metric they touch — **including tenants that churned a year
ago**, because nothing tells Prometheus a project was deleted. A few hundred
tenants is a memory problem; a few thousand is an outage in the monitoring
system you installed to prevent outages.

So **no metric here is labelled by project, model, prompt, user or trace.** The
labels that exist are bounded sets defined in code: `source` is native or otlp,
`provider` is one of three, `retryable` is a boolean, `status` is an HTTP code,
`path` is a route template.

Two of those deserve their own note:

- **`provider`, never `model`.** Model names come from user-authored prompt
  versions. Labelling by model would hand any tenant a way to create unbounded
  series in the platform's own monitoring, simply by naming a model.
- **Route templates, never resolved paths.** `/projects/{project_slug}/traces`,
  not `/projects/acme/traces/4bf92f…`. A request that matches no route is
  recorded as `<unmatched>`, so 404-spraying cannot mint labels either.

A test asserts the rule rather than trusting it: `test_self_metrics.py` walks
the registry and fails if any `lo_*` metric carries a per-tenant label.

### `/metrics` is unauthenticated, and the two decisions are linked

Every other endpoint requires a credential (ADR 0010). This one does not, for
the same reason `/healthz` does not: the scraper is infrastructure, it runs
inside the cluster, and giving Prometheus a platform-operator token to poll
every fifteen seconds would put that credential in a config file on a schedule.

What makes that *safe* rather than merely convenient is the cardinality rule
above — there is no tenant data in the output to leak. The access control is
the NetworkPolicy, which admits only the monitoring namespace.

The two decisions hold each other up, so they are tested together: one test
asserts the endpoint answers without a credential, and the next asserts no
project slug appears in its output. If the second ever fails, the first stops
being a considered trade and becomes a leak.

### Queue depth is reported by the API, not the workers

The arq queue is one shared Redis structure. If every worker replica reported
its depth, Prometheus would hold N identical series and any `sum()` across them
would silently multiply the number — a five-deep queue reading as fifteen with
three workers. The API is a single logical reader of a shared resource, and it
already holds a Redis pool.

It is sampled during the scrape rather than on a timer, so the value is current
rather than up to an interval stale. A failure to reach Redis logs and returns
the rest of the registry: a scrape must not fail because of the outage someone
is using it to debug.

### The worker needed an HTTP server it did not have

The worker consumes from a queue and serves nothing, so there was nothing to
scrape. `start_http_server` runs a minimal WSGI server on a daemon thread — it
cannot block arq's event loop and dies with the process rather than holding
shutdown open — on its own port (9464, the OpenTelemetry Prometheus exporter
convention).

This forced a change worth noting: the worker's NetworkPolicy previously had
**no ingress rules at all**, on the reasoning that nothing should ever connect
to a worker. That is no longer true, and the connection has to be permitted on
the worker's side as well as the scraper's — the both-ends rule that already
cost this project a debugging session (ADR 0011). A worker whose metrics port is
silently firewalled looks exactly like a worker that is not running.

The worker labels its `build_info` with the literal string `lo-worker` rather
than `settings.service_name`. Which binary is running is a fact about the
process, not configuration, and a deployment that forgot `LO_SERVICE_NAME` would
otherwise label worker metrics as the API's and merge two services into one set
of series. This was caught by running it, not by reading it.

### Instrumentation lives at the boundary, not in each implementation

Provider latency and errors are recorded in `GenerationProvider.generate_measured`,
a concrete method on the base class that wraps the abstract `generate`. A new
vendor is instrumented by existing, the same reasoning that put the
sampling-parameter check in one shared place (ADR 0013). Ingest counters live in
`ingest_spans`, which both the native and OTLP paths already funnel through.

### The Grafana dashboard is checked against the registry

`infra/grafana/platform-health.json` is a real dashboard, and a dashboard that
references a renamed metric fails *silently*: it renders, the panel is empty,
and nobody notices until the incident it was meant to show. A test extracts
every `lo_*` name from every panel's PromQL and asserts it exists in the
registry, which turns that into a failure at the moment of the rename.

## Consequences

- The API must stay one process per pod. `prometheus_client` keeps counters in
  process memory, so adding `--workers N` to uvicorn would give each worker its
  own counters and return whichever one answered the scrape. Multiprocess mode
  exists and needs a shared directory and a different registry, so it is a
  deliberate migration rather than something that survives a flag being added.
  This is stated in the module docstring where someone adding that flag will
  read it.
- Prometheus itself is not deployed. The endpoints, the policies and the
  dashboard are, and the $0 constraint means nothing scrapes them in anger yet.

## What production at scale would add

- **Actually running Prometheus**, via kube-prometheus-stack, plus a
  `ServiceMonitor` per workload rather than the plain Services here.
- **Alerting rules as code** — queue depth growing for 15 minutes, 5xx ratio
  above a threshold, provider error rate by retryability — versioned next to
  the dashboard rather than clicked into a UI.
- **Exemplars**, linking a slow histogram bucket to a specific trace id. This
  is the one place a trace identifier legitimately belongs in Prometheus,
  because an exemplar is sampled rather than a label.
- **Multiprocess mode**, if the API ever needs more than one process per pod.
- **A dead-letter gauge.** Failed eval jobs land in `control.dead_letter_jobs`
  and nothing surfaces the depth of that table as a metric yet.
