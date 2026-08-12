"""Eval run endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from lo_api.dependencies import CurrentProject, DbSession
from lo_api.queue import enqueue
from lo_core.evaluators.registry import EvaluatorInfo, available
from lo_core.schemas.evaluation import (
    DeadLetterRead,
    EvalRunCreate,
    EvalRunDetail,
    EvalRunRead,
)
from lo_core.services import evaluation as service

router = APIRouter(tags=["evaluation"])

RunId = Annotated[uuid.UUID, Path(description="Eval run id")]


@router.get(
    "/evaluators",
    response_model=list[EvaluatorInfo],
    summary="List available evaluators and their config schemas",
)
async def list_evaluators() -> list[EvaluatorInfo]:
    """Discovery endpoint.

    Returns each evaluator's JSON Schema so the UI can render a config form
    without hardcoding a copy of the schema that drifts from the backend.
    """
    return available()


@router.post(
    "/projects/{project_slug}/eval/runs",
    response_model=EvalRunRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start an eval run",
)
async def create_run(
    payload: EvalRunCreate,
    project: CurrentProject,
    session: DbSession,
) -> EvalRunRead:
    """Validate, persist, enqueue, return.

    202 rather than 201: the run has been *accepted*, not completed. The caller
    polls `GET /eval/runs/{id}` for progress. Doing the work inline would tie up
    a request worker for minutes and time out at any proxy in front of the API.
    """
    run = await service.create_run(session, project.id, payload)

    # Flush before enqueueing so the row exists if the worker picks the job up
    # immediately — the worker looks the run up by id, and losing that race
    # would make it fail on a row that is about to be committed.
    await session.flush()
    run.job_id = await enqueue("run_eval", str(run.id))
    await session.flush()

    return service.to_read(run)


@router.get(
    "/projects/{project_slug}/eval/runs",
    response_model=list[EvalRunRead],
    summary="List eval runs, newest first",
)
async def list_runs(
    project: CurrentProject,
    session: DbSession,
    run_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EvalRunRead]:
    runs = await service.list_runs(
        session, project.id, status=run_status, limit=limit, offset=offset
    )
    return [service.to_read(r) for r in runs]


@router.get(
    "/projects/{project_slug}/eval/runs/{run_id}",
    response_model=EvalRunDetail,
    summary="Get one eval run with per-example results",
)
async def get_run(
    run_id: RunId,
    project: CurrentProject,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EvalRunDetail:
    run = await service.get_run(session, project.id, run_id)
    return await service.get_run_detail(session, run, limit=limit, offset=offset)


@router.post(
    "/projects/{project_slug}/eval/runs/{run_id}/cancel",
    response_model=EvalRunRead,
    summary="Cancel a pending or running eval run",
)
async def cancel_run(
    run_id: RunId,
    project: CurrentProject,
    session: DbSession,
) -> EvalRunRead:
    run = await service.get_run(session, project.id, run_id)
    return service.to_read(await service.cancel_run(session, run))


@router.get(
    "/dead-letters",
    response_model=list[DeadLetterRead],
    summary="List jobs that exhausted their retries",
)
async def list_dead_letters(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DeadLetterRead]:
    """Operational visibility for failed jobs.

    Not project-scoped: a dead letter is an operator concern, and Phase 8 will
    put this behind an admin-scoped key rather than a project key.
    """
    entries = await service.list_dead_letters(session, limit=limit, offset=offset)
    return [DeadLetterRead.model_validate(e) for e in entries]
