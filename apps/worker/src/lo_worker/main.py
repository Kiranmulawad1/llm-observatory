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
from prometheus_client import start_http_server

from lo_core import metrics
from lo_core.config import get_settings
from lo_core.db import dispose_engine
from lo_core.logging import configure_logging, get_logger
from lo_worker.tasks.alerting import evaluate_alerts
from lo_worker.tasks.evaluation import run_eval
from lo_worker.tasks.sampling import sample_traces

log = get_logger(__name__)


async def ping(ctx: dict[str, Any]) -> str:
    """Scaffolding smoke task. Replaced by real eval jobs in Phase 3."""
    log.info("worker.ping", job_id=ctx.get("job_id"))
    return "pong"


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging()
    settings.assert_production_safe()

    # The worker serves no HTTP of its own, so Prometheus has nothing to scrape
    # unless we give it something. `start_http_server` runs a minimal WSGI
    # server on a daemon thread: it cannot block arq's event loop, and it dies
    # with the process rather than holding shutdown open.
    #
    # A separate port from the API's, and one that is *only* metrics — the
    # worker has no readiness contract to serve and nothing else should be able
    # to reach it. The NetworkPolicy admits the monitoring namespace and
    # nothing else.
    start_http_server(settings.metrics_port)
    # "lo-worker" literally, not settings.service_name. Which binary is running
    # is a fact about this process, not configuration — and a deployment that
    # forgot LO_SERVICE_NAME would label worker metrics as the API's, silently
    # merging two services into one set of series.
    metrics.build_info.labels(service="lo-worker", environment=settings.environment).set(1)

    log.info(
        "worker.startup",
        environment=settings.environment,
        metrics_port=settings.metrics_port,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()
    log.info("worker.shutdown")


class WorkerSettings:
    functions = [ping, run_eval]

    # Alert rules are evaluated once a minute. A minute is the resolution the
    # rules themselves are specified at (window_seconds is >= 60), so a tighter
    # schedule would re-answer an unchanged question; a looser one would delay
    # every alert by the difference.
    cron_jobs = [
        cron(evaluate_alerts, second=0, run_at_startup=False),
        # Every five minutes. Sampling is not time-critical — a human reviews
        # the queue hours later — and a wider window means more useful work per
        # wake-up.
        cron(
            sample_traces,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
            second=30,
            run_at_startup=False,
        ),
    ]
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
