"""Traces and spans — the `telemetry` schema.

Everything before this lived in `control`: low volume, transactional, foreign
keys everywhere. Telemetry is the opposite, and the schema reflects that.

### The span model is OpenTelemetry's, deliberately

A trace is a tree of spans, stored **flat**: every span carries `trace_id` and a
nullable `parent_span_id`, and the tree is reconstructed by following parent
pointers. Ids follow W3C Trace Context — 32 hex characters for a trace, 16 for a
span.

Adopting the standard rather than inventing UUID-based ids costs nothing and buys
interoperability: a team already running OpenTelemetry has ids that join up with
ours, and can export elsewhere later without a migration. An id scheme of our own
would foreclose that for no benefit.

Storing the tree flat rather than as nested JSON is what makes a span queryable —
"p95 latency of every retrieval span across the fleet" is a `WHERE kind =
'retrieval'`, not a JSON traversal of every trace.

### Why spans are a hypertable, and what that costs

`telemetry.spans` is a TimescaleDB hypertable partitioned on `started_at`. Time
partitioning means old chunks can be compressed or dropped wholesale, and queries
with a time bound skip every chunk outside it — which matters because a
dashboard query is almost always "the last hour".

**The constraint that follows:** Timescale requires the partitioning column to be
part of every unique index, so spans get a composite primary key
`(started_at, span_id)` instead of the plain UUID primary key every table in
`control` uses. That inconsistency is not an oversight; it is the price of
partitioning, and it is why ADR 0003 argued for keeping the two schemas separate
in the first place.

### Why a `traces` table exists at all

It is a rollup — duration, cost, token totals, status — recomputed from the spans
on ingest. Deriving those on every dashboard read would aggregate raw spans per
page view, and the span table is the one that grows without bound.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lo_core.db.base import TELEMETRY_SCHEMA, TelemetryBase
from lo_core.db.mixins import CreatedAtMixin

# What an operation *is*. Free-form would make the dashboard's "slowest retrieval
# step" query impossible to write, and an enum on the database would need a
# migration every time someone instruments something new — so it is a constrained
# vocabulary checked in the application, stored as text.
SpanKind = Literal[
    "chain",  # a composite step; the usual root
    "llm",  # a model call
    "retrieval",  # a vector/keyword search
    "rerank",  # reordering retrieved documents
    "tool",  # a function/tool invocation
    "embedding",  # an embedding call
    "guardrail",  # a safety or validation check
    "other",
]

SPAN_KINDS: tuple[str, ...] = (
    "chain",
    "llm",
    "retrieval",
    "rerank",
    "tool",
    "embedding",
    "guardrail",
    "other",
)

SpanStatus = Literal["ok", "error"]
SPAN_STATUSES: tuple[str, ...] = ("ok", "error")

# W3C Trace Context: 16 bytes hex for a trace, 8 bytes hex for a span.
TRACE_ID_LENGTH = 32
SPAN_ID_LENGTH = 16


class Span(CreatedAtMixin, TelemetryBase):
    """One operation inside a trace."""

    __tablename__ = "spans"
    __table_args__ = (
        # `started_at` leads the primary key because it is the partitioning
        # column and Timescale requires it in every unique index. It also makes
        # the key naturally time-ordered, so inserts append to the rightmost
        # index leaf instead of scattering the way a random UUID would.
        PrimaryKeyConstraint("started_at", "span_id", name="pk_spans"),
        CheckConstraint("kind IN " + str(SPAN_KINDS), name="kind_valid"),
        CheckConstraint("status IN " + str(SPAN_STATUSES), name="status_valid"),
        CheckConstraint("duration_ms >= 0", name="duration_non_negative"),
        # Fetching one trace's tree — the single most common query.
        Index("ix_spans_trace_id_started_at", "trace_id", "started_at"),
        # The dashboard's queries: "this project's traffic over the last hour",
        # optionally narrowed to one kind of operation.
        Index("ix_spans_project_id_started_at", "project_id", "started_at"),
        Index("ix_spans_project_id_kind_started_at", "project_id", "kind", "started_at"),
        {"schema": TELEMETRY_SCHEMA},
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    span_id: Mapped[str] = mapped_column(String(SPAN_ID_LENGTH), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(TRACE_ID_LENGTH), nullable=False)
    # Null on the root span. This single column is the entire tree structure.
    parent_span_id: Mapped[str | None] = mapped_column(String(SPAN_ID_LENGTH), nullable=True)

    # Denormalised from the API key rather than a foreign key.
    #
    # No FK to control.projects on purpose: an append-only table taking thousands
    # of inserts a second should not pay a referential integrity check per row,
    # and this schema is meant to be movable to its own instance (ADR 0003),
    # where a cross-database foreign key could not exist anyway.
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default="other")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ok")

    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Computed by the SDK from a monotonic clock rather than by subtracting
    # timestamps here: wall-clock time can jump backwards mid-span (NTP), which
    # would otherwise produce negative durations.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Payloads. JSONB rather than columns because the shape genuinely differs by
    # kind — a retrieval span's output is a document list, an llm span's is text.
    span_input: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    span_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    span_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    # Model call details. Null on non-llm spans; promoted to real columns rather
    # than left in metadata because every cost and token dashboard groups by them.
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)

    # Which registered prompt version produced this call, when the caller knows.
    # This is the join that connects production behaviour back to the registry —
    # "p95 latency for prompt v7" is only answerable because of this column.
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    error_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<Span {self.name} {self.kind}>"


class Trace(CreatedAtMixin, TelemetryBase):
    """Rollup of one trace: its root span plus totals across the tree.

    Recomputed from spans on ingest rather than patched incrementally, because
    spans arrive out of order and across separate batches — a child can land
    before its parent, and a late span can change the totals after the root has
    already been written.
    """

    __tablename__ = "traces"
    __table_args__ = (
        PrimaryKeyConstraint("started_at", "trace_id", name="pk_traces"),
        CheckConstraint("status IN " + str(SPAN_STATUSES), name="status_valid"),
        # The dashboard's list view: recent traces for a project, newest first,
        # filterable by status.
        Index("ix_traces_project_id_started_at", "project_id", "started_at"),
        Index("ix_traces_project_id_status_started_at", "project_id", "status", "started_at"),
        {"schema": TELEMETRY_SCHEMA},
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(TRACE_ID_LENGTH), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # "error" if *any* span in the tree errored. A trace whose retrieval step
    # failed but which still returned something is not a success, and reporting
    # it as one is how error-rate dashboards end up lying.
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ok")

    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    span_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    total_prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    total_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)

    # Copied off the root span so the list view can filter without a join.
    trace_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    # Set in Phase 7 when sampling flags a trace for human review.
    flagged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<Trace {self.trace_id[:8]} {self.status}>"
