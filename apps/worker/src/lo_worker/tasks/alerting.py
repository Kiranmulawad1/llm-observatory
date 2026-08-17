"""Periodic alert evaluation.

Runs on a cron rather than being triggered by ingestion. Evaluating on every
span write would mean running alert queries thousands of times a second to
answer a question whose answer changes once a minute at most — and it would put
alert evaluation on the ingest hot path, where a slow rule would become
backpressure on a customer's application.
"""

from __future__ import annotations

from typing import Any

from lo_core.db import session_scope
from lo_core.logging import get_logger
from lo_core.services import alerts as alert_service

log = get_logger(__name__)


async def evaluate_alerts(ctx: dict[str, Any]) -> int:
    """Evaluate every enabled rule across every project."""
    async with session_scope() as session:
        fired = await alert_service.evaluate_all(session)

    if fired:
        log.info("alerts.evaluated", fired=fired)
    return fired
