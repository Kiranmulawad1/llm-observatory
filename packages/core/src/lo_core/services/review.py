"""Guardrail sampling, the review queue, and promotion back into eval datasets.

This closes the loop the platform exists for:

    production traffic
      -> sampled and checked          (sample_project)
      -> flagged into a queue         (ReviewItem)
      -> labelled by a human          (label_item)
      -> promoted into a dataset      (promote_items)
      -> scored by the next eval run

Each arrow is a function in this module.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.db.models.dataset import Dataset, DatasetItem, DatasetVersion
from lo_core.db.models.review import GuardrailConfig, ReviewItem
from lo_core.db.models.telemetry import Span, Trace
from lo_core.errors import ConflictError, NotFoundError, ValidationError
from lo_core.guardrails import CheckInput, run_checks
from lo_core.logging import get_logger
from lo_core.services import datasets as dataset_service

log = get_logger(__name__)

# How far back a sampling run looks when a project has never been scanned.
INITIAL_LOOKBACK = timedelta(hours=1)
# Ceiling on one run, so a project that was paused for a week does not try to
# process its entire backlog in a single job.
MAX_TRACES_PER_RUN = 500


def sample_bucket(trace_id: str) -> float:
    """Map a trace id to a stable value in [0, 1).

    Deterministic rather than random, which buys two things. Workers need no
    coordination — every one of them agrees on whether a given trace is in the
    sample. And "why wasn't this trace checked?" becomes answerable by
    recomputing the hash, instead of a shrug about randomness.
    """
    digest = hashlib.sha256(trace_id.encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


async def get_config(session: AsyncSession, project_id: uuid.UUID) -> GuardrailConfig | None:
    result = await session.execute(
        select(GuardrailConfig).where(GuardrailConfig.project_id == project_id)
    )
    return result.scalar_one_or_none()


async def upsert_config(
    session: AsyncSession, project_id: uuid.UUID, **values: Any
) -> GuardrailConfig:
    config = await get_config(session, project_id)
    if config is None:
        config = GuardrailConfig(project_id=project_id, **values)
        session.add(config)
    else:
        for key, value in values.items():
            setattr(config, key, value)
    await session.flush()
    await session.refresh(config)
    return config


async def _trace_snapshot(
    session: AsyncSession, project_id: uuid.UUID, trace_id: str, context_field: str
) -> dict[str, Any]:
    """Flatten a trace's spans into the fields a check and a reviewer need.

    Pulls the inputs from the root span, the generated text from the last LLM
    span, and the retrieved documents from whichever span carries them. A
    reviewer looking at a queue item should not have to reconstruct the request
    from a span tree.
    """
    spans = list(
        (
            await session.execute(
                select(Span)
                .where(Span.project_id == project_id, Span.trace_id == trace_id)
                .order_by(Span.started_at)
            )
        )
        .scalars()
        .all()
    )

    root = next((s for s in spans if s.parent_span_id is None), spans[0] if spans else None)
    llm_spans = [s for s in spans if s.kind == "llm"]
    last_llm = llm_spans[-1] if llm_spans else None

    output: str | None = None
    for candidate in (last_llm, root):
        if candidate is not None and candidate.span_output:
            value = candidate.span_output.get("text") or candidate.span_output.get("value")
            if isinstance(value, str):
                output = value
                break

    # Retrieved context can be on any span — a retrieval span's output, or the
    # configured field on any span's input.
    context: list[Any] | None = None
    for span in spans:
        if span.kind == "retrieval" and span.span_output:
            documents = span.span_output.get("documents") or span.span_output.get("value")
            if isinstance(documents, list):
                context = documents
                break
        if span.span_input and context_field in span.span_input:
            value = span.span_input[context_field]
            context = value if isinstance(value, list) else [value]
            break

    return {
        "trace_name": root.name if root else "",
        "inputs": (root.span_input or {}) if root else {},
        "output": output,
        "context": context,
        "model": last_llm.model if last_llm else None,
        "prompt_version_id": last_llm.prompt_version_id if last_llm else None,
    }


async def sample_project(session: AsyncSession, config: GuardrailConfig) -> int:
    """Sample and check recent traces for one project. Returns items queued.

    Idempotent over an overlapping window: `ON CONFLICT DO NOTHING` on
    `(project_id, trace_id)` means re-running the sampler never gives a human
    the same trace twice.
    """
    if not config.enabled:
        return 0

    since = config.last_scanned_at or (datetime.now(UTC) - INITIAL_LOOKBACK)
    now = datetime.now(UTC)

    traces = list(
        (
            await session.execute(
                select(Trace)
                .where(
                    Trace.project_id == config.project_id,
                    Trace.started_at > since,
                    Trace.started_at <= now,
                )
                .order_by(Trace.started_at)
                .limit(MAX_TRACES_PER_RUN)
            )
        )
        .scalars()
        .all()
    )

    enabled_checks = {
        name
        for name, on in (
            ("pii", config.check_pii),
            ("grounding", config.check_grounding),
            ("toxicity", config.check_toxicity),
        )
        if on
    }

    queued = 0
    for trace in traces:
        bucket = sample_bucket(trace.trace_id)

        # Errored traces are always examined regardless of rate — they are the
        # ones a human most wants to see, and sampling them away to hit a quota
        # would be exactly backwards.
        in_sample = trace.status == "error" or bucket < config.sample_rate
        if not in_sample:
            continue

        snapshot = await _trace_snapshot(
            session, config.project_id, trace.trace_id, config.context_field
        )
        findings = run_checks(
            CheckInput(
                output=snapshot["output"] or "",
                inputs=snapshot["inputs"],
                context=snapshot["context"],
            ),
            enabled=enabled_checks,
        )

        if findings:
            sampled_as = "flagged"
        else:
            # A clean trace still enters the queue at the control rate. A second
            # independent hash so control selection is not correlated with the
            # sampling decision that preceded it.
            control_bucket = sample_bucket(f"control:{trace.trace_id}")
            if control_bucket >= config.control_sample_rate:
                continue
            sampled_as = "control"

        severity = max((f.severity for f in findings), default=0.0)

        stmt = (
            pg_insert(ReviewItem)
            .values(
                id=uuid.uuid4(),
                project_id=config.project_id,
                trace_id=trace.trace_id,
                status="pending",
                sampled_as=sampled_as,
                findings=[f.to_dict() for f in findings],
                severity=severity,
                **snapshot,
            )
            .on_conflict_do_nothing(index_elements=[ReviewItem.project_id, ReviewItem.trace_id])
            .returning(ReviewItem.id)
        )
        if (await session.execute(stmt)).scalar_one_or_none() is not None:
            queued += 1

    # Advance the watermark to the window we actually examined, so the next run
    # starts where this one stopped rather than rescanning.
    config.last_scanned_at = traces[-1].started_at if traces else now
    await session.flush()

    if queued:
        log.info("guardrails.sampled", project=str(config.project_id), queued=queued)
    return queued


async def sample_all(session: AsyncSession) -> int:
    """Sample every project with guardrails enabled.

    Projects are processed independently: one project's malformed trace must not
    stop another project from being sampled.
    """
    configs = list(
        (await session.execute(select(GuardrailConfig).where(GuardrailConfig.enabled.is_(True))))
        .scalars()
        .all()
    )

    total = 0
    for config in configs:
        try:
            total += await sample_project(session, config)
        except Exception as exc:
            log.error("guardrails.sample_failed", project=str(config.project_id), error=str(exc))
    return total


# --- The queue ------------------------------------------------------------


async def list_items(
    session: AsyncSession,
    project_id: uuid.UUID,
    status: str | None = "pending",
    limit: int = 50,
    offset: int = 0,
) -> list[ReviewItem]:
    """Queue contents, worst first.

    Ordered by severity so a leaked API key is reviewed before an ungrounded
    number, and by age within a severity so nothing starves.
    """
    stmt = select(ReviewItem).where(ReviewItem.project_id == project_id)
    if status is not None:
        stmt = stmt.where(ReviewItem.status == status)

    result = await session.execute(
        stmt.order_by(ReviewItem.severity.desc(), ReviewItem.created_at).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


async def get_item(session: AsyncSession, project_id: uuid.UUID, item_id: uuid.UUID) -> ReviewItem:
    result = await session.execute(
        select(ReviewItem).where(ReviewItem.id == item_id, ReviewItem.project_id == project_id)
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise NotFoundError(f"review item {item_id} not found")
    return item


async def label_item(
    session: AsyncSession,
    item: ReviewItem,
    verdict: str,
    reason: str | None = None,
    notes: str | None = None,
    corrected_output: str | None = None,
    labeled_by: str | None = None,
) -> ReviewItem:
    """Record a human judgement."""
    if item.promoted_at is not None:
        raise ConflictError("this item has already been promoted into a dataset")

    item.verdict = verdict
    item.label_reason = reason
    item.notes = notes
    item.corrected_output = corrected_output
    item.labeled_by = labeled_by
    item.labeled_at = datetime.now(UTC)
    item.status = "labeled"

    await session.flush()
    return item


async def skip_item(session: AsyncSession, item: ReviewItem) -> ReviewItem:
    """Dismiss without a verdict — a false positive worth clearing, not judging."""
    item.status = "skipped"
    await session.flush()
    return item


async def queue_stats(session: AsyncSession, project_id: uuid.UUID) -> dict[str, Any]:
    """Counts by status, plus the control-sample signal.

    `control_flagged_bad` is the number that matters: clean traces a human
    judged bad. It is the false-negative rate of the checks, and the only reason
    the control sample exists.
    """
    rows = (
        await session.execute(
            select(ReviewItem.status, func.count())
            .where(ReviewItem.project_id == project_id)
            .group_by(ReviewItem.status)
        )
    ).all()
    by_status = {status: count for status, count in rows}

    control_bad = await session.scalar(
        select(func.count())
        .select_from(ReviewItem)
        .where(
            ReviewItem.project_id == project_id,
            ReviewItem.sampled_as == "control",
            ReviewItem.verdict == "bad",
        )
    )
    control_total = await session.scalar(
        select(func.count())
        .select_from(ReviewItem)
        .where(
            ReviewItem.project_id == project_id,
            ReviewItem.sampled_as == "control",
            ReviewItem.status == "labeled",
        )
    )
    promoted = await session.scalar(
        select(func.count())
        .select_from(ReviewItem)
        .where(ReviewItem.project_id == project_id, ReviewItem.promoted_at.is_not(None))
    )

    # `session.scalar` is typed as returning None, so the counts are coerced
    # before arithmetic rather than after.
    missed = int(control_bad or 0)
    reviewed = int(control_total or 0)

    return {
        "pending": by_status.get("pending", 0),
        "labeled": by_status.get("labeled", 0),
        "skipped": by_status.get("skipped", 0),
        "promoted": promoted or 0,
        "control_reviewed": reviewed,
        "control_missed": missed,
        # What fraction of traces the checks called clean were actually bad.
        "estimated_miss_rate": (missed / reviewed) if reviewed else None,
    }


# --- The flywheel ---------------------------------------------------------


async def promote_items(
    session: AsyncSession,
    project_id: uuid.UUID,
    item_ids: list[uuid.UUID],
    dataset_slug: str,
    created_by: str | None = None,
) -> DatasetVersion:
    """Turn labelled review items into a new dataset version.

    **Batched, and it has to be.** Dataset versions are immutable, so promoting
    one item at a time would create a version per label — fifty labels, fifty
    versions, and no way to say which run tested which set. One promotion is one
    version.

    The new version carries every item from the current latest version *plus*
    the promoted examples, because a version is a complete snapshot rather than
    a delta (see the DatasetVersion model).

    The expected output is the reviewer's correction when they supplied one, and
    the model's own output when they marked it good — a "good" verdict is
    precisely the statement that the output was the right answer.
    """
    if not item_ids:
        raise ValidationError("no items selected")

    items = list(
        (
            await session.execute(
                select(ReviewItem).where(
                    ReviewItem.project_id == project_id, ReviewItem.id.in_(item_ids)
                )
            )
        )
        .scalars()
        .all()
    )

    if len(items) != len(item_ids):
        raise NotFoundError("one or more review items were not found in this project")

    unlabeled = [i for i in items if i.status != "labeled"]
    if unlabeled:
        raise ValidationError(f"{len(unlabeled)} of the selected items have not been labelled")

    already = [i for i in items if i.promoted_at is not None]
    if already:
        raise ConflictError(f"{len(already)} of the selected items were already promoted")

    dataset = await dataset_service.get_dataset(session, project_id, dataset_slug)

    # Carry forward the existing items so the new version is complete.
    existing: list[DatasetItem] = []
    try:
        current = await dataset_service.get_version(session, dataset.id, None)
        existing = await dataset_service.list_items(session, current.id, limit=10_000)
    except NotFoundError:
        # First version of an empty dataset.
        pass

    new_items: list[dict[str, Any]] = []
    for item in items:
        expected = item.corrected_output if item.verdict == "bad" else item.output
        if expected is None:
            raise ValidationError(
                f"item {item.id} is labelled 'bad' but has no corrected output — "
                "an example with no expected answer cannot be scored"
            )
        new_items.append(
            {
                "inputs": item.inputs,
                "expected_output": expected,
                "expected_context": item.context,
                "metadata": {
                    # Provenance. This is what lets someone six months from now
                    # ask where a dataset example came from and get an answer.
                    "source": "review_queue",
                    "review_item_id": str(item.id),
                    "trace_id": item.trace_id,
                    "verdict": item.verdict,
                    "reason": item.label_reason,
                    "labeled_by": item.labeled_by,
                },
            }
        )

    from lo_core.schemas.evaluation import DatasetItemIn, DatasetVersionCreate

    payload = DatasetVersionCreate(
        items=[
            DatasetItemIn(
                inputs=e.inputs,
                expected_output=e.expected_output,
                expected_context=e.expected_context,
                metadata=e.item_metadata,
            )
            for e in existing
        ]
        + [DatasetItemIn(**row) for row in new_items],
        created_by=created_by,
        change_note=f"Promoted {len(items)} reviewed example(s) from the review queue.",
    )

    version = await dataset_service.create_version(session, dataset, payload)

    promoted_at = datetime.now(UTC)
    for item in items:
        item.promoted_to_version_id = version.id
        item.promoted_at = promoted_at

    await session.flush()
    log.info(
        "review.promoted",
        project=str(project_id),
        dataset=dataset_slug,
        version=version.version,
        count=len(items),
    )
    return version


async def list_datasets_for_promotion(
    session: AsyncSession, project_id: uuid.UUID
) -> list[Dataset]:
    result = await session.execute(
        select(Dataset).where(Dataset.project_id == project_id).order_by(Dataset.slug)
    )
    return list(result.scalars().all())
