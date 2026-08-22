"""The eval execution engine.

Runs inside the worker. Four properties matter more than throughput, and each
one shapes the code below:

**Isolation per example.** One provider timeout must not discard the other 499
results. Every example is executed in its own transaction and its own
try/except; a failure writes an error row and the run continues.

**Resumability.** Retrying a run that died on example 480 must not re-pay for
the 479 that already succeeded. Completed examples are loaded up front and
skipped, keyed on the `(eval_run_id, dataset_item_id)` unique constraint.

**Bounded concurrency.** Examples are I/O-bound, so they run concurrently — but
behind a semaphore. Unbounded `asyncio.gather` over 500 examples would open 500
simultaneous provider connections and trip a rate limit, turning a slow run into
a failed one.

**A session per task.** A SQLAlchemy `AsyncSession` is not safe for concurrent
use; sharing one across gathered tasks corrupts its state in ways that surface as
baffling errors far from the cause. Each concurrent example therefore opens its
own short transaction.
"""

from __future__ import annotations

import asyncio
import traceback
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core import metrics
from lo_core.db import session_scope
from lo_core.db.models.dataset import DatasetItem
from lo_core.db.models.evaluation import DeadLetterJob, EvalResult, EvalRun, EvalScore
from lo_core.db.models.prompt import PromptVersion
from lo_core.evaluators.base import EvaluationSample, Evaluator, UnscoreableError
from lo_core.evaluators.judge import JudgeEvaluator
from lo_core.evaluators.registry import EvaluatorSpec, build_all
from lo_core.evaluators.similarity import EmbeddingSimilarityEvaluator
from lo_core.logging import get_logger
from lo_core.providers import (
    GenerationProvider,
    get_embedding_provider,
    get_generation_provider,
)
from lo_core.providers.base import GenerationRequest
from lo_core.schemas.prompt import Message, RenderedMessage
from lo_core.services.evaluation import compute_aggregates
from lo_core.templating import render_messages

log = get_logger(__name__)


async def execute_run(run_id: uuid.UUID) -> str:
    """Execute an eval run end to end. Returns the terminal status.

    Raises on a *run-level* failure so arq retries the job; per-example failures
    are recorded and never raise.
    """
    async with session_scope() as session:
        run = await session.get(EvalRun, run_id)
        if run is None:
            raise RuntimeError(f"eval run {run_id} not found")
        if run.status in ("succeeded", "failed", "cancelled"):
            # A retry of an already-finished run. Idempotent by design: arq can
            # redeliver a job after the worker died between completing the work
            # and acknowledging it.
            return run.status

        run.status = "running"
        if run.started_at is None:
            run.started_at = datetime.now(UTC)
        config = dict(run.provider_config)
        specs = [EvaluatorSpec.model_validate(spec) for spec in run.evaluators]
        dataset_version_id = run.dataset_version_id
        prompt_version_id = run.prompt_version_id

        # Loaded here, inside the same session, because the judge evaluator has
        # no database access of its own — it is handed a resolved template.
        judge_template: list[Message] | None = None
        if run.judge_prompt_version_id is not None:
            judge_version = await session.get(PromptVersion, run.judge_prompt_version_id)
            if judge_version is None:
                raise RuntimeError(f"judge version {run.judge_prompt_version_id} not found")
            judge_template = [Message.model_validate(m) for m in judge_version.messages]

    generation = get_generation_provider(config["generation_provider"])
    evaluators = build_all(specs)

    for evaluator in evaluators:
        if isinstance(evaluator, JudgeEvaluator):
            if judge_template is None:  # pragma: no cover - create_run pins it
                raise RuntimeError("run has an llm_judge evaluator but no pinned rubric version")
            evaluator.bind(
                provider=generation,
                template=judge_template,
                model=config.get("judge_model") or config["model"],
                target_model=config["model"],
            )

    # Only construct the embedder if something actually needs it — building the
    # local one loads an ONNX model, which is seconds of latency and hundreds of
    # MB of memory to pay for a run that never embeds anything.
    embedder = None
    if any(isinstance(e, EmbeddingSimilarityEvaluator) for e in evaluators):
        embedder = get_embedding_provider(config["embedding_provider"])
        for evaluator in evaluators:
            if isinstance(evaluator, EmbeddingSimilarityEvaluator):
                evaluator.bind(embedder)

    try:
        return await _run_items(
            run_id=run_id,
            dataset_version_id=dataset_version_id,
            prompt_version_id=prompt_version_id,
            config=config,
            evaluators=evaluators,
            generation=generation,
        )
    except Exception as exc:
        async with session_scope() as session:
            await session.execute(
                update(EvalRun)
                .where(EvalRun.id == run_id)
                .values(status="failed", error=str(exc), finished_at=datetime.now(UTC))
            )
        raise
    finally:
        await generation.aclose()
        if embedder is not None:
            await embedder.aclose()


async def _run_items(
    run_id: uuid.UUID,
    dataset_version_id: uuid.UUID,
    prompt_version_id: uuid.UUID | None,
    config: dict[str, Any],
    evaluators: list[Evaluator[Any]],
    generation: GenerationProvider,
) -> str:
    async with session_scope() as session:
        items = list(
            (
                await session.execute(
                    select(DatasetItem)
                    .where(DatasetItem.dataset_version_id == dataset_version_id)
                    .order_by(DatasetItem.item_index)
                )
            )
            .scalars()
            .all()
        )

        template: list[Message] | None = None
        if prompt_version_id is not None:
            version = await session.get(PromptVersion, prompt_version_id)
            if version is None:
                raise RuntimeError(f"prompt version {prompt_version_id} not found")
            template = [Message.model_validate(m) for m in version.messages]

        # The resume set: examples already generated successfully. Errored rows
        # are deliberately *not* included, so a retry re-attempts them — a
        # transient provider timeout should be retried, not frozen into the run.
        done_rows = await session.execute(
            select(EvalResult.dataset_item_id).where(
                EvalResult.eval_run_id == run_id, EvalResult.error.is_(None)
            )
        )
        already_done = set(done_rows.scalars().all())

    pending = [item for item in items if item.id not in already_done]
    log.info(
        "eval.run.start",
        run_id=str(run_id),
        total=len(items),
        pending=len(pending),
        resumed=len(already_done),
    )

    semaphore = asyncio.Semaphore(int(config.get("concurrency", 8)))

    async def worker(item: DatasetItem) -> bool:
        async with semaphore:
            return await _execute_item(
                run_id=run_id,
                item=item,
                template=template,
                config=config,
                evaluators=evaluators,
                generation=generation,
            )

    # return_exceptions=True: a task that raises unexpectedly must not cancel
    # its siblings mid-flight and lose their results.
    outcomes = await asyncio.gather(*(worker(item) for item in pending), return_exceptions=True)

    failures = sum(1 for o in outcomes if isinstance(o, BaseException) or o is False)

    async with session_scope() as session:
        aggregates = await compute_aggregates(session, run_id)
        # Read rather than passed in: a run can be picked up, retried, or
        # resumed by a different worker, so the authoritative start time is the
        # one on the row, not one this process happened to observe.
        run_started_at = await session.scalar(
            select(EvalRun.started_at).where(EvalRun.id == run_id)
        )
        total_completed = await session.scalar(
            select(func.count())
            .select_from(EvalResult)
            .where(EvalResult.eval_run_id == run_id, EvalResult.error.is_(None))
        )
        completed = bool(total_completed)

        # `partial` is its own terminal state: the run finished, but some
        # examples errored. Collapsing that into "succeeded" would hide a
        # provider outage, and into "failed" would discard usable results.
        status = "succeeded" if failures == 0 else ("partial" if completed else "failed")

        finished_at = datetime.now(UTC)
        await session.execute(
            update(EvalRun)
            .where(EvalRun.id == run_id)
            .values(
                status=status,
                completed_items=total_completed,
                failed_items=failures,
                aggregate_scores={k: v.model_dump() for k, v in aggregates.items()},
                finished_at=finished_at,
            )
        )

    # Duration from the run's own timestamps rather than a timer around this
    # function: a run can be picked up, retried, or resumed by a different
    # worker, and the number worth alerting on is how long the *run* took from
    # the user's point of view, not how long this process was busy.
    if run_started_at is not None:
        metrics.eval_run_duration.labels(status=status).observe(
            (finished_at - run_started_at).total_seconds()
        )
    metrics.eval_examples.labels(outcome="scored").inc(total_completed or 0)
    metrics.eval_examples.labels(outcome="errored").inc(failures)

    log.info("eval.run.finished", run_id=str(run_id), status=status, failed=failures)
    return status


async def _execute_item(
    run_id: uuid.UUID,
    item: DatasetItem,
    template: list[Message] | None,
    config: dict[str, Any],
    evaluators: list[Evaluator[Any]],
    generation: GenerationProvider,
) -> bool:
    """Generate and score one example. Returns False on generation failure.

    Never raises for an expected failure — the whole point is that one bad
    example does not take the run down with it.
    """
    rendered: list[RenderedMessage] = []
    try:
        if template is not None:
            rendered = render_messages(template, item.inputs)
        else:
            # No prompt version: the dataset supplies the message directly.
            raw = item.inputs.get("prompt") or item.inputs.get("input")
            if not isinstance(raw, str):
                raise ValueError(
                    "run has no prompt version, so each item needs a string "
                    "'prompt' or 'input' field"
                )
            rendered = [RenderedMessage(role="user", content=raw)]

        response = await generation.generate_measured(
            GenerationRequest(
                messages=rendered,
                model=config["model"],
                max_tokens=int(config.get("max_tokens", 4096)),
                parameters=dict(config.get("parameters") or {}),
            )
        )
    except Exception as exc:
        # Deliberately broad: a provider timeout, a template render error and a
        # vendor SDK raising something undocumented are all "this example
        # failed", and none of them should end the run.
        await _write_result(
            run_id=run_id,
            item=item,
            rendered=rendered,
            output=None,
            error=f"{type(exc).__name__}: {exc}",
            response=None,
        )
        log.warning("eval.item.failed", run_id=str(run_id), index=item.item_index, error=str(exc))
        return False

    result_id = await _write_result(
        run_id=run_id,
        item=item,
        rendered=rendered,
        output=response.text,
        error=None,
        response=response,
    )

    sample = EvaluationSample(
        output=response.text,
        inputs=item.inputs,
        expected_output=item.expected_output,
        expected_context=item.expected_context,
        metadata=item.item_metadata,
    )

    scores: list[dict[str, Any]] = []
    for evaluator in evaluators:
        try:
            outcome = await evaluator.evaluate(sample)
            scores.append(
                {
                    "evaluator": evaluator.name,
                    "score": outcome.score,
                    "passed": outcome.passed,
                    "detail": outcome.detail,
                    "error": None,
                }
            )
        except UnscoreableError as exc:
            # A gap, not a failure: null score plus a reason, excluded from the
            # mean rather than dragging it down as a zero.
            scores.append(
                {
                    "evaluator": evaluator.name,
                    "score": None,
                    "passed": None,
                    "detail": {},
                    "error": str(exc),
                }
            )
        except Exception as exc:
            scores.append(
                {
                    "evaluator": evaluator.name,
                    "score": None,
                    "passed": None,
                    "detail": {},
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    await _write_scores(run_id=run_id, result_id=result_id, scores=scores)
    return True


async def _write_result(
    run_id: uuid.UUID,
    item: DatasetItem,
    rendered: list[RenderedMessage],
    output: str | None,
    error: str | None,
    response: Any,
) -> uuid.UUID:
    """Upsert the result row, keyed on (run, item).

    Upsert rather than insert so a retry that reaches an example twice updates
    it instead of colliding with the unique constraint.
    """
    async with session_scope() as session:
        stmt = (
            pg_insert(EvalResult)
            .values(
                id=uuid.uuid4(),
                eval_run_id=run_id,
                dataset_item_id=item.id,
                item_index=item.item_index,
                rendered_messages=[m.model_dump() for m in rendered],
                output=output,
                error=error,
                latency_ms=getattr(response, "latency_ms", None),
                prompt_tokens=getattr(response, "input_tokens", None),
                completion_tokens=getattr(response, "output_tokens", None),
                cost_usd=getattr(response, "cost_usd", None),
            )
            .on_conflict_do_update(
                index_elements=[EvalResult.eval_run_id, EvalResult.dataset_item_id],
                set_={
                    "output": output,
                    "error": error,
                    "latency_ms": getattr(response, "latency_ms", None),
                    "prompt_tokens": getattr(response, "input_tokens", None),
                    "completion_tokens": getattr(response, "output_tokens", None),
                    "cost_usd": getattr(response, "cost_usd", None),
                },
            )
            .returning(EvalResult.id)
        )
        result_id = (await session.execute(stmt)).scalar_one()
    return result_id


async def _write_scores(
    run_id: uuid.UUID, result_id: uuid.UUID, scores: list[dict[str, Any]]
) -> None:
    if not scores:
        return
    async with session_scope() as session:
        stmt = pg_insert(EvalScore).values(
            [
                {
                    "id": uuid.uuid4(),
                    "eval_result_id": result_id,
                    "eval_run_id": run_id,
                    "evaluator": s["evaluator"],
                    "score": s["score"],
                    "passed": s["passed"],
                    "detail": s["detail"],
                    "error": s["error"],
                }
                for s in scores
            ]
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[EvalScore.eval_result_id, EvalScore.evaluator],
                set_={
                    "score": stmt.excluded.score,
                    "passed": stmt.excluded.passed,
                    "detail": stmt.excluded.detail,
                    "error": stmt.excluded.error,
                },
            )
        )


async def record_dead_letter(
    session: AsyncSession,
    job_id: str,
    function_name: str,
    job_args: dict[str, Any],
    exc: BaseException,
    attempts: int,
    eval_run_id: uuid.UUID | None = None,
) -> DeadLetterJob:
    """Persist a job that exhausted its retries.

    ADR 0002 accepted that arq has no built-in dead-letter queue and that we
    would own this. The payload plus the final exception plus a link to the run
    is what makes the failure diagnosable and replayable — a broker-level DLQ
    would hold only the opaque job payload.
    """
    entry = DeadLetterJob(
        job_id=job_id,
        function_name=function_name,
        job_args=job_args,
        exception_type=type(exc).__name__,
        exception_message=str(exc),
        traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[:20_000],
        attempts=attempts,
        eval_run_id=eval_run_id,
    )
    session.add(entry)
    await session.flush()
    return entry
