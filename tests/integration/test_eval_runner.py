"""End-to-end eval execution.

These commit for real (see the `committed_project` fixture) because the runner
opens its own sessions per concurrent example and cannot see uncommitted rows.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from lo_core.db import session_scope
from lo_core.db.models.evaluation import EvalResult, EvalRun, EvalScore
from lo_core.errors import ValidationError
from lo_core.evaluators.registry import EvaluatorSpec
from lo_core.providers.fake import FAIL_MARKER
from lo_core.schemas.evaluation import (
    DatasetCreate,
    DatasetItemIn,
    DatasetVersionCreate,
    EvalRunCreate,
)
from lo_core.schemas.prompt import Message, ModelParameters, PromptCreate, PromptVersionCreate
from lo_core.services import datasets as dataset_service
from lo_core.services import evaluation as eval_service
from lo_core.services import prompts as prompt_service
from lo_core.services.runner import execute_run

pytestmark = pytest.mark.integration


def slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def setup_fixture(
    project_id: uuid.UUID,
    items: list[DatasetItemIn],
    template: str = "{{ question }}",
) -> tuple[str, str]:
    """Create a dataset and a prompt, both committed. Returns their slugs."""
    dataset_slug, prompt_slug = slug("ds"), slug("pr")
    async with session_scope() as session:
        dataset = await dataset_service.create_dataset(
            session, project_id, DatasetCreate(slug=dataset_slug, name="Test dataset")
        )
        await dataset_service.create_version(session, dataset, DatasetVersionCreate(items=items))

        prompt = await prompt_service.create_prompt(
            session, project_id, PromptCreate(slug=prompt_slug, name="Test prompt")
        )
        await prompt_service.create_version(
            session,
            prompt,
            PromptVersionCreate(
                messages=[Message(role="user", content=template)],
                parameters=ModelParameters(model="fake-model"),
            ),
        )
    return dataset_slug, prompt_slug


async def start_run(project_id: uuid.UUID, payload: EvalRunCreate) -> uuid.UUID:
    async with session_scope() as session:
        run = await eval_service.create_run(session, project_id, payload)
        return run.id


class TestHappyPath:
    async def test_run_completes_and_scores_every_item(self, committed_project: uuid.UUID) -> None:
        """The fake provider echoes the input, so exact_match against the same
        text is a predictable pass."""
        items = [
            DatasetItemIn(inputs={"question": "alpha"}, expected_output="alpha"),
            DatasetItemIn(inputs={"question": "beta"}, expected_output="beta"),
            DatasetItemIn(inputs={"question": "gamma"}, expected_output="wrong"),
        ]
        dataset_slug, prompt_slug = await setup_fixture(committed_project, items)

        run_id = await start_run(
            committed_project,
            EvalRunCreate(
                dataset=dataset_slug,
                prompt=prompt_slug,
                prompt_version="1",
                evaluators=[EvaluatorSpec(type="regex_match", config={"pattern": "^alpha"})],
            ),
        )

        status = await execute_run(run_id)
        assert status == "succeeded"

        async with session_scope() as session:
            run = await session.get(EvalRun, run_id)
            assert run is not None
            assert run.completed_items == 3
            assert run.failed_items == 0
            assert run.started_at is not None and run.finished_at is not None

            aggregate = run.aggregate_scores["regex_match"]
            assert aggregate["count"] == 3
            # Only "alpha" matches ^alpha.
            assert aggregate["mean"] == pytest.approx(1 / 3)
            assert aggregate["pass_rate"] == pytest.approx(1 / 3)

    async def test_results_record_generation_details(self, committed_project: uuid.UUID) -> None:
        items = [DatasetItemIn(inputs={"question": "hello"}, expected_output="hello")]
        dataset_slug, prompt_slug = await setup_fixture(committed_project, items)

        run_id = await start_run(
            committed_project,
            EvalRunCreate(
                dataset=dataset_slug,
                prompt=prompt_slug,
                prompt_version="1",
                evaluators=[EvaluatorSpec(type="exact_match")],
            ),
        )
        await execute_run(run_id)

        async with session_scope() as session:
            result = (
                await session.execute(select(EvalResult).where(EvalResult.eval_run_id == run_id))
            ).scalar_one()
            assert result.output is not None
            assert result.prompt_tokens and result.prompt_tokens > 0
            # Exactly what was sent, for debugging a bad score later.
            assert result.rendered_messages == [{"role": "user", "content": "hello"}]

    async def test_embedding_similarity_scores(self, committed_project: uuid.UUID) -> None:
        items = [
            DatasetItemIn(
                inputs={"question": "the capital is paris"}, expected_output="the capital is paris"
            )
        ]
        dataset_slug, prompt_slug = await setup_fixture(committed_project, items)

        run_id = await start_run(
            committed_project,
            EvalRunCreate(
                dataset=dataset_slug,
                prompt=prompt_slug,
                prompt_version="1",
                evaluators=[EvaluatorSpec(type="embedding_similarity", config={"threshold": 0.5})],
            ),
        )
        await execute_run(run_id)

        async with session_scope() as session:
            score = (
                await session.execute(select(EvalScore).where(EvalScore.eval_run_id == run_id))
            ).scalar_one()
            assert score.score is not None and score.score > 0.5
            assert score.detail["embedding_provider"] == "fake"


class TestFailureIsolation:
    async def test_one_failing_item_does_not_sink_the_run(
        self, committed_project: uuid.UUID
    ) -> None:
        """The central resilience property: 1 provider failure, 2 usable results."""
        items = [
            DatasetItemIn(inputs={"question": "fine"}, expected_output="fine"),
            DatasetItemIn(inputs={"question": FAIL_MARKER}, expected_output="never"),
            DatasetItemIn(inputs={"question": "also fine"}, expected_output="also fine"),
        ]
        dataset_slug, prompt_slug = await setup_fixture(committed_project, items)

        run_id = await start_run(
            committed_project,
            EvalRunCreate(
                dataset=dataset_slug,
                prompt=prompt_slug,
                prompt_version="1",
                evaluators=[EvaluatorSpec(type="exact_match")],
            ),
        )

        status = await execute_run(run_id)
        # `partial` is its own terminal state — neither success nor failure.
        assert status == "partial"

        async with session_scope() as session:
            run = await session.get(EvalRun, run_id)
            assert run is not None
            assert run.failed_items == 1
            assert run.completed_items == 2

            errored = (
                (
                    await session.execute(
                        select(EvalResult).where(
                            EvalResult.eval_run_id == run_id, EvalResult.error.is_not(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(errored) == 1

    async def test_missing_expected_output_is_unscoreable_not_zero(
        self, committed_project: uuid.UUID
    ) -> None:
        """A dataset gap must not drag the mean down and look like a regression."""
        items = [
            DatasetItemIn(inputs={"question": "alpha"}, expected_output="alpha"),
            DatasetItemIn(inputs={"question": "beta"}, expected_output=None),
        ]
        dataset_slug, prompt_slug = await setup_fixture(committed_project, items)

        run_id = await start_run(
            committed_project,
            EvalRunCreate(
                dataset=dataset_slug,
                prompt=prompt_slug,
                prompt_version="1",
                evaluators=[EvaluatorSpec(type="exact_match")],
            ),
        )
        await execute_run(run_id)

        async with session_scope() as session:
            run = await session.get(EvalRun, run_id)
            assert run is not None
            aggregate = run.aggregate_scores["exact_match"]

            # The point: the gap is excluded from the denominator, not scored 0.0.
            # Two examples ran, but only one was scoreable.
            assert aggregate["count"] == 1
            assert aggregate["unscoreable"] == 1

            scored = (
                await session.execute(
                    select(EvalScore).where(
                        EvalScore.eval_run_id == run_id, EvalScore.score.is_not(None)
                    )
                )
            ).scalar_one()
            # The mean is exactly the one real score — the null contributed
            # nothing rather than averaging in as a zero.
            assert aggregate["mean"] == pytest.approx(scored.score)

            unscoreable = (
                await session.execute(
                    select(EvalScore).where(
                        EvalScore.eval_run_id == run_id, EvalScore.score.is_(None)
                    )
                )
            ).scalar_one()
            assert "expected_output" in (unscoreable.error or "")


class TestResume:
    async def test_rerun_does_not_duplicate_completed_work(
        self, committed_project: uuid.UUID
    ) -> None:
        """Re-executing must upsert, not collide, and not re-pay for done items."""
        items = [
            DatasetItemIn(inputs={"question": f"q{i}"}, expected_output=f"q{i}") for i in range(4)
        ]
        dataset_slug, prompt_slug = await setup_fixture(committed_project, items)

        run_id = await start_run(
            committed_project,
            EvalRunCreate(
                dataset=dataset_slug,
                prompt=prompt_slug,
                prompt_version="1",
                evaluators=[EvaluatorSpec(type="exact_match")],
            ),
        )
        await execute_run(run_id)

        # Reset to running so the guard doesn't short-circuit, then re-execute.
        async with session_scope() as session:
            run = await session.get(EvalRun, run_id)
            assert run is not None
            run.status = "running"

        await execute_run(run_id)

        async with session_scope() as session:
            result_count = await session.scalar(
                select(func.count()).select_from(EvalResult).where(EvalResult.eval_run_id == run_id)
            )
            score_count = await session.scalar(
                select(func.count()).select_from(EvalScore).where(EvalScore.eval_run_id == run_id)
            )
            assert result_count == 4
            assert score_count == 4

    async def test_terminal_run_is_not_re_executed(self, committed_project: uuid.UUID) -> None:
        """arq can redeliver a job after the worker died post-completion."""
        items = [DatasetItemIn(inputs={"question": "a"}, expected_output="a")]
        dataset_slug, prompt_slug = await setup_fixture(committed_project, items)

        run_id = await start_run(
            committed_project,
            EvalRunCreate(
                dataset=dataset_slug,
                prompt=prompt_slug,
                prompt_version="1",
                evaluators=[EvaluatorSpec(type="exact_match")],
            ),
        )
        assert await execute_run(run_id) == "succeeded"
        assert await execute_run(run_id) == "succeeded"


class TestRunCreationValidation:
    async def test_unknown_evaluator_rejected_before_enqueue(
        self, committed_project: uuid.UUID
    ) -> None:
        dataset_slug, prompt_slug = await setup_fixture(
            committed_project, [DatasetItemIn(inputs={"question": "a"})]
        )
        async with session_scope() as session:
            with pytest.raises(ValidationError, match="unknown evaluator"):
                await eval_service.create_run(
                    session,
                    committed_project,
                    EvalRunCreate(
                        dataset=dataset_slug,
                        prompt=prompt_slug,
                        prompt_version="1",
                        evaluators=[EvaluatorSpec(type="does_not_exist")],
                    ),
                )

    async def test_bad_regex_rejected_at_creation(self, committed_project: uuid.UUID) -> None:
        """Fails on the API call, not on example 300 of 500."""
        dataset_slug, prompt_slug = await setup_fixture(
            committed_project, [DatasetItemIn(inputs={"question": "a"})]
        )
        async with session_scope() as session:
            with pytest.raises(ValidationError, match="invalid config"):
                await eval_service.create_run(
                    session,
                    committed_project,
                    EvalRunCreate(
                        dataset=dataset_slug,
                        prompt=prompt_slug,
                        prompt_version="1",
                        evaluators=[
                            EvaluatorSpec(type="regex_match", config={"pattern": "(unclosed"})
                        ],
                    ),
                )

    async def test_unknown_provider_rejected(self, committed_project: uuid.UUID) -> None:
        dataset_slug, prompt_slug = await setup_fixture(
            committed_project, [DatasetItemIn(inputs={"question": "a"})]
        )
        async with session_scope() as session:
            with pytest.raises(ValidationError, match="unknown generation provider"):
                await eval_service.create_run(
                    session,
                    committed_project,
                    EvalRunCreate(
                        dataset=dataset_slug,
                        prompt=prompt_slug,
                        evaluators=[EvaluatorSpec(type="exact_match")],
                        generation_provider="nope",
                    ),
                )
