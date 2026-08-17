"""Dashboard metrics: time-bucketed aggregations over spans and traces.

Everything here is computed **on the fly** from raw rows, using Timescale's
`time_bucket()`. No materialised views, no refresh policies, no staleness.

**Why not continuous aggregates?** They are the right answer eventually and the
wrong answer now. A continuous aggregate is a materialised view Timescale
refreshes in the background — it makes counts and sums O(buckets) instead of
O(rows), which matters enormously once a window covers tens of millions of spans.

It also costs real things: it cannot be created inside a transaction (which
fights Alembic), it introduces a refresh lag so the dashboard is seconds stale,
and percentiles cannot be materialised without the `timescaledb_toolkit`
extension — so `p95` would still scan raw rows and you would maintain two code
paths that must agree.

At this volume a bounded-window scan is a few milliseconds. The upgrade path is
documented in ADR 0008; the trigger is when a one-hour window stops returning in
double-digit milliseconds, not before.

**Every query here is bounded by time.** That is not politeness — an unbounded
query against a hypertable scans every chunk ever written, and the entire point
of partitioning is that a time predicate lets Postgres skip chunks it can prove
are irrelevant.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.db.models.telemetry import Span, Trace

# Bucket widths offered to the dashboard. Constrained rather than free-form
# because the value is interpolated into SQL — and because a caller asking for
# 1-second buckets over 30 days wants 2.6 million rows in a chart.
BUCKET_INTERVALS: dict[str, str] = {
    "1m": "1 minute",
    "5m": "5 minutes",
    "15m": "15 minutes",
    "1h": "1 hour",
    "1d": "1 day",
}


def _bucket_expr(bucket: str) -> Any:
    """`time_bucket()` over the span start time.

    The interval comes from the dict above, never from caller input — this
    string ends up inside SQL, and a lookup table is what keeps it from being an
    injection point.
    """
    interval = BUCKET_INTERVALS.get(bucket)
    if interval is None:
        raise ValueError(f"unknown bucket {bucket!r}; expected one of {sorted(BUCKET_INTERVALS)}")
    return func.time_bucket(text(f"INTERVAL '{interval}'"), Span.started_at)


def _trace_bucket_expr(bucket: str) -> Any:
    interval = BUCKET_INTERVALS.get(bucket)
    if interval is None:
        raise ValueError(f"unknown bucket {bucket!r}; expected one of {sorted(BUCKET_INTERVALS)}")
    return func.time_bucket(text(f"INTERVAL '{interval}'"), Trace.started_at)


def _span_filters(
    project_id: uuid.UUID,
    since: datetime,
    until: datetime | None,
    kind: str | None,
    model: str | None,
    prompt_version_id: uuid.UUID | None,
) -> list[Any]:
    filters: list[Any] = [Span.project_id == project_id, Span.started_at >= since]
    if until is not None:
        filters.append(Span.started_at <= until)
    if kind is not None:
        filters.append(Span.kind == kind)
    if model is not None:
        filters.append(Span.model == model)
    # The join that connects production behaviour back to the registry. "p95
    # latency for prompt v7 versus v6" is only answerable because spans carry
    # the version that produced them.
    if prompt_version_id is not None:
        filters.append(Span.prompt_version_id == prompt_version_id)
    return filters


async def timeseries(
    session: AsyncSession,
    project_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
    bucket: str = "5m",
    kind: str | None = None,
    model: str | None = None,
    prompt_version_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Volume, latency percentiles, error rate and cost per time bucket.

    One query returning every series the dashboard draws. Splitting it into four
    would mean four scans of the same rows and four chances for the buckets to
    disagree at the edges.

    `percentile_cont` is an ordered-set aggregate: it sorts the durations inside
    each bucket and interpolates. That is exact rather than approximate, which
    matters because an approximate p99 that is wrong in the tail is wrong
    precisely where you were looking.
    """
    bucket_col = _bucket_expr(bucket).label("bucket")
    filters = _span_filters(project_id, since, until, kind, model, prompt_version_id)

    stmt: Select[Any] = (
        select(
            bucket_col,
            func.count().label("span_count"),
            func.count().filter(Span.status == "error").label("error_count"),
            func.percentile_cont(0.5).within_group(Span.duration_ms.asc()).label("p50_latency_ms"),
            func.percentile_cont(0.95).within_group(Span.duration_ms.asc()).label("p95_latency_ms"),
            func.percentile_cont(0.99).within_group(Span.duration_ms.asc()).label("p99_latency_ms"),
            func.coalesce(func.sum(Span.cost_usd), 0).label("cost_usd"),
            func.coalesce(func.sum(Span.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(Span.completion_tokens), 0).label("completion_tokens"),
        )
        .where(and_(*filters))
        .group_by(bucket_col)
        .order_by(bucket_col)
    )

    rows = (await session.execute(stmt)).all()
    return [
        {
            "bucket": row.bucket,
            "span_count": row.span_count,
            "error_count": row.error_count,
            # Computed here rather than in the browser so every consumer — the
            # dashboard, an alert rule, a CLI — agrees on the definition.
            "error_rate": (row.error_count / row.span_count) if row.span_count else 0.0,
            "p50_latency_ms": float(row.p50_latency_ms) if row.p50_latency_ms is not None else None,
            "p95_latency_ms": float(row.p95_latency_ms) if row.p95_latency_ms is not None else None,
            "p99_latency_ms": float(row.p99_latency_ms) if row.p99_latency_ms is not None else None,
            "cost_usd": float(row.cost_usd or 0),
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
        }
        for row in rows
    ]


async def summary(
    session: AsyncSession,
    project_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
    kind: str | None = None,
    model: str | None = None,
    prompt_version_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Headline numbers for the window: the stat tiles above the charts."""
    filters = _span_filters(project_id, since, until, kind, model, prompt_version_id)

    row = (
        await session.execute(
            select(
                func.count().label("span_count"),
                func.count(func.distinct(Span.trace_id)).label("trace_count"),
                func.count().filter(Span.status == "error").label("error_count"),
                func.percentile_cont(0.5).within_group(Span.duration_ms.asc()).label("p50"),
                func.percentile_cont(0.95).within_group(Span.duration_ms.asc()).label("p95"),
                func.percentile_cont(0.99).within_group(Span.duration_ms.asc()).label("p99"),
                func.coalesce(func.sum(Span.cost_usd), 0).label("cost_usd"),
                func.coalesce(func.sum(Span.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(Span.completion_tokens), 0).label("completion_tokens"),
            ).where(and_(*filters))
        )
    ).one()

    return {
        "span_count": row.span_count,
        "trace_count": row.trace_count,
        "error_count": row.error_count,
        "error_rate": (row.error_count / row.span_count) if row.span_count else 0.0,
        "p50_latency_ms": float(row.p50) if row.p50 is not None else None,
        "p95_latency_ms": float(row.p95) if row.p95 is not None else None,
        "p99_latency_ms": float(row.p99) if row.p99 is not None else None,
        "cost_usd": float(row.cost_usd or 0),
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
    }


async def breakdown(
    session: AsyncSession,
    project_id: uuid.UUID,
    since: datetime,
    dimension: str = "model",
    until: datetime | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Group the window by model or span kind.

    Answers "which model is eating the budget" and "which step is slowest",
    which are the two questions a cost or latency spike immediately raises.
    """
    column = {"model": Span.model, "kind": Span.kind}.get(dimension)
    if column is None:
        raise ValueError(f"unknown dimension {dimension!r}; expected 'model' or 'kind'")

    filters: list[Any] = [Span.project_id == project_id, Span.started_at >= since]
    if until is not None:
        filters.append(Span.started_at <= until)
    # A null model on a retrieval span is not a category anyone wants a row for.
    if dimension == "model":
        filters.append(Span.model.is_not(None))

    rows = (
        await session.execute(
            select(
                column.label("label"),
                func.count().label("span_count"),
                func.count().filter(Span.status == "error").label("error_count"),
                func.percentile_cont(0.95).within_group(Span.duration_ms.asc()).label("p95"),
                func.coalesce(func.sum(Span.cost_usd), 0).label("cost_usd"),
            )
            .where(and_(*filters))
            .group_by(column)
            .order_by(func.count().desc())
            .limit(limit)
        )
    ).all()

    return [
        {
            "label": row.label,
            "span_count": row.span_count,
            "error_count": row.error_count,
            "error_rate": (row.error_count / row.span_count) if row.span_count else 0.0,
            "p95_latency_ms": float(row.p95) if row.p95 is not None else None,
            "cost_usd": float(row.cost_usd or 0),
        }
        for row in rows
    ]


async def trace_timeseries(
    session: AsyncSession,
    project_id: uuid.UUID,
    since: datetime,
    bucket: str = "5m",
) -> list[dict[str, Any]]:
    """Per-bucket trace volume and end-to-end latency.

    Distinct from the span series: a *trace* is one user-visible request, so its
    p95 is what a customer experiences. A span p95 mixes a 5 ms retrieval with a
    2 s generation and describes nobody's experience.
    """
    bucket_col = _trace_bucket_expr(bucket).label("bucket")

    rows = (
        await session.execute(
            select(
                bucket_col,
                func.count().label("trace_count"),
                func.count().filter(Trace.status == "error").label("error_count"),
                func.percentile_cont(0.95).within_group(Trace.duration_ms.asc()).label("p95"),
                func.coalesce(func.sum(Trace.total_cost_usd), 0).label("cost_usd"),
            )
            .where(Trace.project_id == project_id, Trace.started_at >= since)
            .group_by(bucket_col)
            .order_by(bucket_col)
        )
    ).all()

    return [
        {
            "bucket": row.bucket,
            "trace_count": row.trace_count,
            "error_count": row.error_count,
            "error_rate": (row.error_count / row.trace_count) if row.trace_count else 0.0,
            "p95_latency_ms": float(row.p95) if row.p95 is not None else None,
            "cost_usd": float(row.cost_usd or 0),
        }
        for row in rows
    ]


async def metric_value(
    session: AsyncSession,
    project_id: uuid.UUID,
    metric: str,
    window_seconds: int,
) -> tuple[float | None, int]:
    """Evaluate one alert metric over a window. Returns `(value, sample_size)`.

    Shares the definitions above rather than reimplementing them — an alert that
    computes "error rate" differently from the dashboard is an alert nobody
    trusts, because the graph and the page disagree.

    The sample size is returned so a rule can refuse to fire on three requests
    at 3am, where one failure is a 33% error rate.
    """
    from datetime import UTC

    since = datetime.now(UTC) - timedelta(seconds=window_seconds)

    row = (
        await session.execute(
            select(
                func.count().label("trace_count"),
                func.count().filter(Trace.status == "error").label("error_count"),
                func.percentile_cont(0.95).within_group(Trace.duration_ms.asc()).label("p95"),
                func.percentile_cont(0.99).within_group(Trace.duration_ms.asc()).label("p99"),
                func.coalesce(func.sum(Trace.total_cost_usd), 0).label("cost_usd"),
            ).where(Trace.project_id == project_id, Trace.started_at >= since)
        )
    ).one()

    sample = row.trace_count
    if metric == "error_rate":
        return ((row.error_count / sample) if sample else None), sample
    if metric == "p95_latency_ms":
        return (float(row.p95) if row.p95 is not None else None), sample
    if metric == "p99_latency_ms":
        return (float(row.p99) if row.p99 is not None else None), sample
    if metric == "cost_usd":
        return float(row.cost_usd or 0), sample
    if metric == "trace_count":
        return float(sample), sample
    raise ValueError(f"unknown metric {metric!r}")


async def total_cost(
    session: AsyncSession, project_id: uuid.UUID, since: datetime
) -> Decimal | None:
    return await session.scalar(
        select(func.sum(Trace.total_cost_usd)).where(
            Trace.project_id == project_id, Trace.started_at >= since
        )
    )
