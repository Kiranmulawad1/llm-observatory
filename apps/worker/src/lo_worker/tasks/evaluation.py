"""Arq task wrapping the eval runner.

The task itself is thin — the engine lives in `lo_core.services.runner` so the
same code is reachable from a test, a CLI or a future scheduler without going
through Redis. What belongs *here* is the queue-specific concern arq does not
provide: the dead-letter path.
"""

from __future__ import annotations

import uuid
from typing import Any

from lo_core.db import session_scope
from lo_core.logging import get_logger
from lo_core.services.runner import execute_run, record_dead_letter

log = get_logger(__name__)

# Must match WorkerSettings.max_tries; used to detect the final attempt.
MAX_TRIES = 3


async def run_eval(ctx: dict[str, Any], run_id: str) -> str:
    """Execute an eval run, dead-lettering it if this was the last attempt.

    arq re-enqueues with backoff on exception and gives up after `max_tries`.
    Without the handler below, that final give-up is silent: the job vanishes
    from Redis and the run sits in `running` forever with nothing explaining
    why. Catching the last attempt is what turns a disappearance into a record.
    """
    attempt: int = ctx.get("job_try", 1)
    job_id: str = ctx.get("job_id", "unknown")

    try:
        status = await execute_run(uuid.UUID(run_id))
    except Exception as exc:
        if attempt >= MAX_TRIES:
            log.error(
                "eval.run.dead_letter",
                run_id=run_id,
                job_id=job_id,
                attempts=attempt,
                error=str(exc),
            )
            # A separate transaction: the runner already rolled back its own,
            # and the dead-letter record must survive regardless.
            async with session_scope() as session:
                await record_dead_letter(
                    session,
                    job_id=job_id,
                    function_name="run_eval",
                    job_args={"run_id": run_id},
                    exc=exc,
                    attempts=attempt,
                    eval_run_id=uuid.UUID(run_id),
                )
            # Swallowed on the final attempt: re-raising would only make arq log
            # the same failure again, and the record above is now the durable
            # account of it.
            return "failed"

        log.warning("eval.run.retry", run_id=run_id, attempt=attempt, error=str(exc))
        raise

    log.info("eval.run.completed", run_id=run_id, status=status)
    return status
