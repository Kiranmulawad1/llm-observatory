"""Arq worker entrypoint.

Run with:  arq lo_worker.main.WorkerSettings

Scaling profile is deliberately different from the API's. Eval jobs are
network-bound fan-out (N examples x M evaluators, each an LLM or embedding
call), so a single process holds a large number of concurrent coroutines rather
than a large number of processes. `max_jobs` is the concurrency knob; in
Kubernetes the replica count is driven by queue depth, not CPU.
"""

from __future__ import annotations

from typing import Any

from arq import cron
from arq.connections import RedisSettings

from lo_core.config import get_settings
from lo_core.db import dispose_engine
from lo_core.logging import configure_logging, get_logger
from lo_worker.tasks.alerting import evaluate_alerts
from lo_worker.tasks.evaluation import run_eval

log = get_logger(__name__)


async def ping(ctx: dict[str, Any]) -> str:
    """Scaffolding smoke task. Replaced by real eval jobs in Phase 3."""
    log.info("worker.ping", job_id=ctx.get("job_id"))
    return "pong"


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging()
    settings.assert_production_safe()
    log.info("worker.startup", environment=settings.environment)


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()
    log.info("worker.shutdown")


class WorkerSettings:
    functions = [ping, run_eval]

    # Alert rules are evaluated once a minute. A minute is the resolution the
    # rules themselves are specified at (window_seconds is >= 60), so a tighter
    # schedule would re-answer an unchanged question; a looser one would delay
    # every alert by the difference.
    cron_jobs = [cron(evaluate_alerts, second=0, run_at_startup=False)]
    on_startup = startup
    on_shutdown = shutdown

    # Retries are per-job; arq re-enqueues with backoff. Jobs that exhaust
    # `max_tries` are written to control.dead_letter_jobs in Phase 3 so a failed
    # eval run is never silently lost.
    max_tries = 3
    job_timeout = 900  # seconds; a large eval batch is legitimately slow
    max_jobs = 20
    # Keep results long enough for the API to report terminal job state.
    keep_result = 3600
    health_check_interval = 30

    # arq reads this off the class dict, so it must be a value rather than a
    # method. Evaluating it at import is fine: this module is only ever loaded
    # as the worker entrypoint, where a bad config should fail immediately.
    redis_settings = RedisSettings.from_dsn(str(get_settings().redis_url))
