"""Trace ingestion, rollup, and querying."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core import metrics
from lo_core.db.models.telemetry import Span, Trace
from lo_core.errors import NotFoundError
from lo_core.schemas.telemetry import (
    SpanIngest,
    SpanNode,
    TraceDetail,
    TraceIngestResponse,
    TraceRead,
)


async def ingest_spans(
    session: AsyncSession,
    project_id: uuid.UUID,
    spans: list[SpanIngest],
    *,
    # Which wire format this batch arrived on. A metric label, not behaviour —
    # the two paths converge here precisely so they cannot diverge.
    source: str = "native",
) -> TraceIngestResponse:
    """Store a batch of spans and refresh the rollup for every trace they touch.

    **Idempotent by (started_at, span_id).** The SDK retries a failed flush
    without knowing whether the first attempt landed, so the same span will
    genuinely arrive twice. `ON CONFLICT DO NOTHING` makes that a no-op instead
    of an error — and the count is reported back rather than hidden, so a client
    seeing constant duplicates knows its retry logic is misfiring.
    """
    rows = [
        {
            "started_at": s.started_at,
            "span_id": s.span_id,
            "trace_id": s.trace_id,
            "parent_span_id": s.parent_span_id,
            "project_id": project_id,
            "name": s.name,
            "kind": s.kind,
            "status": s.status,
            "ended_at": s.ended_at,
            "duration_ms": s.duration_ms,
            "span_input": s.input,
            "span_output": s.output,
            "span_metadata": s.metadata,
            "model": s.model,
            "prompt_tokens": s.prompt_tokens,
            "completion_tokens": s.completion_tokens,
            "cost_usd": s.cost_usd,
            "prompt_version_id": s.prompt_version_id,
            "error_type": s.error_type,
            "error_message": s.error_message,
        }
        for s in spans
    ]

    stmt = (
        pg_insert(Span)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[Span.started_at, Span.span_id])
        .returning(Span.span_id)
    )
    inserted = len((await session.execute(stmt)).scalars().all())

    metrics.ingest_batches.labels(source=source).inc()
    metrics.spans_ingested.labels(source=source).inc(inserted)
    if len(rows) - inserted:
        # Duplicates are expected — the SDK retries without knowing whether the
        # first attempt landed — but a *rising* rate means retry logic is
        # misfiring, which is invisible without counting them.
        metrics.spans_rejected.labels(source=source, reason="duplicate").inc(len(rows) - inserted)

    trace_ids = {s.trace_id for s in spans}
    for trace_id in trace_ids:
        await refresh_trace(session, project_id, trace_id)

    return TraceIngestResponse(
        accepted=inserted,
        duplicates=len(rows) - inserted,
        traces_touched=len(trace_ids),
    )


async def refresh_trace(session: AsyncSession, project_id: uuid.UUID, trace_id: str) -> None:
    """Recompute one trace's rollup from its spans.

    Recomputed, not patched incrementally. Spans arrive out of order and across
    separate batches — a child can land before its parent, and a late span can
    change the totals after the root has already been written — so incrementally
    adding deltas would drift from the truth in ways nothing would detect.

    The cost is one aggregate query per touched trace per batch. That is
    acceptable because a trace has tens of spans, not millions, and the query is
    bounded by the `(trace_id, started_at)` index.
    """
    aggregate = (
        await session.execute(
            select(
                func.min(Span.started_at).label("started_at"),
                func.max(Span.ended_at).label("ended_at"),
                func.count().label("span_count"),
                func.count().filter(Span.status == "error").label("error_count"),
                func.coalesce(func.sum(Span.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(Span.completion_tokens), 0).label("completion_tokens"),
                func.sum(Span.cost_usd).label("cost_usd"),
            ).where(Span.trace_id == trace_id, Span.project_id == project_id)
        )
    ).one()

    if aggregate.span_count == 0:  # pragma: no cover - called right after insert
        return

    # The root is the span with no parent. Its name and metadata become the
    # trace's, which is what makes a trace list readable ("answer_question")
    # rather than a wall of ids.
    root = (
        await session.execute(
            select(Span)
            .where(
                Span.trace_id == trace_id,
                Span.project_id == project_id,
                Span.parent_span_id.is_(None),
            )
            .order_by(Span.started_at)
            .limit(1)
        )
    ).scalar_one_or_none()

    duration_ms = root.duration_ms if root is not None else None
    if duration_ms is None and aggregate.ended_at is not None:
        # Fall back to the observed window when the root has not arrived yet, so
        # an in-flight trace still shows a sensible duration.
        delta = aggregate.ended_at - aggregate.started_at
        duration_ms = int(delta.total_seconds() * 1000)

    values: dict[str, Any] = {
        "started_at": aggregate.started_at,
        "trace_id": trace_id,
        "project_id": project_id,
        "name": root.name if root is not None else "(root pending)",
        # Any errored span makes the whole trace an error. A request whose
        # retrieval step failed but which still returned something is not a
        # success, and counting it as one is how error-rate dashboards lie.
        "status": "error" if aggregate.error_count else "ok",
        "ended_at": aggregate.ended_at,
        "duration_ms": duration_ms,
        "span_count": aggregate.span_count,
        "error_count": aggregate.error_count,
        "total_prompt_tokens": aggregate.prompt_tokens,
        "total_completion_tokens": aggregate.completion_tokens,
        "total_cost_usd": aggregate.cost_usd,
        "trace_metadata": root.span_metadata if root is not None else {},
    }

    # A rollup's `started_at` is MIN(span.started_at), and that value can move
    # *earlier* when a span from before the current earliest arrives in a later
    # batch — which is routine, because spans complete innermost-first while a
    # batching exporter flushes on a timer.
    #
    # `started_at` is also part of the primary key, because Timescale requires
    # the partitioning column in every unique index. So when the minimum moves,
    # the upsert below no longer matches the existing row and inserts a *second*
    # rollup for the same trace. Both rows then satisfy a lookup by trace_id,
    # and the read path raises MultipleResultsFound — the trace becomes
    # unreadable, which is a strange way for late data to present.
    #
    # Existing rows are therefore read first, so that the superseded ones can be
    # removed after the write, and so that state which does not come from the
    # spans survives being re-keyed.
    existing = (
        await session.execute(
            select(Trace.started_at, Trace.flagged_at).where(
                Trace.trace_id == trace_id, Trace.project_id == project_id
            )
        )
    ).all()

    # `flagged_at` is set by guardrail sampling, not derived from spans. If a
    # flagged trace were re-keyed without carrying it, the trace would silently
    # drop out of the human review queue — losing exactly the traces someone
    # decided were worth looking at.
    flagged_at = next((row.flagged_at for row in existing if row.flagged_at is not None), None)
    if flagged_at is not None:
        values["flagged_at"] = flagged_at

    stmt = pg_insert(Trace).values(values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Trace.started_at, Trace.trace_id],
            set_={k: v for k, v in values.items() if k not in ("started_at", "trace_id")},
        )
    )

    # Insert first, then clear superseded rows, so the row just written is never
    # the one deleted.
    if any(row.started_at != aggregate.started_at for row in existing):
        await session.execute(
            delete(Trace).where(
                Trace.trace_id == trace_id,
                Trace.project_id == project_id,
                Trace.started_at != aggregate.started_at,
            )
        )


# --- Queries --------------------------------------------------------------


async def list_traces(
    session: AsyncSession,
    project_id: uuid.UUID,
    status: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[TraceRead]:
    stmt = select(Trace).where(Trace.project_id == project_id)
    if status is not None:
        stmt = stmt.where(Trace.status == status)
    # Time bounds are what let a hypertable skip whole chunks. A query without
    # one scans every chunk ever written, so the API defaults to a window rather
    # than leaving it optional in practice.
    if since is not None:
        stmt = stmt.where(Trace.started_at >= since)
    if until is not None:
        stmt = stmt.where(Trace.started_at <= until)

    result = await session.execute(
        stmt.order_by(Trace.started_at.desc()).limit(limit).offset(offset)
    )
    return [TraceRead.model_validate(t) for t in result.scalars().all()]


async def get_trace(session: AsyncSession, project_id: uuid.UUID, trace_id: str) -> TraceDetail:
    """Fetch a trace and assemble its span tree."""
    trace = (
        await session.execute(
            select(Trace).where(Trace.project_id == project_id, Trace.trace_id == trace_id)
        )
    ).scalar_one_or_none()
    if trace is None:
        raise NotFoundError(f"trace {trace_id} not found")

    spans = list(
        (
            await session.execute(
                select(Span)
                .where(Span.project_id == project_id, Span.trace_id == trace_id)
                .order_by(Span.started_at)
            )
        )
        .scalars()
        .all()
    )

    root, orphans = build_tree(spans)
    detail = TraceDetail.model_validate(trace)
    detail.root = root
    detail.orphans = orphans
    return detail


def build_tree(spans: list[Span]) -> tuple[SpanNode | None, list[SpanNode]]:
    """Turn flat span rows into a tree.

    One pass to build the nodes, one to link them — O(n), not the O(n²) that
    searching for each span's children would cost.

    A span whose `parent_span_id` names a span that is not in this set is
    returned as an orphan rather than dropped. That happens legitimately: the
    parent may still be buffered in the SDK, or a flush may have been partial.
    Hiding those spans would make a trace look complete when it is not.
    """
    nodes: dict[str, SpanNode] = {s.span_id: SpanNode.model_validate(s) for s in spans}

    root: SpanNode | None = None
    orphans: list[SpanNode] = []

    for span in spans:
        node = nodes[span.span_id]
        if span.parent_span_id is None:
            # Two roots would mean two traces sharing an id. Keep the first and
            # treat the rest as orphans rather than silently discarding one.
            if root is None:
                root = node
            else:
                orphans.append(node)
        else:
            parent = nodes.get(span.parent_span_id)
            if parent is None:
                orphans.append(node)
            else:
                parent.children.append(node)

    return root, orphans


async def trace_cost(
    session: AsyncSession, project_id: uuid.UUID, since: datetime
) -> Decimal | None:
    """Total spend for a project since a point in time."""
    return await session.scalar(
        select(func.sum(Trace.total_cost_usd)).where(
            Trace.project_id == project_id, Trace.started_at >= since
        )
    )
