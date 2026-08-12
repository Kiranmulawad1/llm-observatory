"""Eval run creation, reads and aggregation.

Execution itself lives in `services/runner.py`. The split is deliberate: this
module runs inside the API request (validate, persist, enqueue — all fast), and
the runner runs inside the worker. Keeping creation cheap is what lets
`POST /eval/runs` return immediately with a run id instead of blocking for the
minutes a real eval takes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.db.models.dataset import DatasetVersion
from lo_core.db.models.evaluation import (
    TERMINAL_STATUSES,
    DeadLetterJob,
    EvalResult,
    EvalRun,
    EvalScore,
)
from lo_core.errors import ConflictError, NotFoundError, ValidationError
from lo_core.evaluators.registry import build_all
from lo_core.providers import EMBEDDING_PROVIDERS, GENERATION_PROVIDERS
from lo_core.providers.pricing import PRICING_CHECKED
from lo_core.schemas.evaluation import (
    EvalResultRead,
    EvalRunCreate,
    EvalRunDetail,
    EvalRunRead,
    EvalScoreRead,
    EvaluatorAggregate,
)
from lo_core.services import datasets as dataset_service
from lo_core.services import prompts as prompt_service


async def create_run(
    session: AsyncSession,
    project_id: uuid.UUID,
    payload: EvalRunCreate,
) -> EvalRun:
    """Validate everything cheap, then persist a pending run.

    All validation happens *here*, before a job is enqueued: an unknown
    evaluator, a bad regex, a model that rejects the prompt's stored
    temperature. Every one of those would otherwise be discovered by the worker
    partway through a run, after the provider has already been paid for the
    examples that did succeed.
    """
    if payload.generation_provider not in GENERATION_PROVIDERS:
        raise ValidationError(
            f"unknown generation provider {payload.generation_provider!r}; "
            f"available: {', '.join(GENERATION_PROVIDERS)}"
        )
    if payload.embedding_provider not in EMBEDDING_PROVIDERS:
        raise ValidationError(
            f"unknown embedding provider {payload.embedding_provider!r}; "
            f"available: {', '.join(EMBEDDING_PROVIDERS)}"
        )

    # Constructs every evaluator, so a bad config fails now rather than later.
    build_all(payload.evaluators)

    dataset = await dataset_service.get_dataset(session, project_id, payload.dataset)
    dataset_version = await dataset_service.get_version(
        session, dataset.id, payload.dataset_version
    )
    if dataset_version.item_count == 0:
        raise ValidationError("dataset version contains no items")

    prompt_version = None
    model = payload.model
    parameters: dict[str, Any] = {}

    if payload.prompt is not None:
        prompt = await prompt_service.get_prompt(session, project_id, payload.prompt)
        prompt_version = await prompt_service.resolve_version(
            session, prompt.id, payload.prompt_version or "production"
        )
        parameters = dict(prompt_version.parameters)
        model = payload.model or parameters.get("model")

    if model is None:
        raise ValidationError(
            "no model specified: set `model` on the run, or record one in the prompt version"
        )

    # The collision described in providers/anthropic_provider.py: a stored
    # prompt version can carry sampling parameters that a newer model rejects.
    if payload.generation_provider == "anthropic":
        from lo_core.providers.anthropic_provider import validate_request

        validate_request(model, parameters)

    run = EvalRun(
        project_id=project_id,
        dataset_version_id=dataset_version.id,
        prompt_version_id=prompt_version.id if prompt_version else None,
        status="pending",
        evaluators=[spec.model_dump() for spec in payload.evaluators],
        provider_config={
            "generation_provider": payload.generation_provider,
            "embedding_provider": payload.embedding_provider,
            "model": model,
            "max_tokens": payload.max_tokens,
            "concurrency": payload.concurrency,
            "parameters": parameters,
        },
        commit_sha=payload.commit_sha,
        triggered_by=payload.triggered_by,
        label=payload.label,
        total_items=dataset_version.item_count,
    )
    session.add(run)
    await session.flush()
    return run


async def get_run(session: AsyncSession, project_id: uuid.UUID, run_id: uuid.UUID) -> EvalRun:
    result = await session.execute(
        select(EvalRun).where(EvalRun.id == run_id, EvalRun.project_id == project_id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise NotFoundError(f"eval run {run_id} not found")
    return run


def _runs_query(project_id: uuid.UUID) -> Select[tuple[EvalRun]]:
    return (
        select(EvalRun).where(EvalRun.project_id == project_id).order_by(EvalRun.created_at.desc())
    )


async def list_runs(
    session: AsyncSession,
    project_id: uuid.UUID,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[EvalRun]:
    stmt = _runs_query(project_id)
    if status is not None:
        stmt = stmt.where(EvalRun.status == status)
    result = await session.execute(stmt.limit(limit).offset(offset))
    return list(result.scalars().all())


def to_read(run: EvalRun) -> EvalRunRead:
    read = EvalRunRead.model_validate(run)
    # A run finished after the pricing snapshot was taken has costs computed
    # from a table that may already be out of date. Flagged rather than hidden,
    # so a dashboard can mark the figure instead of presenting it as exact.
    finished = run.finished_at or run.created_at
    read.pricing_stale = finished.date() > PRICING_CHECKED
    return read


async def get_run_detail(
    session: AsyncSession,
    run: EvalRun,
    limit: int = 200,
    offset: int = 0,
) -> EvalRunDetail:
    """Full run with per-example results and scores, in two queries.

    Not `selectinload` on a relationship: results and scores are both
    `lazy="raise"`, and the pagination has to apply to *results* while scores
    come along for whichever page was selected.
    """
    result_rows = await session.execute(
        select(EvalResult)
        .where(EvalResult.eval_run_id == run.id)
        .order_by(EvalResult.item_index)
        .limit(limit)
        .offset(offset)
    )
    results = list(result_rows.scalars().all())

    scores_by_result: dict[uuid.UUID, list[EvalScoreRead]] = {}
    if results:
        score_rows = await session.execute(
            select(EvalScore)
            .where(EvalScore.eval_result_id.in_([r.id for r in results]))
            .order_by(EvalScore.evaluator)
        )
        for score in score_rows.scalars():
            scores_by_result.setdefault(score.eval_result_id, []).append(
                EvalScoreRead.model_validate(score)
            )

    detail = EvalRunDetail.model_validate(to_read(run))
    detail.results = [
        EvalResultRead(
            id=r.id,
            item_index=r.item_index,
            output=r.output,
            error=r.error,
            latency_ms=r.latency_ms,
            prompt_tokens=r.prompt_tokens,
            completion_tokens=r.completion_tokens,
            cost_usd=r.cost_usd,
            scores=scores_by_result.get(r.id, []),
        )
        for r in results
    ]
    return detail


async def compute_aggregates(
    session: AsyncSession, run_id: uuid.UUID
) -> dict[str, EvaluatorAggregate]:
    """Aggregate scores per evaluator with one GROUP BY.

    This is what the denormalised `eval_run_id` on `eval_scores` buys: no join
    back through `eval_results` to summarise a run.

    SQL aggregates skip NULLs, so unscoreable examples are excluded from the
    mean and counted separately — the whole reason `score` is nullable.
    """
    rows = await session.execute(
        select(
            EvalScore.evaluator,
            func.count().label("total"),
            func.count(EvalScore.score).label("scored"),
            func.avg(EvalScore.score).label("mean"),
            func.min(EvalScore.score).label("minimum"),
            func.max(EvalScore.score).label("maximum"),
            func.count(EvalScore.passed).filter(EvalScore.passed.is_(True)).label("passed"),
            func.count(EvalScore.passed).label("with_verdict"),
        )
        .where(EvalScore.eval_run_id == run_id)
        .group_by(EvalScore.evaluator)
    )

    aggregates: dict[str, EvaluatorAggregate] = {}
    for row in rows:
        aggregates[row.evaluator] = EvaluatorAggregate(
            evaluator=row.evaluator,
            count=row.scored,
            mean=float(row.mean) if row.mean is not None else None,
            minimum=float(row.minimum) if row.minimum is not None else None,
            maximum=float(row.maximum) if row.maximum is not None else None,
            pass_rate=(row.passed / row.with_verdict) if row.with_verdict else None,
            unscoreable=row.total - row.scored,
        )
    return aggregates


async def cancel_run(session: AsyncSession, run: EvalRun) -> EvalRun:
    if run.status in TERMINAL_STATUSES:
        raise ConflictError(f"run is already {run.status}")
    run.status = "cancelled"
    run.finished_at = datetime.now(UTC)
    await session.flush()
    await session.refresh(run)
    return run


async def list_dead_letters(
    session: AsyncSession, limit: int = 50, offset: int = 0
) -> list[DeadLetterJob]:
    result = await session.execute(
        select(DeadLetterJob).order_by(DeadLetterJob.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


async def get_dataset_version(session: AsyncSession, version_id: uuid.UUID) -> DatasetVersion:
    version = await session.get(DatasetVersion, version_id)
    if version is None:
        raise NotFoundError(f"dataset version {version_id} not found")
    return version
