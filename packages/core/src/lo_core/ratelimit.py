"""Per-project rate limiting for trace ingestion, backed by Redis.

Ingestion is the one endpoint an external application hammers, so it is the one
that needs a ceiling. Without it a single misconfigured client — a retry loop, a
flush interval of zero — degrades the API for every other project sharing it.

**A sliding window over a fixed one.** A fixed window resets on a boundary, so a
client can send its full quota at 11:59:59 and again at 12:00:00 — twice the
intended rate, right when the window turns over. This keeps the last window's
worth of timestamps and expires them continuously, so the limit holds at every
instant rather than on average.

**Failure is open, not closed.** If Redis is unavailable the request is allowed
through. A rate limiter is a protection against abuse, not a correctness
mechanism — refusing all telemetry because the limiter is down would turn a
degraded dependency into an outage, and losing observability data is exactly the
wrong thing to do during an incident.
"""

from __future__ import annotations

import time
import uuid

import redis.asyncio as aioredis

from lo_core.config import get_settings
from lo_core.logging import get_logger

log = get_logger(__name__)

# Fallback when no limit is passed and settings are unavailable. The real
# default lives in `Settings.ingest_rate_limit_per_minute`, so a deployment can
# raise or lower it without a code change — see the comment there.
DEFAULT_LIMIT = 6000
WINDOW_SECONDS = 60

_client: aioredis.Redis | None = None


def get_client() -> aioredis.Redis:
    global _client
    if _client is None:
        settings = get_settings()
        _client = aioredis.from_url(str(settings.redis_url))  # type: ignore[no-untyped-call]
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


class RateLimitResult:
    __slots__ = ("allowed", "limit", "remaining", "retry_after")

    def __init__(self, allowed: bool, limit: int, remaining: int, retry_after: int) -> None:
        self.allowed = allowed
        self.limit = limit
        self.remaining = remaining
        self.retry_after = retry_after


async def check_rate_limit(
    project_id: uuid.UUID,
    cost: int = 1,
    limit: int | None = None,
) -> RateLimitResult:
    """Consume `cost` units from the project's window.

    `cost` is the number of spans in the batch, not 1 per request — otherwise a
    client could send 500-span batches a thousand times a minute and stay inside
    a request-count limit while writing half a million rows.

    `limit` defaults to the configured ceiling rather than to a constant, so a
    deployment can size it for its own tenants.
    """
    if limit is None:
        limit = get_settings().ingest_rate_limit_per_minute

    key = f"ratelimit:ingest:{project_id}"
    now = time.time()
    window_start = now - WINDOW_SECONDS

    try:
        client = get_client()
        # One round trip. Interleaving these as separate awaits would let two
        # concurrent requests both read a stale count and both be allowed.
        async with client.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, 0, window_start)
            pipe.zcard(key)
            pipe.expire(key, WINDOW_SECONDS * 2)
            _, used, _ = await pipe.execute()

        used = int(used)
        if used + cost > limit:
            return RateLimitResult(
                allowed=False,
                limit=limit,
                remaining=max(0, limit - used),
                retry_after=WINDOW_SECONDS,
            )

        # One member per unit, so a batch of N consumes N. Members must be unique
        # or the sorted set would collapse them into one entry and undercount.
        async with client.pipeline(transaction=True) as pipe:
            for index in range(cost):
                pipe.zadd(key, {f"{now}:{index}:{uuid.uuid4().hex[:8]}": now})
            pipe.expire(key, WINDOW_SECONDS * 2)
            await pipe.execute()

        return RateLimitResult(
            allowed=True, limit=limit, remaining=max(0, limit - used - cost), retry_after=0
        )

    except Exception as exc:
        # Fail open. See the module docstring: telemetry loss during a Redis
        # outage is worse than the abuse this prevents.
        log.warning("ratelimit.unavailable", error=str(exc))
        return RateLimitResult(allowed=True, limit=limit, remaining=limit, retry_after=0)
