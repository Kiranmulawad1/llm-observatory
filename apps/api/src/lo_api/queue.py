"""Arq job enqueueing from the API.

One pool for the process, created lazily and closed on shutdown — the same
lifecycle as the database engine, and for the same reason: a connection per
request would spend a TCP handshake on every enqueue.
"""

from __future__ import annotations

from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from lo_core.config import get_settings

_pool: ArqRedis | None = None


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await create_pool(RedisSettings.from_dsn(str(settings.redis_url)))
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
    _pool = None


async def enqueue(function: str, *args: Any) -> str | None:
    """Enqueue a job and return its id.

    Returns None if arq declined to enqueue (it de-duplicates by job id). The
    caller records whatever comes back on the run, so a run whose job id is null
    is visibly un-enqueued rather than silently stuck.
    """
    pool = await get_pool()
    job = await pool.enqueue_job(function, *args)
    return job.job_id if job is not None else None
