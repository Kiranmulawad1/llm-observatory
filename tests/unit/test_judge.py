"""Judge evaluator: scale mapping, structured output, self-judging."""

from __future__ import annotations

import json

import pytest

from lo_core.evaluators.base import EvaluationSample, UnscoreableError
from lo_core.evaluators.judge import JudgeConfig, JudgeEvaluator, _stringify_context
from lo_core.evaluators.rubrics import (
    BUILTIN_RUBRICS,
    JUDGE_RESPONSE_SCHEMA,
    JUDGE_SCALE_MAX,
    JUDGE_SCALE_MIN,
)
from lo_core.providers.base import (
    GenerationProvider,
    GenerationRequest,
    GenerationResponse,
)
from lo_core.schemas.prompt import Message


class ScriptedJudge(GenerationProvider):
    """Returns a fixed verdict, and records what it was asked."""

    name = "scripted"

    def __init__(self, payload: object, model: str = "claude-opus-5") -> None:
        self._payload = payload
        self._model = model
        self.last_request: GenerationRequest | None = None

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.last_request = request
        text = self._payload if isinstance(self._payload, str) else json.dumps(self._payload)
        return GenerationResponse(text=text, model=self._model)


def build(provider: GenerationProvider, **config: object) -> JudgeEvaluator:
    evaluator = JudgeEvaluator(JudgeConfig(rubric="judge-correctness", **config))  # type: ignore[arg-type]
    evaluator.bind(
        provider=provider,
        template=[Message(role="user", content="Rate {{ output }} against {{ expected_output }}")],
        model="claude-opus-5",
        target_model="claude-sonnet-5",
    )
    return evaluator


SAMPLE = EvaluationSample(output="Paris", expected_output="Paris", inputs={"question": "capital?"})


class TestScaleMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(1, 0.0), (2, 0.25), (3, 0.5), (4, 0.75), (5, 1.0)],
    )
    async def test_maps_1_to_5_onto_0_to_1(self, raw: int, expected: float) -> None:
        """The bottom of the rubric is the bottom of the scale, not 0.2."""
        judge = build(ScriptedJudge({"score": raw, "reasoning": "because"}))
        outcome = await judge.evaluate(SAMPLE)
        assert outcome.score == pytest.approx(expected)
        assert outcome.detail["raw_score"] == raw

    async def test_out_of_range_score_is_clamped(self) -> None:
        judge = build(ScriptedJudge({"score": 9, "reasoning": "over"}))
        assert (await judge.evaluate(SAMPLE)).score == 1.0

    async def test_default_threshold_needs_four_of_five(self) -> None:
        """Judged scores are noisy near the middle, so the bar sits above it."""
        assert (await build(ScriptedJudge({"score": 4, "reasoning": ""})).evaluate(SAMPLE)).passed
        assert not (
            await build(ScriptedJudge({"score": 3, "reasoning": ""})).evaluate(SAMPLE)
        ).passed

    async def test_reasoning_is_preserved(self) -> None:
        judge = build(ScriptedJudge({"score": 2, "reasoning": "omits the date"}))
        assert (await judge.evaluate(SAMPLE)).detail["reasoning"] == "omits the date"


class TestStructuredOutput:
    async def test_requests_the_judge_schema(self) -> None:
        """Constrained generation, not regex-parsing a number out of prose."""
        provider = ScriptedJudge({"score": 5, "reasoning": "ok"})
        await build(provider).evaluate(SAMPLE)
        assert provider.last_request is not None
        assert provider.last_request.response_schema == JUDGE_RESPONSE_SCHEMA

    async def test_unparseable_verdict_is_unscoreable_not_zero(self) -> None:
        """A broken judge means "no score", not "the answer was bad"."""
        judge = build(ScriptedJudge("I would say about a 4 out of 5"))
        with pytest.raises(UnscoreableError, match="unusable output"):
            await judge.evaluate(SAMPLE)

    async def test_missing_score_key_is_unscoreable(self) -> None:
        judge = build(ScriptedJudge({"reasoning": "forgot the score"}))
        with pytest.raises(UnscoreableError):
            await judge.evaluate(SAMPLE)


class TestSelfJudging:
    async def test_flags_when_judge_equals_model_under_test(self) -> None:
        """Recorded, not hidden: models rate their own output more favourably."""
        judge = JudgeEvaluator(JudgeConfig(rubric="judge-correctness"))
        judge.bind(
            provider=ScriptedJudge({"score": 5, "reasoning": "great"}, model="claude-opus-5"),
            template=[Message(role="user", content="{{ output }}")],
            model="claude-opus-5",
            target_model="claude-opus-5",
        )
        detail = (await judge.evaluate(SAMPLE)).detail
        assert detail["self_judged"] is True

    async def test_not_flagged_when_models_differ(self) -> None:
        judge = build(ScriptedJudge({"score": 5, "reasoning": "great"}, model="claude-opus-5"))
        assert "self_judged" not in (await judge.evaluate(SAMPLE)).detail


class TestContextRendering:
    def test_list_of_passages_is_numbered(self) -> None:
        """Faithfulness reasoning refers to "passage 2"; a blob gives it nothing."""
        rendered = _stringify_context(["first passage", "second passage"])
        assert rendered == "[1] first passage\n\n[2] second passage"

    def test_string_context_passes_through(self) -> None:
        assert _stringify_context("just text") == "just text"

    def test_none_becomes_empty(self) -> None:
        assert _stringify_context(None) == ""

    async def test_rubric_variables_are_available(self) -> None:
        provider = ScriptedJudge({"score": 5, "reasoning": "ok"})
        judge = JudgeEvaluator(JudgeConfig(rubric="judge-faithfulness"))
        judge.bind(
            provider=provider,
            template=[Message(role="user", content="ctx={{ context }} out={{ output }}")],
            model="claude-opus-5",
        )
        await judge.evaluate(
            EvaluationSample(output="Paris", inputs={"context": ["doc a", "doc b"]})
        )
        assert provider.last_request is not None
        content = provider.last_request.messages[0].content
        assert "[1] doc a" in content
        assert "out=Paris" in content


class TestBuiltinRubrics:
    def test_every_rubric_has_a_judge_prefixed_slug(self) -> None:
        assert all(r.slug.startswith("judge-") for r in BUILTIN_RUBRICS)

    def test_slugs_are_unique(self) -> None:
        slugs = [r.slug for r in BUILTIN_RUBRICS]
        assert len(slugs) == len(set(slugs))

    def test_covers_the_four_required_dimensions(self) -> None:
        slugs = {r.slug for r in BUILTIN_RUBRICS}
        assert slugs == {
            "judge-correctness",
            "judge-faithfulness",
            "judge-relevance",
            "judge-toxicity",
        }

    def test_every_rubric_anchors_all_five_scale_points(self) -> None:
        """An unanchored 1-5 produces a judge that clusters everything at 3-4."""
        for rubric in BUILTIN_RUBRICS:
            for point in range(JUDGE_SCALE_MIN, JUDGE_SCALE_MAX + 1):
                assert f"{point} -" in rubric.user, f"{rubric.slug} lacks an anchor for {point}"

    def test_rubric_templates_compile(self) -> None:
        from lo_core.templating import compile_template

        for rubric in BUILTIN_RUBRICS:
            compile_template(rubric.system)
            compile_template(rubric.user)
