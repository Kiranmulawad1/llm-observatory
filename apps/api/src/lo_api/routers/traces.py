"""Trace ingestion and querying.

Two very different audiences share this file, and they authenticate differently:

  POST /v1/traces      an external application's SDK, holding a project API key
  GET  /projects/...   the dashboard, going through the Next.js BFF

The ingest route is the only endpoint in the platform designed to be called from
outside your own infrastructure, which is why it is the one with a rate limit and
an API key rather than a project slug in the path — the key *is* the project.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from lo_api.dependencies import CurrentProject, DbSession, IngestPrincipal
from lo_core.errors import ForbiddenError, RateLimitError
from lo_core.ratelimit import check_rate_limit
from lo_core.schemas.telemetry import (
    TraceDetail,
    TraceIngestRequest,
    TraceIngestResponse,
    TraceRead,
)
from lo_core.services import traces as service

router = APIRouter(tags=["traces"])

# Default query window. A hypertable query without a time bound scans every
# chunk ever written, so the API supplies one rather than leaving it optional
# and letting an innocent-looking `GET /traces` table-scan a year of data.
DEFAULT_WINDOW = timedelta(hours=24)


@router.post(
    "/v1/traces",
    response_model=TraceIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a batch of spans",
)
async def ingest(
    payload: TraceIngestRequest,
    principal: IngestPrincipal,
    session: DbSession,
    response: Response,
) -> TraceIngestResponse:
    """Accept spans from an instrumented application.

    202, not 201: the spans are accepted and durable, but the trace rollup they
    belong to may still be incomplete — later spans in the same trace are
    expected to arrive in subsequent batches.

    The project comes from the API key, never from the request body. A client
    cannot write into a project it does not hold a key for, which is the whole
    reason this endpoint authenticates rather than taking a project slug.
    """
    # The admin token can ingest on behalf of any project, but it carries no
    # project of its own — so ingestion genuinely requires a project key.
    if principal.key is None:
        raise ForbiddenError("trace ingestion requires a project API key, not the admin token")
    project_id = principal.key.project_id

    # Cost is the span count, not one per request. Otherwise a client could send
    # 500-span batches a thousand times a minute and stay under a request-count
    # limit while writing half a million rows.
    limit = await check_rate_limit(project_id, cost=len(payload.spans))
    response.headers["X-RateLimit-Limit"] = str(limit.limit)
    response.headers["X-RateLimit-Remaining"] = str(limit.remaining)
    if not limit.allowed:
        raise RateLimitError(
            f"ingest rate limit of {limit.limit} spans/min exceeded",
            retry_after=limit.retry_after,
        )

    return await service.ingest_spans(session, project_id, payload.spans)


@router.get(
    "/projects/{project_slug}/traces",
    response_model=list[TraceRead],
    summary="List traces, newest first",
)
async def list_traces(
    project: CurrentProject,
    session: DbSession,
    trace_status: Annotated[str | None, Query(alias="status")] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TraceRead]:
    return await service.list_traces(
        session,
        project.id,
        status=trace_status,
        since=since or datetime.now(UTC) - DEFAULT_WINDOW,
        until=until,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/projects/{project_slug}/traces/{trace_id}",
    response_model=TraceDetail,
    summary="Get one trace with its full span tree",
)
async def get_trace(
    trace_id: Annotated[str, Path(description="32-character hex trace id")],
    project: CurrentProject,
    session: DbSession,
) -> TraceDetail:
    """Return the trace rollup plus its spans assembled into a tree.

    The tree is built server-side so the dashboard, the CLI and any future
    consumer share one implementation of the parent-pointer walk rather than
    each reimplementing it.
    """
    return await service.get_trace(session, project.id, trace_id)
