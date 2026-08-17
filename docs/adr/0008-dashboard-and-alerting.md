# ADR 0008 — Dashboard metrics, the BFF, and alerting

**Status:** Accepted (Phase 6)

## Context

Five phases of backend with a starter page for a frontend. This phase builds
every view at once — observability, traces, prompts, evals, comparison, settings
— plus the alerting deferred from Phase 5, which needs the metrics queries this
phase introduces anyway.

## Decision

### Metrics are computed on the fly, not materialised

Every dashboard number comes from a `time_bucket()` query over raw spans,
bounded by the window being displayed.

**Continuous aggregates were considered and deliberately deferred.** They are
TimescaleDB's headline feature and the eventual right answer: a materialised,
auto-refreshing rollup makes counts and sums O(buckets) rather than O(rows),
which matters enormously once a window spans tens of millions of rows.

They also cost real things today:

- A continuous aggregate **cannot be created inside a transaction**, which
  fights Alembic's migration model.
- Refresh happens on a policy, so the dashboard becomes seconds stale — on a
  view whose entire purpose is "what is happening right now".
- **Percentiles cannot be materialised** without the `timescaledb_toolkit`
  extension, which the base image does not ship. So `p95` would still scan raw
  rows, and there would be two code paths that must agree on what a metric means.

At current volume a bounded-window scan returns in single-digit milliseconds.
The trigger to revisit is measured, not guessed: when a one-hour window stops
returning in double-digit milliseconds. At that point counts, sums and cost move
to a continuous aggregate and percentiles stay live — and the two-path
consistency problem becomes worth paying for.

**Every query carries a time bound.** Not politeness: an unbounded query against
a hypertable scans every chunk ever written, which defeats the only reason to
partition. The window and the bucket width are chosen together in one table, so
a caller cannot request 1-minute buckets over 30 days and put 43,200 points into
a chart 900 pixels wide.

### One response for tiles and charts

`GET /metrics` returns the summary and the series together. Fetching them
separately invites a dashboard where the headline number and the graph disagree
because they were computed a second apart, against slightly different windows.

Error rate is computed **server-side** for the same reason: the dashboard, an
alert rule, and any future CLI must all agree on the definition, and three
consumers each dividing their own numerator is three chances to diverge.

### The frontend is a BFF, and the proxy is GET-only

The browser never holds the platform credential. Server Components fetch for the
first paint; a `/api/proxy/*` route handler serves client-side polling with the
key attached server-side.

**That proxy accepts GET only.** A general passthrough forwarding any method
would hand the browser the entire authenticated API surface — precisely what the
BFF pattern exists to prevent. Mutations go through explicit Server Actions that
validate their own input.

### Polling, not WebSockets

Ten-second freshness is what a metrics view needs. Polling has no connection
state, no reconnect path, and no sticky-session problem when the API scales to N
pods behind a load balancer.

A failed poll leaves the last good data on screen rather than blanking the
dashboard — stale numbers beat an empty page during the incident you opened it
for.

SSE is noted as the right answer for a **live trace tail** specifically, where
the payload is a stream of discrete events rather than a periodic snapshot.

### Charts are hand-rolled SVG

The mark specs are precise — 2px strokes, rounded data-ends anchored to the
baseline, a 2px surface gap between adjacent bars, recessive grid, crosshair
tooltips — and bending a charting library's defaults into them is more code than
drawing them. Colours come from CSS custom properties, so both themes resolve
from one definition.

Three rules are non-negotiable and worth stating:

- **One y-axis, always.** p50/p95/p99 share a scale because they are the same
  measure. A second axis is the most common charting mistake — it makes any two
  series look correlated by construction.
- **Categorical colours are assigned in fixed order, never cycled**, and span
  kind maps to a fixed slot — so `retrieval` is the same colour in every trace,
  and colour follows the entity rather than its position in one list.
- **Identity is never carried by colour alone.** Legends for multi-series charts,
  `+`/`−` prefixes on diff lines, arrows plus words on deltas, text on status
  pills.

### Alerting: rules, a cron, and signed webhooks

Evaluated by a worker cron once a minute — not on ingest. Evaluating per span
would run alert queries thousands of times a second to answer a question whose
answer changes once a minute, and would put alert evaluation on the ingest hot
path where a slow rule becomes backpressure on a customer's application.

**Four gates before anything is sent**, in order of cheapness:

1. **Cooldown.** A condition true for an hour fires once, not sixty times. This
   is the difference between an alert and a pager-spam generator, and it is
   checked first because it is free.
2. **Sample size.** One failure out of three is a 33% error rate at 3am and
   means nothing.
3. **Threshold**, strictly — a rule set to the current steady-state value does
   not fire forever.
4. **Delivery**, with failures counted.

**`last_fired_at` is stamped even when delivery fails.** Otherwise a broken
endpoint retries the same alert every evaluation, and a recovered endpoint is hit
with an hour of backlog at once.

**Webhooks are HMAC-signed** over the exact bytes sent. Anyone who learns the URL
could otherwise forge an alert — and alert endpoints routinely page a human or
open a ticket. Signing the serialised bytes rather than a re-serialised dict
means the receiver must verify against the raw body, which is the only way the
signature is meaningful.

**Metrics are a closed vocabulary**, not arbitrary SQL. A rule that can run any
query is a rule that can table-scan production from a cron job.

## Consequences

- **Alert evaluation shares the dashboard's metric definitions.** An alert that
  computed "error rate" differently from the graph is an alert nobody trusts,
  because the page and the pager disagree.
- **A persistently dead webhook disables its rule** after ten consecutive
  failures, visibly. A silent alerting outage is the worst kind.
- **Rules are evaluated independently** — one project's exploding webhook must
  not stop another project's alerts from being checked.
- **`trace_count below N` is a heartbeat check.** The same mechanism that catches
  a spike catches a pipeline that stopped sending entirely, which is the failure
  a threshold-above rule would never see.
- **Trace-level and span-level rates differ, deliberately.** Alerts measure
  traces because a trace is one user-visible request; a span p95 mixes a 5 ms
  retrieval with a 2 s generation and describes nobody's experience.
