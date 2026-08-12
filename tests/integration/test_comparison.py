"""Run comparison, judge seeding, and the judge running end to end."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from lo_core.db import session_scope
from lo_core.db.models.evaluation import EvalRun, EvalScore
from lo_core.db.models.prompt import Prompt
from lo_core.errors import ConflictError, ValidationError
from lo_core.evaluators.registry import EvaluatorSpec
from lo_core.schemas.evaluation import (
    DatasetCreate,
    DatasetItemIn,
    DatasetVersionCreate,
    EvalRunCreate,
)
from lo_core.schemas.prompt import Message, ModelParameters, PromptCreate, PromptVersionCreate
from lo_core.services import comparison as comparison_service
from lo_core.services import datasets as dataset_service
from lo_core.services import evaluation as eval_service
from lo_core.services import judges as judge_service
from lo_core.services import prompts as prompt_service
from lo_core.services.runner import execute_run

pytestmark = pytest.mark.integration


def slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def setup(project_id: uuid.UUID, items: list[DatasetItemIn]) -> tuple[str, str]:
    dataset_slug, prompt_slug = slug("ds"), slug("pr")
    async with session_scope() as session:
        dataset = await dataset_service.create_dataset(
            session, project_id, DatasetCreate(slug=dataset_slug, name="D")
        )
        await dataset_service.create_version(session, dataset, DatasetVersionCreate(items=items))

        prompt = await prompt_service.create_prompt(
            session, project_id, PromptCreate(slug=prompt_slug, name="P")
        )
        await prompt_service.create_version(
            session,
            prompt,
            PromptVersionCreate(
                messages=[Message(role="user", content="{{ question }}")],
                parameters=ModelParameters(model="fake-model"),
            ),
        )
    return dataset_slug, prompt_slug


async def run_eval(project_id: uuid.UUID, payload: EvalRunCreate) -> uuid.UUID:
    async with session_scope() as session:
        run = await eval_service.create_run(session, project_id, payload)
        run_id = run.id
    await execute_run(run_id)
    return run_id


class TestJudgeSeeding:
    async def test_seeds_four_rubrics_as_judge_prompts(self, committed_project: uuid.UUID) -> None:
        async with session_scope() as session:
            created = await judge_service.seed_builtin_rubrics(session, committed_project)
            assert len(created) == 4
            assert all(p.kind == "judge" for p in created)

        async with session_scope() as session:
            judges = await judge_service.list_judges(session, committed_project)
            assert {p.slug for p in judges} == {
                "judge-correctness",
                "judge-faithfulness",
                "judge-relevance",
                "judge-toxicity",
            }

    async def test_seeding_is_idempotent_and_never_overwrites_edits(
        self, committed_project: uuid.UUID
    ) -> None:
        """Re-seeding on deploy must not silently change what a rubric means."""
        async with session_scope() as session:
            await judge_service.seed_builtin_rubrics(session, committed_project)

        # A team edits a rubric: appends version 2 and promotes it.
        async with session_scope() as session:
            prompt = (
                await session.execute(
                    select(Prompt).where(
                        Prompt.project_id == committed_project,
                        Prompt.slug == "judge-correctness",
                    )
                )
            ).scalar_one()
            await prompt_service.create_version(
                session,
                prompt,
                PromptVersionCreate(
                    messages=[Message(role="user", content="Our stricter rubric {{ output }}")]
                ),
            )

        async with session_scope() as session:
            again = await judge_service.seed_builtin_rubrics(session, committed_project)
            assert again == []

            prompt = (
                await session.execute(
                    select(Prompt).where(
                        Prompt.project_id == committed_project,
                        Prompt.slug == "judge-correctness",
                    )
                )
            ).scalar_one()
            versions = await prompt_service.list_versions(session, prompt.id)
            assert len(versions) == 2
            assert "stricter" in versions[0].messages[0]["content"]

    async def test_rubrics_are_labelled_production(self, committed_project: uuid.UUID) -> None:
        async with session_scope() as session:
            await judge_service.seed_builtin_rubrics(session, committed_project)
            version = await judge_service.resolve_rubric(
                session, committed_project, "judge-correctness", None
            )
            assert version.version == 1

    async def test_application_prompt_rejected_as_rubric(
        self, committed_project: uuid.UUID
    ) -> None:
        """Rendering an app prompt as a rubric would 'work' and produce nonsense."""
        _, prompt_slug = await setup(committed_project, [DatasetItemIn(inputs={"question": "q"})])
        async with session_scope() as session:
            with pytest.raises(ValidationError, match="not a judge rubric"):
                await judge_service.resolve_rubric(session, committed_project, prompt_slug, None)


class TestJudgeEndToEnd:
    async def test_judge_scores_and_run_pins_the_rubric_version(
        self, committed_project: uuid.UUID
    ) -> None:
        """The pin is what makes a judged score attributable later."""
        items = [DatasetItemIn(inputs={"question": "q1"}, expected_output="q1")]
        dataset_slug, prompt_slug = await setup(committed_project, items)

        async with session_scope() as session:
            await judge_service.seed_builtin_rubrics(session, committed_project)

        run_id = await run_eval(
            committed_project,
            EvalRunCreate(
                dataset=dataset_slug,
                prompt=prompt_slug,
                prompt_version="1",
                evaluators=[
                    EvaluatorSpec(type="llm_judge", config={"rubric": "judge-correctness"})
                ],
                model="fake-model",
                judge_model="fake-model",
            ),
        )

        async with session_scope() as session:
            run = await session.get(EvalRun, run_id)
            assert run is not None
            assert run.status == "succeeded"
            assert run.judge_prompt_version_id is not None

            score = (
                await session.execute(select(EvalScore).where(EvalScore.eval_run_id == run_id))
            ).scalar_one()
            assert score.evaluator == "llm_judge"
            assert score.score is not None
            assert score.detail["scale"] == "1-5"
            assert 1 <= score.detail["raw_score"] <= 5

    async def test_two_judges_in_one_run_rejected(self, committed_project: uuid.UUID) -> None:
        """Scores are unique per (result, evaluator), so two judges would collide
        on insert. The registry's duplicate-name check catches it at creation."""
        dataset_slug, prompt_slug = await setup(
            committed_project, [DatasetItemIn(inputs={"question": "q"})]
        )
        async with session_scope() as session:
            await judge_service.seed_builtin_rubrics(session, committed_project)
            with pytest.raises(ValidationError, match="specified more than once"):
                await eval_service.create_run(
                    session,
                    committed_project,
                    EvalRunCreate(
                        dataset=dataset_slug,
                        prompt=prompt_slug,
                        prompt_version="1",
                        evaluators=[
                            EvaluatorSpec(type="llm_judge", config={"rubric": "judge-correctness"}),
                            EvaluatorSpec(type="llm_judge", config={"rubric": "judge-relevance"}),
                        ],
                    ),
                )

    async def test_missing_rubric_rejected_at_creation(self, committed_project: uuid.UUID) -> None:
        from lo_core.errors import NotFoundError

        dataset_slug, prompt_slug = await setup(
            committed_project, [DatasetItemIn(inputs={"question": "q"})]
        )
        async with session_scope() as session:
            with pytest.raises(NotFoundError, match="judge rubric"):
                await eval_service.create_run(
                    session,
                    committed_project,
                    EvalRunCreate(
                        dataset=dataset_slug,
                        prompt=prompt_slug,
                        prompt_version="1",
                        evaluators=[
                            EvaluatorSpec(type="llm_judge", config={"rubric": "judge-nope"})
                        ],
                    ),
                )


class TestComparison:
    async def _two_runs(self, project_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, str]:
        items = [
            DatasetItemIn(inputs={"question": "alpha"}, expected_output="alpha"),
            DatasetItemIn(inputs={"question": "beta"}, expected_output="beta"),
        ]
        dataset_slug, prompt_slug = await setup(project_id, items)

        payload = EvalRunCreate(
            dataset=dataset_slug,
            prompt=prompt_slug,
            prompt_version="1",
            evaluators=[EvaluatorSpec(type="regex_match", config={"pattern": "^alpha"})],
        )
        first = await run_eval(project_id, payload)
        second = await run_eval(project_id, payload)
        return first, second, dataset_slug

    async def test_identical_runs_show_no_regression(self, committed_project: uuid.UUID) -> None:
        first, second, _ = await self._two_runs(committed_project)

        async with session_scope() as session:
            baseline = await session.get(EvalRun, first)
            candidate = await session.get(EvalRun, second)
            assert baseline is not None and candidate is not None
            result = await comparison_service.compare_runs(session, baseline, candidate)

        assert result.alignment == "identity"
        assert result.regressed_count == 0
        assert result.improved_count == 0
        assert all(e.change == "unchanged" for e in result.evaluators)

    async def test_examples_align_by_identity(self, committed_project: uuid.UUID) -> None:
        first, second, _ = await self._two_runs(committed_project)
        async with session_scope() as session:
            baseline = await session.get(EvalRun, first)
            candidate = await session.get(EvalRun, second)
            assert baseline is not None and candidate is not None
            result = await comparison_service.compare_runs(session, baseline, candidate)

        assert len(result.examples) == 2
        # Same dataset item on both sides of every comparison.
        assert all(e.dataset_item_id is not None for e in result.examples)
        assert [e.item_index for e in result.examples] == [0, 1]

    async def test_different_dataset_versions_refused_by_default(
        self, committed_project: uuid.UUID
    ) -> None:
        """The guard against silently comparing unrelated examples."""
        dataset_slug, prompt_slug = await setup(
            committed_project,
            [DatasetItemIn(inputs={"question": "alpha"}, expected_output="alpha")],
        )
        payload = EvalRunCreate(
            dataset=dataset_slug,
            prompt=prompt_slug,
            prompt_version="1",
            evaluators=[EvaluatorSpec(type="exact_match")],
        )
        first = await run_eval(committed_project, payload)

        # A second dataset version, then a run against it.
        async with session_scope() as session:
            dataset = await dataset_service.get_dataset(session, committed_project, dataset_slug)
            await dataset_service.create_version(
                session,
                dataset,
                DatasetVersionCreate(
                    items=[
                        DatasetItemIn(inputs={"question": "inserted"}, expected_output="inserted"),
                        DatasetItemIn(inputs={"question": "alpha"}, expected_output="alpha"),
                    ]
                ),
            )
        second = await run_eval(committed_project, payload)

        async with session_scope() as session:
            baseline = await session.get(EvalRun, first)
            candidate = await session.get(EvalRun, second)
            assert baseline is not None and candidate is not None

            with pytest.raises(ConflictError, match="align=positional"):
                await comparison_service.compare_runs(session, baseline, candidate)

            # The opt-out works, and says plainly that it is weaker.
            result = await comparison_service.compare_runs(
                session, baseline, candidate, align="positional"
            )
            assert result.alignment == "positional"
            assert any("aligned by index" in w for w in result.warnings)

    async def test_model_difference_is_surfaced_as_a_warning(
        self, committed_project: uuid.UUID
    ) -> None:
        """A score delta means different things depending on what moved."""
        items = [DatasetItemIn(inputs={"question": "alpha"}, expected_output="alpha")]
        dataset_slug, prompt_slug = await setup(committed_project, items)

        first = await run_eval(
            committed_project,
            EvalRunCreate(
                dataset=dataset_slug,
                prompt=prompt_slug,
                prompt_version="1",
                evaluators=[EvaluatorSpec(type="exact_match")],
                model="fake-model",
            ),
        )
        second = await run_eval(
            committed_project,
            EvalRunCreate(
                dataset=dataset_slug,
                prompt=prompt_slug,
                prompt_version="1",
                evaluators=[EvaluatorSpec(type="exact_match")],
                model="another-fake-model",
            ),
        )

        async with session_scope() as session:
            baseline = await session.get(EvalRun, first)
            candidate = await session.get(EvalRun, second)
            assert baseline is not None and candidate is not None
            result = await comparison_service.compare_runs(session, baseline, candidate)

        assert any("different models" in w for w in result.warnings)

    async def test_regression_is_detected_and_counted(self, committed_project: uuid.UUID) -> None:
        """A stricter evaluator on the same outputs reads as a regression."""
        items = [DatasetItemIn(inputs={"question": "alpha"}, expected_output="alpha")]
        dataset_slug, prompt_slug = await setup(committed_project, items)

        lenient = await run_eval(
            committed_project,
            EvalRunCreate(
                dataset=dataset_slug,
                prompt=prompt_slug,
                prompt_version="1",
                evaluators=[EvaluatorSpec(type="regex_match", config={"pattern": "alpha"})],
            ),
        )
        strict = await run_eval(
            committed_project,
            EvalRunCreate(
                dataset=dataset_slug,
                prompt=prompt_slug,
                prompt_version="1",
                evaluators=[
                    EvaluatorSpec(
                        type="regex_match", config={"pattern": "impossible", "full_match": True}
                    )
                ],
            ),
        )

        async with session_scope() as session:
            baseline = await session.get(EvalRun, lenient)
            candidate = await session.get(EvalRun, strict)
            assert baseline is not None and candidate is not None
            result = await comparison_service.compare_runs(session, baseline, candidate)

        assert result.regressed_count == 1
        delta = next(e for e in result.evaluators if e.evaluator == "regex_match")
        assert delta.change == "regressed"
        assert delta.delta is not None and delta.delta < 0
