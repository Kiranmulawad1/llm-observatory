"""Guardrail configuration and the human review queue.

This is the data flywheel: production traffic is sampled, cheap checks flag
suspicious traces, a human labels them, and the labels become eval examples that
the next prompt change is tested against.

**Review items snapshot the trace rather than referencing it.** That looks like
denormalisation and is deliberate. `telemetry` is append-only time-series under a
retention policy — spans get dropped after 90 days. A review item is control-plane
data that must outlive them: a labelled example is worth more the older it gets,
and a queue full of rows pointing at deleted traces would be worthless. The
`trace_id` is kept for linking back while the trace still exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lo_core.db.base import CONTROL_SCHEMA, ControlBase
from lo_core.db.mixins import TimestampMixin, UUIDPrimaryKey

ReviewStatus = Literal["pending", "labeled", "skipped"]
REVIEW_STATUSES: tuple[str, ...] = ("pending", "labeled", "skipped")

# What a human concluded. Deliberately binary rather than a 1-5 scale: a
# reviewer's job here is to decide whether this output was acceptable, and a
# middle option is where ambiguous cases go to be forgotten.
ReviewVerdict = Literal["good", "bad"]
REVIEW_VERDICTS: tuple[str, ...] = ("good", "bad")

# Why the trace entered the queue.
#   flagged  — a check tripped
#   control  — a clean trace sampled anyway, to measure what the checks miss
SampleReason = Literal["flagged", "control"]
SAMPLE_REASONS: tuple[str, ...] = ("flagged", "control")


class GuardrailConfig(UUIDPrimaryKey, TimestampMixin, ControlBase):
    """Per-project sampling and check settings."""

    __tablename__ = "guardrail_configs"
    __table_args__ = (
        # One config per project. The unique constraint is what lets the sampler
        # upsert without a read-then-write race.
        UniqueConstraint("project_id", name="uq_guardrail_configs_project_id"),
        CheckConstraint("sample_rate >= 0 AND sample_rate <= 1", name="sample_rate_range"),
        CheckConstraint(
            "control_sample_rate >= 0 AND control_sample_rate <= 1",
            name="control_sample_rate_range",
        ),
        {"schema": CONTROL_SCHEMA},
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # Fraction of traces to check at all.
    sample_rate: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.1")

    # Fraction of *clean* sampled traces that still enter the queue.
    #
    # This is the measurement instrument. Reviewing only flagged traces means
    # you only ever see failures your checks already know how to find — a blind
    # spot stays invisible by construction. The control sample is how the
    # false-negative rate becomes observable.
    control_sample_rate: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.05")

    check_pii: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    check_grounding: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    check_toxicity: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    # Where the retrieved context lives on a span's input, for the grounding
    # check. Configurable because every team names it differently.
    context_field: Mapped[str] = mapped_column(String(64), nullable=False, server_default="context")

    # Opt-in judge confirmation on flagged items. Off by default because it
    # costs money per flagged trace, and the heuristics are free.
    escalate_to_judge: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    judge_rubric: Mapped[str | None] = mapped_column(String(64), nullable=True)
    judge_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Watermark: traces started before this have already been considered. Keeps
    # each run of the sampler from re-scanning the whole retention window.
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<GuardrailConfig rate={self.sample_rate}>"


class ReviewItem(UUIDPrimaryKey, TimestampMixin, ControlBase):
    """One trace awaiting, or carrying, a human judgement."""

    __tablename__ = "review_items"
    __table_args__ = (
        # A trace enters the queue at most once. The sampler is idempotent, so a
        # re-run over an overlapping window must not duplicate work for a human.
        UniqueConstraint("project_id", "trace_id", name="uq_review_items_project_id_trace_id"),
        CheckConstraint("status IN " + str(REVIEW_STATUSES), name="status_valid"),
        CheckConstraint(
            "verdict IS NULL OR verdict IN " + str(REVIEW_VERDICTS), name="verdict_valid"
        ),
        CheckConstraint("sampled_as IN " + str(SAMPLE_REASONS), name="sampled_as_valid"),
        # The queue's own query: this project's pending items, worst first.
        Index("ix_review_items_project_id_status_severity", "project_id", "status", "severity"),
        {"schema": CONTROL_SCHEMA},
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # No foreign key: telemetry lives in another schema and may move to another
    # instance entirely (ADR 0003), and its rows are dropped by retention.
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    sampled_as: Mapped[str] = mapped_column(String(16), nullable=False, server_default="flagged")

    # Check output: [{"check": "pii", "severity": 0.8, "detail": {...}}, ...]
    findings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    # Highest severity across findings, denormalised so the queue can order by
    # it without unpacking JSON on every row.
    severity: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")

    # --- the snapshot ---
    # Copied at sampling time so the item survives trace retention. See the
    # module docstring.
    trace_name: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    # --- the human's verdict ---
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    label_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # What the answer *should* have been.
    #
    # Without this a "bad" label produces an eval example with no expected
    # output — unscoreable by exactly the evaluators you would want to run
    # against it. The correction is what makes the flywheel actually turn.
    corrected_output: Mapped[str | None] = mapped_column(Text, nullable=True)

    labeled_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    labeled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- promotion into an eval dataset ---
    # RESTRICT: a dataset version that an item was promoted into records where
    # that example came from, and deleting it would orphan the provenance.
    promoted_to_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.dataset_versions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<ReviewItem {self.trace_id[:8]} {self.status}>"
