"""Eval runs, per-example results, per-evaluator scores, and the dead-letter queue.

The split across three tables is deliberate:

  eval_runs     one row per run — what was run, against what, and how it ended
  eval_results  one row per (run, dataset item) — the generation, once
  eval_scores   one row per (run, item, evaluator) — the scores

Storing scores as a JSON blob on the result row would be simpler to write and
much worse to read: aggregate queries ("mean faithfulness for this run", "which
examples regressed between run 38 and 41") become JSONB gymnastics instead of a
GROUP BY, and adding an evaluator to an existing run means rewriting rows.
Generation output lives on `eval_results` rather than being duplicated per
evaluator, because the model is called once and scored many times.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal  # runtime import: SQLAlchemy evaluates the annotation
from typing import Any, Literal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lo_core.db.base import CONTROL_SCHEMA, ControlBase
from lo_core.db.mixins import CreatedAtMixin, TimestampMixin, UUIDPrimaryKey

# Terminal states are `succeeded`, `failed` and `cancelled`. `partial` is also
# terminal: the run finished but some examples errored, which is a genuinely
# different outcome from "the run itself failed" and should not be flattened
# into either success or failure.
RunStatus = Literal["pending", "running", "succeeded", "partial", "failed", "cancelled"]

RUN_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "succeeded",
    "partial",
    "failed",
    "cancelled",
)
TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "partial", "failed", "cancelled"})


class EvalRun(UUIDPrimaryKey, TimestampMixin, ControlBase):
    """One execution of a set of evaluators over a dataset version."""

    __tablename__ = "eval_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN " + str(RUN_STATUSES),
            name="status_valid",
        ),
        CheckConstraint("total_items >= 0", name="total_items_non_negative"),
        # The dashboard's primary query is "recent runs for this project", and
        # the comparison view's is "runs for this dataset version". Both are
        # newest-first.
        Index("ix_eval_runs_project_id_created_at", "project_id", "created_at"),
        Index("ix_eval_runs_dataset_version_id_created_at", "dataset_version_id", "created_at"),
        {"schema": CONTROL_SCHEMA},
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # RESTRICT on both: a run is a historical record of what was evaluated, and
    # deleting the dataset version or prompt version out from under it would
    # leave a result nobody can interpret. Versions are append-only anyway, so
    # this only fires on a manual cleanup mistake.
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.dataset_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Nullable: a run may score outputs supplied by the caller rather than
    # generating them from a registered prompt.
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.prompt_versions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")

    # The evaluator specs as requested, stored verbatim. Reproducing a run means
    # knowing the evaluator *configuration* (regex pattern, similarity threshold),
    # not just the evaluator names.
    evaluators: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)

    # Provider and decoding settings actually used, after merging the prompt
    # version's parameters with any per-run override.
    provider_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    # Provenance. `commit_sha` is what makes a regression bisectable back to the
    # code change that caused it.
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    triggered_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)

    total_items: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # {evaluator_name: {"mean": .., "min": .., "max": .., "pass_rate": .., "count": ..}}
    # Computed once when the run reaches a terminal state. Recomputing on every
    # dashboard read would mean aggregating thousands of score rows per page view.
    aggregate_scores: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    # Populated only when the run itself failed, as opposed to individual items.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Arq job id, so the API can report queue state for a run still pending.
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    results: Mapped[list[EvalResult]] = relationship(
        back_populates="eval_run",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<EvalRun {self.id} {self.status}>"


class EvalResult(UUIDPrimaryKey, CreatedAtMixin, ControlBase):
    """The generation for one dataset item within one run."""

    __tablename__ = "eval_results"
    __table_args__ = (
        # The resume key. A retried job upserts on this pair, so an item already
        # generated is never paid for twice — which matters when a run is 500
        # LLM calls and the worker died on number 480.
        UniqueConstraint(
            "eval_run_id", "dataset_item_id", name="uq_eval_results_run_id_dataset_item_id"
        ),
        Index("ix_eval_results_eval_run_id_item_index", "eval_run_id", "item_index"),
        {"schema": CONTROL_SCHEMA},
    )

    eval_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.eval_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dataset_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.dataset_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)

    # Exactly what was sent to the provider, post-render. Without this, debugging
    # a bad score means guessing at how the template resolved.
    rendered_messages: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set when generation failed for this item. The run continues; one provider
    # timeout should not discard the other 499 results.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Numeric, not float. Money in binary floating point accumulates error over
    # thousands of rows, and "total spend" is a number people reconcile against
    # a provider invoice.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)

    eval_run: Mapped[EvalRun] = relationship(back_populates="results", lazy="raise")
    scores: Mapped[list[EvalScore]] = relationship(
        back_populates="result",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<EvalResult #{self.item_index}>"


class EvalScore(UUIDPrimaryKey, CreatedAtMixin, ControlBase):
    """One evaluator's verdict on one result."""

    __tablename__ = "eval_scores"
    __table_args__ = (
        UniqueConstraint("eval_result_id", "evaluator", name="uq_eval_scores_result_id_evaluator"),
        CheckConstraint("score >= 0.0 AND score <= 1.0", name="score_normalised"),
        # `eval_run_id` is denormalised onto this table purely so aggregation
        # ("mean score per evaluator for run X") is a single indexed GROUP BY
        # rather than a join back through eval_results.
        Index("ix_eval_scores_eval_run_id_evaluator", "eval_run_id", "evaluator"),
        {"schema": CONTROL_SCHEMA},
    )

    eval_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.eval_results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    eval_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.eval_runs.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The evaluator's registered name, e.g. "exact_match". Not a foreign key:
    # evaluators are code, not rows, and a run must stay readable after an
    # evaluator is renamed or removed from the registry.
    evaluator: Mapped[str] = mapped_column(String(64), nullable=False)

    # Always normalised to 0.0-1.0 so scores from different evaluator families
    # (boolean match, cosine similarity, judge rubric) are comparable and can
    # share one aggregation path.
    #
    # Nullable, paired with `error` below, to distinguish "scored badly" from
    # "could not be scored". An exact-match evaluator against an item with no
    # expected output is unscoreable, and recording that as 0.0 would silently
    # drag down the run's mean and make a dataset gap look like a quality
    # regression. SQL aggregates skip NULLs, so the mean stays honest.
    score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Why this evaluator produced no score. Null on success.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Null when the evaluator has no threshold notion. Kept separate from `score`
    # so "did it pass" survives a later threshold change being applied in the UI.
    passed: Mapped[bool | None] = mapped_column(nullable=True)

    # Evaluator-specific evidence: the matched group, the cosine value, the
    # judge's reasoning. This is what makes a low score actionable.
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")

    result: Mapped[EvalResult] = relationship(back_populates="scores", lazy="raise")

    def __repr__(self) -> str:
        return f"<EvalScore {self.evaluator}={self.score}>"


class DeadLetterJob(UUIDPrimaryKey, CreatedAtMixin, ControlBase):
    """A job that exhausted its retries.

    ADR 0002 chose Arq partly knowing it has no built-in dead-letter queue, so
    this table is that mechanism. A broker-level DLQ would only hold the opaque
    payload; this holds the payload *and* the final exception *and* a pointer to
    the affected run, which is what makes a failure diagnosable and replayable
    rather than just visible.
    """

    __tablename__ = "dead_letter_jobs"
    __table_args__ = (
        Index("ix_dead_letter_jobs_replayed_at_created_at", "replayed_at", "created_at"),
        {"schema": CONTROL_SCHEMA},
    )

    job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    function_name: Mapped[str] = mapped_column(String(128), nullable=False)

    # The arguments needed to re-enqueue. These are internal identifiers, never
    # credentials — the worker resolves secrets from configuration at run time,
    # so replaying a job cannot leak one out of this table.
    job_args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")

    exception_type: Mapped[str] = mapped_column(String(200), nullable=False)
    exception_message: Mapped[str] = mapped_column(Text, nullable=False)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # Nullable link back to the run this job was executing, so the API can show
    # "this run died because of X" instead of just leaving it stuck.
    eval_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.eval_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    replayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<DeadLetterJob {self.function_name} {self.exception_type}>"
