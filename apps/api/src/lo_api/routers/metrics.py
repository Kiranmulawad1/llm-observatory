"""Dashboard metrics and alert rules."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field

from lo_api.dependencies import CurrentProject, DbSession
from lo_core.db.models.alerting import ALERT_COMPARISONS, ALERT_METRICS
from lo_core.errors import ValidationError
from lo_core.services import alerts as alert_service
from lo_core.services import metrics as service

router = APIRouter(tags=["metrics"])

# Windows the dashboard offers, mapped to a sensible bucket width. Pairing them
# keeps a caller from asking for 1-minute buckets over 30 days and getting 43,200
# points into a chart 900 pixels wide.
WINDOWS: dict[str, tuple[timedelta, str]] = {
    "15m": (timedelta(minutes=15), "1m"),
    "1h": (timedelta(hours=1), "1m"),
    "6h": (timedelta(hours=6), "5m"),
    "24h": (timedelta(hours=24), "15m"),
    "7d": (timedelta(days=7), "1h"),
    "30d": (timedelta(days=30), "1d"),
}

Window = Annotated[str, Query(description="One of: " + ", ".join(WINDOWS))]


def resolve_window(window: str) -> tuple[datetime, str]:
    entry = WINDOWS.get(window)
    if entry is None:
        raise ValidationError(f"unknown window {window!r}; expected one of {sorted(WINDOWS)}")
    delta, bucket = entry
    return datetime.now(UTC) - delta, bucket


class MetricsResponse(BaseModel):
    window: str
    bucket: str
    since: datetime
    summary: dict[str, Any]
    series: list[dict[str, Any]]


@router.get(
    "/projects/{project_slug}/metrics",
    response_model=MetricsResponse,
    summary="Time-bucketed metrics for the dashboard",
)
async def get_metrics(
    project: CurrentProject,
    session: DbSession,
    window: Window = "1h",
    kind: Annotated[str | None, Query()] = None,
    model: Annotated[str | None, Query()] = None,
    prompt_version_id: Annotated[uuid.UUID | None, Query()] = None,
) -> MetricsResponse:
    """Headline numbers plus the series behind them.

    Returned together in one response because the tiles and the charts must
    describe the same window — fetching them separately invites a UI where the
    summary and the graph disagree because they were computed a second apart.
    """
    since, bucket = resolve_window(window)
    return MetricsResponse(
        window=window,
        bucket=bucket,
        since=since,
        summary=await service.summary(
            session,
            project.id,
            since,
            kind=kind,
            model=model,
            prompt_version_id=prompt_version_id,
        ),
        series=await service.timeseries(
            session,
            project.id,
            since,
            bucket=bucket,
            kind=kind,
            model=model,
            prompt_version_id=prompt_version_id,
        ),
    )


@router.get(
    "/projects/{project_slug}/metrics/breakdown",
    response_model=list[dict[str, Any]],
    summary="Group the window by model or span kind",
)
async def get_breakdown(
    project: CurrentProject,
    session: DbSession,
    window: Window = "24h",
    dimension: Annotated[str, Query(pattern="^(model|kind)$")] = "model",
) -> list[dict[str, Any]]:
    since, _ = resolve_window(window)
    return await service.breakdown(session, project.id, since, dimension=dimension)


# --- Alert rules ----------------------------------------------------------


class AlertRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    metric: str = Field(description="One of: " + ", ".join(ALERT_METRICS))
    comparison: str = Field(default="above", description="above | below")
    threshold: float
    window_seconds: int = Field(default=300, ge=60, le=86_400)
    # Guards against paging on a tiny sample: one failure out of three is a 33%
    # error rate and means nothing.
    min_sample_size: int = Field(default=5, ge=1)
    # The difference between an alert and a pager-spam generator.
    cooldown_seconds: int = Field(default=900, ge=0, le=86_400)
    webhook_url: str = Field(min_length=1)
    description: str | None = None
    enabled: bool = True


class AlertRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: str | None
    metric: str
    comparison: str
    threshold: float
    window_seconds: int
    min_sample_size: int
    cooldown_seconds: int
    webhook_url: str
    enabled: bool
    last_fired_at: datetime | None
    last_value: float | None
    consecutive_failures: int
    created_at: datetime


@router.post(
    "/projects/{project_slug}/alerts",
    response_model=AlertRuleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an alert rule",
)
async def create_alert(
    payload: AlertRuleCreate, project: CurrentProject, session: DbSession
) -> AlertRuleRead:
    if payload.metric not in ALERT_METRICS:
        raise ValidationError(
            f"unknown metric {payload.metric!r}; expected one of {sorted(ALERT_METRICS)}"
        )
    if payload.comparison not in ALERT_COMPARISONS:
        raise ValidationError(f"comparison must be one of {sorted(ALERT_COMPARISONS)}")

    rule = await alert_service.create_rule(session, project.id, **payload.model_dump())
    return AlertRuleRead.model_validate(rule)


@router.get(
    "/projects/{project_slug}/alerts",
    response_model=list[AlertRuleRead],
    summary="List alert rules",
)
async def list_alerts(project: CurrentProject, session: DbSession) -> list[AlertRuleRead]:
    rules = await alert_service.list_rules(session, project.id)
    return [AlertRuleRead.model_validate(r) for r in rules]


@router.delete(
    "/projects/{project_slug}/alerts/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an alert rule",
)
async def delete_alert(
    rule_id: Annotated[uuid.UUID, Path()], project: CurrentProject, session: DbSession
) -> None:
    rule = await alert_service.get_rule(session, project.id, rule_id)
    await alert_service.delete_rule(session, rule)
