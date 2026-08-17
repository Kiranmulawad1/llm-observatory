"""Review queue, guardrail config, and promotion into eval datasets."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field

from lo_api.dependencies import CurrentProject, DbSession
from lo_core.db.models.review import REVIEW_VERDICTS
from lo_core.errors import ValidationError
from lo_core.schemas.evaluation import DatasetVersionRead
from lo_core.services import review as service

router = APIRouter(tags=["review"])

ItemId = Annotated[uuid.UUID, Path(description="Review item id")]


# --- Schemas --------------------------------------------------------------


class GuardrailConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    sample_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    # Fraction of *clean* sampled traces that still reach a human. This is the
    # instrument that measures what the checks miss.
    control_sample_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    check_pii: bool = True
    check_grounding: bool = True
    check_toxicity: bool = True
    context_field: str = Field(default="context", max_length=64)
    escalate_to_judge: bool = False
    judge_rubric: str | None = None
    judge_model: str | None = None


class GuardrailConfigRead(GuardrailConfigIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    last_scanned_at: datetime | None


class ReviewItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    trace_id: str
    status: str
    sampled_as: str
    findings: list[dict[str, Any]]
    severity: float
    trace_name: str
    inputs: dict[str, Any]
    output: str | None
    context: list[Any] | None
    model: str | None
    verdict: str | None
    label_reason: str | None
    notes: str | None
    corrected_output: str | None
    labeled_by: str | None
    labeled_at: datetime | None
    promoted_at: datetime | None
    created_at: datetime


class LabelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str = Field(description="good | bad")
    reason: str | None = Field(default=None, max_length=64)
    notes: str | None = None
    # Required in practice for a "bad" verdict — promotion rejects a bad label
    # with no correction, because an example with no expected answer cannot be
    # scored by the evaluators you would run against it.
    corrected_output: str | None = None
    labeled_by: str | None = Field(default=None, max_length=200)


class PromoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    dataset: str = Field(description="Slug of the dataset to append to")
    created_by: str | None = None


# --- Config ---------------------------------------------------------------


@router.get(
    "/projects/{project_slug}/guardrails",
    response_model=GuardrailConfigRead | None,
    summary="Get guardrail settings",
)
async def get_guardrails(project: CurrentProject, session: DbSession) -> GuardrailConfigRead | None:
    config = await service.get_config(session, project.id)
    return GuardrailConfigRead.model_validate(config) if config else None


@router.put(
    "/projects/{project_slug}/guardrails",
    response_model=GuardrailConfigRead,
    summary="Enable or update guardrail sampling",
)
async def set_guardrails(
    payload: GuardrailConfigIn, project: CurrentProject, session: DbSession
) -> GuardrailConfigRead:
    """PUT because it is idempotent — one config per project, replaced wholesale."""
    config = await service.upsert_config(session, project.id, **payload.model_dump())
    return GuardrailConfigRead.model_validate(config)


# --- Queue ----------------------------------------------------------------


@router.get(
    "/projects/{project_slug}/review",
    response_model=list[ReviewItemRead],
    summary="The review queue, worst first",
)
async def list_review(
    project: CurrentProject,
    session: DbSession,
    item_status: Annotated[str | None, Query(alias="status")] = "pending",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ReviewItemRead]:
    items = await service.list_items(
        session, project.id, status=item_status, limit=limit, offset=offset
    )
    return [ReviewItemRead.model_validate(i) for i in items]


@router.get(
    "/projects/{project_slug}/review/stats",
    response_model=dict[str, Any],
    summary="Queue counts and the estimated check miss rate",
)
async def review_stats(project: CurrentProject, session: DbSession) -> dict[str, Any]:
    """`estimated_miss_rate` is the number worth watching.

    It is the fraction of traces the checks called clean that a human then
    judged bad — the false-negative rate, and the only reason the control sample
    exists. A rising value means the heuristics need work.
    """
    return await service.queue_stats(session, project.id)


@router.post(
    "/projects/{project_slug}/review/{item_id}/label",
    response_model=ReviewItemRead,
    summary="Record a verdict",
)
async def label(
    item_id: ItemId, payload: LabelIn, project: CurrentProject, session: DbSession
) -> ReviewItemRead:
    if payload.verdict not in REVIEW_VERDICTS:
        raise ValidationError(f"verdict must be one of {sorted(REVIEW_VERDICTS)}")

    item = await service.get_item(session, project.id, item_id)
    labeled = await service.label_item(
        session,
        item,
        verdict=payload.verdict,
        reason=payload.reason,
        notes=payload.notes,
        corrected_output=payload.corrected_output,
        labeled_by=payload.labeled_by,
    )
    return ReviewItemRead.model_validate(labeled)


@router.post(
    "/projects/{project_slug}/review/{item_id}/skip",
    response_model=ReviewItemRead,
    summary="Dismiss an item without a verdict",
)
async def skip(item_id: ItemId, project: CurrentProject, session: DbSession) -> ReviewItemRead:
    """For a false positive worth clearing rather than judging."""
    item = await service.get_item(session, project.id, item_id)
    return ReviewItemRead.model_validate(await service.skip_item(session, item))


# --- The flywheel ---------------------------------------------------------


@router.post(
    "/projects/{project_slug}/review/promote",
    response_model=DatasetVersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Promote labelled items into a dataset version",
)
async def promote(
    payload: PromoteIn, project: CurrentProject, session: DbSession
) -> DatasetVersionRead:
    """Turn reviewed production failures into eval examples.

    Batched deliberately: dataset versions are immutable, so promoting one item
    at a time would create a version per label. One promotion, one version,
    carrying every existing example plus the new ones.
    """
    version = await service.promote_items(
        session,
        project.id,
        item_ids=payload.item_ids,
        dataset_slug=payload.dataset,
        created_by=payload.created_by,
    )
    return DatasetVersionRead.model_validate(version)
