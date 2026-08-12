"""Side-by-side comparison of two eval runs.

This is the question the whole platform exists to answer: *did this change make
things worse?* Everything else — immutable prompt versions, pinned dataset
versions, pinned judge rubrics — exists so that this comparison means something.

Two things make a comparison trustworthy, and both are enforced here:

**Examples are aligned by identity, not position.** Two runs over the same
dataset version are compared by `dataset_item_id`. Aligning by index looks
equivalent and is not: insert one row into a dataset and every subsequent index
shifts, so example *n* in one run is a different question from example *n* in the
other. Every downstream "regression" is then a comparison between two unrelated
examples — precisely the silent-wrongness this tool is supposed to catch.

**What differed between the runs is reported, not assumed.** The prompt version,
the model, the judge rubric and the dataset version are all surfaced, because
"faithfulness fell 0.12" means something entirely different depending on which of
those moved.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.db.models.evaluation import EvalResult, EvalRun, EvalScore
from lo_core.errors import ConflictError
from lo_core.schemas.evaluation import (
    ComparisonChange,
    EvaluatorDelta,
    ExampleComparison,
    RunComparison,
    RunSummary,
)

# Below this, a score difference is noise rather than signal — judged scores in
# particular wobble slightly between runs on identical input.
DEFAULT_EPSILON = 1e-9


async def compare_runs(
    session: AsyncSession,
    baseline: EvalRun,
    candidate: EvalRun,
    align: str = "identity",
    epsilon: float = DEFAULT_EPSILON,
) -> RunComparison:
    """Compare two runs.

    `align="identity"` (the default) requires the same dataset version and
    matches examples by item id. `align="positional"` is the explicit opt-out for
    comparing across dataset versions; it matches by index and attaches a warning,
    so a weaker comparison is always visibly weaker.
    """
    warnings: list[str] = []

    same_dataset = baseline.dataset_version_id == candidate.dataset_version_id
    if align == "identity" and not same_dataset:
        raise ConflictError(
            "runs use different dataset versions, so examples cannot be matched by "
            "identity; pass align=positional to compare them by index instead"
        )
    if align == "positional" and not same_dataset:
        warnings.append(
            "runs use different dataset versions; examples are aligned by index, "
            "so a row inserted or reordered between versions will compare "
            "unrelated examples"
        )

    if baseline.prompt_version_id != candidate.prompt_version_id:
        warnings.append("runs used different prompt versions")
    if baseline.judge_prompt_version_id != candidate.judge_prompt_version_id:
        # The one people forget: a stricter rubric lowers every score it touches,
        # and looks exactly like a model regression.
        warnings.append(
            "runs used different judge rubric versions; a score change may reflect "
            "the rubric rather than the model"
        )
    baseline_model = (baseline.provider_config or {}).get("model")
    candidate_model = (candidate.provider_config or {}).get("model")
    if baseline_model != candidate_model:
        warnings.append(f"runs used different models ({baseline_model} -> {candidate_model})")

    evaluators = _compare_aggregates(baseline, candidate, epsilon)
    examples = await _compare_examples(session, baseline, candidate, align, epsilon)

    return RunComparison(
        baseline=_summarise(baseline),
        candidate=_summarise(candidate),
        alignment=align,
        warnings=warnings,
        evaluators=evaluators,
        examples=examples,
        regressed_count=sum(1 for e in examples if e.change == "regressed"),
        improved_count=sum(1 for e in examples if e.change == "improved"),
    )


def _summarise(run: EvalRun) -> RunSummary:
    return RunSummary(
        id=run.id,
        status=run.status,
        label=run.label,
        commit_sha=run.commit_sha,
        dataset_version_id=run.dataset_version_id,
        prompt_version_id=run.prompt_version_id,
        judge_prompt_version_id=run.judge_prompt_version_id,
        model=(run.provider_config or {}).get("model"),
        total_items=run.total_items,
        completed_items=run.completed_items,
        failed_items=run.failed_items,
        created_at=run.created_at,
    )


def _classify(before: float | None, after: float | None, epsilon: float) -> ComparisonChange:
    if before is None and after is None:
        return "unchanged"
    if before is None:
        return "added"
    if after is None:
        return "removed"
    if abs(after - before) <= epsilon:
        return "unchanged"
    return "improved" if after > before else "regressed"


def _compare_aggregates(
    baseline: EvalRun, candidate: EvalRun, epsilon: float
) -> list[EvaluatorDelta]:
    """Per-evaluator aggregate deltas.

    Evaluators present in only one run are reported as added/removed rather than
    omitted: a run that quietly dropped an evaluator is a real difference, and
    silently comparing the four they share would hide it.
    """
    before_all: dict[str, Any] = baseline.aggregate_scores or {}
    after_all: dict[str, Any] = candidate.aggregate_scores or {}

    deltas: list[EvaluatorDelta] = []
    for name in sorted(set(before_all) | set(after_all)):
        before = before_all.get(name, {}).get("mean")
        after = after_all.get(name, {}).get("mean")
        deltas.append(
            EvaluatorDelta(
                evaluator=name,
                baseline_mean=before,
                candidate_mean=after,
                delta=(after - before) if (before is not None and after is not None) else None,
                change=_classify(before, after, epsilon),
                baseline_pass_rate=before_all.get(name, {}).get("pass_rate"),
                candidate_pass_rate=after_all.get(name, {}).get("pass_rate"),
            )
        )
    return deltas


async def _load_side(
    session: AsyncSession, run_id: uuid.UUID
) -> tuple[
    dict[uuid.UUID, EvalResult], dict[int, EvalResult], dict[uuid.UUID, dict[str, float | None]]
]:
    """Load one run's results plus its scores, in two queries."""
    results = list(
        (
            await session.execute(
                select(EvalResult)
                .where(EvalResult.eval_run_id == run_id)
                .order_by(EvalResult.item_index)
            )
        )
        .scalars()
        .all()
    )

    scores: dict[uuid.UUID, dict[str, float | None]] = {}
    if results:
        rows = await session.execute(select(EvalScore).where(EvalScore.eval_run_id == run_id))
        for score in rows.scalars():
            scores.setdefault(score.eval_result_id, {})[score.evaluator] = score.score

    by_item = {r.dataset_item_id: r for r in results}
    by_index = {r.item_index: r for r in results}
    return by_item, by_index, scores


async def _compare_examples(
    session: AsyncSession,
    baseline: EvalRun,
    candidate: EvalRun,
    align: str,
    epsilon: float,
) -> list[ExampleComparison]:
    base_by_item, base_by_index, base_scores = await _load_side(session, baseline.id)
    cand_by_item, cand_by_index, cand_scores = await _load_side(session, candidate.id)

    # Keyed by item id under identity alignment and by index under positional —
    # the whole point of the flag — so the lookups are deliberately heterogenous.
    base_lookup: dict[Any, EvalResult]
    cand_lookup: dict[Any, EvalResult]
    if align == "identity":
        keys: list[Any] = sorted(
            set(base_by_item) | set(cand_by_item),
            key=lambda k: (base_by_item.get(k) or cand_by_item[k]).item_index,
        )
        base_lookup, cand_lookup = dict(base_by_item), dict(cand_by_item)
    else:
        keys = sorted(set(base_by_index) | set(cand_by_index))
        base_lookup, cand_lookup = dict(base_by_index), dict(cand_by_index)

    comparisons: list[ExampleComparison] = []
    for key in keys:
        before = base_lookup.get(key)
        after = cand_lookup.get(key)
        if before is None and after is None:  # pragma: no cover - keys come from both
            continue

        before_scores = base_scores.get(before.id, {}) if before else {}
        after_scores = cand_scores.get(after.id, {}) if after else {}

        per_evaluator: dict[str, ComparisonChange] = {}
        deltas: dict[str, float | None] = {}
        for name in sorted(set(before_scores) | set(after_scores)):
            per_evaluator[name] = _classify(
                before_scores.get(name), after_scores.get(name), epsilon
            )
            b, a = before_scores.get(name), after_scores.get(name)
            deltas[name] = (a - b) if (b is not None and a is not None) else None

        # An example counts as regressed if *any* evaluator regressed and none
        # improved. Mixed movement is reported as "unchanged" at the example
        # level with the per-evaluator detail intact — collapsing a genuine
        # tradeoff into a single verdict would be a judgement the tool has no
        # basis to make.
        changes = set(per_evaluator.values())
        if "regressed" in changes and "improved" not in changes:
            overall: ComparisonChange = "regressed"
        elif "improved" in changes and "regressed" not in changes:
            overall = "improved"
        elif before is None:
            overall = "added"
        elif after is None:
            overall = "removed"
        else:
            overall = "unchanged"

        comparisons.append(
            ExampleComparison(
                item_index=(before or after).item_index,  # type: ignore[union-attr]
                dataset_item_id=(before or after).dataset_item_id,  # type: ignore[union-attr]
                change=overall,
                baseline_output=before.output if before else None,
                candidate_output=after.output if after else None,
                baseline_scores=before_scores,
                candidate_scores=after_scores,
                score_deltas=deltas,
                evaluator_changes=per_evaluator,
            )
        )

    return comparisons
