"""Liveness and readiness probes.

These are deliberately different, and the difference matters in Kubernetes:

  /healthz (liveness)  — "is this process wedged?" Checks nothing external.
                         If it consulted Postgres, a database outage would fail
                         every pod's liveness probe and the kubelet would restart
                         the entire fleet in a loop, turning a recoverable
                         dependency blip into a full outage.

  /readyz (readiness)  — "can this pod serve traffic right now?" Checks the
                         dependencies it genuinely needs. Failing here pulls the
                         pod out of the Service endpoints without killing it, so
                         it rejoins automatically once the dependency recovers.
"""

from __future__ import annotations

from typing import Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from lo_core.config import get_settings
from lo_core.db import get_engine

router = APIRouter(tags=["health"])

CheckStatus = Literal["ok", "error"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, CheckStatus]


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/readyz", summary="Readiness probe", response_model=ReadinessResponse)
async def readyz(response: Response) -> ReadinessResponse:
    settings = get_settings()
    checks: dict[str, CheckStatus] = {}

    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "error"

    # redis-py ships `from_url` untyped; the annotation restores strictness
    # for everything downstream of this call.
    client: aioredis.Redis = aioredis.from_url(str(settings.redis_url))  # type: ignore[no-untyped-call]
    try:
        await client.ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "error"
    finally:
        await client.aclose()

    healthy = all(v == "ok" for v in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ready" if healthy else "degraded", checks=checks)
