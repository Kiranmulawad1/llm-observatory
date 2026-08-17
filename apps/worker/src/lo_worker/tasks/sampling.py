"""Periodic guardrail sampling.

Runs on a cron rather than at ingest, for the same reason alerting does:
checking traces on the write path would put heuristic evaluation — regexes over
model output, string searches over retrieved context — into a customer's request
latency. The sampler reads recent traces afterwards, off the hot path entirely.

Every five minutes rather than every minute. Sampling is not time-critical (a
human reviews the queue hours later), and a wider window means each run does
more useful work per wake-up.
"""

from __future__ import annotations

from typing import Any

from lo_core.db import session_scope
from lo_core.logging import get_logger
from lo_core.services import review as review_service

log = get_logger(__name__)


async def sample_traces(ctx: dict[str, Any]) -> int:
    """Sample and check recent traces for every project with guardrails on."""
    async with session_scope() as session:
        queued = await review_service.sample_all(session)

    if queued:
        log.info("guardrails.queued", items=queued)
    return queued
