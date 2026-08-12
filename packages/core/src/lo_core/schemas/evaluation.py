"""Wire contracts for datasets and eval runs."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from lo_core.db.models.evaluation import RunStatus
from lo_core.db.models.prompt import SLUG_PATTERN
from lo_core.evaluators.registry import EvaluatorSpec

Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN, min_length=1, max_length=64)]

# --- Datasets -------------------------------------------------------------


class DatasetItemIn(BaseModel):
    """One uploaded example.

    `inputs` is an object of template variables because a prompt version is a
    template — see the DatasetItem model for why a flat string would not work.
    """

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any]
    expected_output: str | None = None
    expected_context: list[Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DatasetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: Slug
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class DatasetVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A version is the *complete* item set, not a delta. Uploading is therefore
    # idempotent in shape: the same file always produces the same version
    # content, and there is no partial-append state to reason about.
    items: list[DatasetItemIn] = Field(min_length=1, max_length=10_000)
    created_by: str | None = Field(default=None, max_length=200)
    change_note: str | None = None


class DatasetItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    item_index: int
    inputs: dict[str, Any]
    expected_output: str | None
    expected_context: list[Any] | None


class DatasetVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dataset_id: uuid.UUID
    version: int
    item_count: int
    content_hash: str
    created_by: str | None
    change_note: str | None
    created_at: datetime


class DatasetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    slug: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    latest_version: int | None = None


# --- Eval runs ------------------------------------------------------------


class EvalRunCreate(BaseModel):
    """Request to start an eval run.

    Every reference is by slug plus an optional version, so a CI job can pin
    exact versions while an interactive user gets the latest.
    """

    model_config = ConfigDict(extra="forbid")

    dataset: Slug
    dataset_version: int | None = Field(
        default=None, description="Defaults to the dataset's latest version."
    )

    # Optional: a run may instead score outputs the caller already has, though
    # generating them from a registered prompt is the common case.
    prompt: Slug | None = None
    prompt_version: str | None = Field(
        default=None, description="Version number or label. Defaults to the latest version."
    )

    evaluators: list[EvaluatorSpec] = Field(min_length=1)

    generation_provider: str = "fake"
    # Overrides the prompt version's stored model, when set.
    model: str | None = None
    max_tokens: int = Field(default=4096, gt=0, le=128_000)
    embedding_provider: str = "fake"

    # The judge runs through the same provider as generation, but on its own
    # model: judging is a different task from the one under test, and pinning it
    # separately is what lets you evaluate a cheap model with a strong judge.
    #
    # Defaults to the strongest tier. A judge is the thing deciding whether a
    # change ships, so its accuracy is worth more than its cost — and a judge
    # that is weaker than the model it grades produces scores nobody trusts.
    judge_model: str = "claude-opus-5"

    # Bounds in-flight provider calls for this run. Capped because it is the
    # main way one run can exhaust a provider rate limit and degrade every other
    # run sharing the worker.
    concurrency: int = Field(default=8, ge=1, le=64)

    commit_sha: str | None = Field(default=None, max_length=40)
    triggered_by: str | None = Field(default=None, max_length=200)
    label: str | None = Field(default=None, max_length=200)


class EvaluatorAggregate(BaseModel):
    """Summary for one evaluator across a run."""

    evaluator: str
    count: int
    mean: float | None
    minimum: float | None
    maximum: float | None
    pass_rate: float | None
    # Examples this evaluator could not score (missing expected output, etc.).
    # Reported separately so a dataset gap never reads as a quality drop.
    unscoreable: int = 0


class EvalScoreRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    evaluator: str
    score: float | None
    passed: bool | None
    detail: dict[str, Any]
    error: str | None


class EvalResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    item_index: int
    output: str | None
    error: str | None
    latency_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: Decimal | None
    scores: list[EvalScoreRead] = Field(default_factory=list)


class EvalRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    dataset_version_id: uuid.UUID
    prompt_version_id: uuid.UUID | None
    status: RunStatus
    evaluators: list[dict[str, Any]]
    provider_config: dict[str, Any]
    commit_sha: str | None
    triggered_by: str | None
    label: str | None
    total_items: int
    completed_items: int
    failed_items: int
    aggregate_scores: dict[str, Any]
    error: str | None
    job_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime

    # True when any cost figure was computed from a pricing table older than the
    # run. Surfaced rather than hidden so a dashboard can flag it instead of
    # presenting a stale number as authoritative.
    pricing_stale: bool = False


class EvalRunDetail(EvalRunRead):
    results: list[EvalResultRead] = Field(default_factory=list)


class DeadLetterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: str
    function_name: str
    exception_type: str
    exception_message: str
    attempts: int
    eval_run_id: uuid.UUID | None
    replayed_at: datetime | None
    created_at: datetime


ComparisonChange = Literal["improved", "regressed", "unchanged", "added", "removed"]
Alignment = Literal["identity", "positional"]


class RunSummary(BaseModel):
    """The identity of a run, for the header of a comparison.

    Carries every pinned version, because a score delta is only interpretable
    against what actually differed between the two runs.
    """

    id: uuid.UUID
    status: RunStatus
    label: str | None
    commit_sha: str | None
    dataset_version_id: uuid.UUID
    prompt_version_id: uuid.UUID | None
    judge_prompt_version_id: uuid.UUID | None
    model: str | None
    total_items: int
    completed_items: int
    failed_items: int
    created_at: datetime


class EvaluatorDelta(BaseModel):
    evaluator: str
    baseline_mean: float | None
    candidate_mean: float | None
    delta: float | None
    change: ComparisonChange
    baseline_pass_rate: float | None = None
    candidate_pass_rate: float | None = None


class ExampleComparison(BaseModel):
    item_index: int
    dataset_item_id: uuid.UUID
    change: ComparisonChange
    baseline_output: str | None
    candidate_output: str | None
    baseline_scores: dict[str, float | None] = Field(default_factory=dict)
    candidate_scores: dict[str, float | None] = Field(default_factory=dict)
    score_deltas: dict[str, float | None] = Field(default_factory=dict)
    evaluator_changes: dict[str, ComparisonChange] = Field(default_factory=dict)


class RunComparison(BaseModel):
    baseline: RunSummary
    candidate: RunSummary
    alignment: Alignment
    # Everything that differed between the runs besides the scores themselves.
    # Surfaced rather than buried: a rubric change and a model change produce
    # identical-looking score movement.
    warnings: list[str] = Field(default_factory=list)
    evaluators: list[EvaluatorDelta] = Field(default_factory=list)
    examples: list[ExampleComparison] = Field(default_factory=list)
    regressed_count: int = 0
    improved_count: int = 0
