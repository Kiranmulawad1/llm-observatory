"""Retrieval metrics: precision@k, recall@k, MRR."""

from __future__ import annotations

import pytest

from lo_core.evaluators.base import EvaluationSample, UnscoreableError
from lo_core.evaluators.retrieval import (
    MRREvaluator,
    PrecisionAtKEvaluator,
    RecallAtKEvaluator,
    RetrievalConfig,
)


def sample(retrieved: list[object] | None, relevant: list[object] | None) -> EvaluationSample:
    inputs: dict[str, object] = {}
    if retrieved is not None:
        inputs["retrieved_context"] = retrieved
    return EvaluationSample(output="irrelevant", inputs=inputs, expected_context=relevant)


class TestPrecision:
    async def test_all_retrieved_are_relevant(self) -> None:
        outcome = await PrecisionAtKEvaluator().evaluate(sample(["a", "b"], ["a", "b", "c"]))
        assert outcome.score == 1.0

    async def test_half_relevant(self) -> None:
        outcome = await PrecisionAtKEvaluator().evaluate(sample(["a", "x"], ["a", "b"]))
        assert outcome.score == 0.5

    async def test_k_truncates_the_retrieved_list(self) -> None:
        """precision@1 looks only at the top result."""
        evaluator = PrecisionAtKEvaluator(RetrievalConfig(k=1))
        assert (await evaluator.evaluate(sample(["a", "x", "y"], ["a"]))).score == 1.0
        assert (await evaluator.evaluate(sample(["x", "a"], ["a"]))).score == 0.0

    async def test_empty_retrieval_scores_zero_not_unscoreable(self) -> None:
        """Retrieving nothing is a retrieval failure, not a missing label."""
        outcome = await PrecisionAtKEvaluator().evaluate(sample([], ["a"]))
        assert outcome.score == 0.0
        assert outcome.passed is False


class TestRecall:
    async def test_found_all_relevant(self) -> None:
        outcome = await RecallAtKEvaluator().evaluate(sample(["a", "b", "z"], ["a", "b"]))
        assert outcome.score == 1.0

    async def test_missed_half(self) -> None:
        outcome = await RecallAtKEvaluator().evaluate(sample(["a"], ["a", "b"]))
        assert outcome.score == 0.5

    async def test_reports_which_passages_were_missed(self) -> None:
        """The actionable half of a bad score."""
        outcome = await RecallAtKEvaluator().evaluate(sample(["a"], ["a", "b", "c"]))
        assert set(outcome.detail["missed"]) == {"b", "c"}

    async def test_no_ground_truth_is_unscoreable(self) -> None:
        with pytest.raises(UnscoreableError, match="no relevant documents"):
            await RecallAtKEvaluator().evaluate(sample(["a"], []))


class TestMRR:
    async def test_first_position_scores_one(self) -> None:
        outcome = await MRREvaluator().evaluate(sample(["a", "x", "y"], ["a"]))
        assert outcome.score == 1.0
        assert outcome.detail["first_relevant_rank"] == 1

    async def test_third_position_scores_one_third(self) -> None:
        """Rank-sensitive, unlike precision and recall."""
        outcome = await MRREvaluator().evaluate(sample(["x", "y", "a"], ["a"]))
        assert outcome.score == pytest.approx(1 / 3)
        assert outcome.detail["first_relevant_rank"] == 3

    async def test_not_found_scores_zero(self) -> None:
        outcome = await MRREvaluator().evaluate(sample(["x", "y"], ["a"]))
        assert outcome.score == 0.0
        assert outcome.detail["first_relevant_rank"] is None

    async def test_precision_and_recall_ignore_order_but_mrr_does_not(self) -> None:
        """The property that justifies having MRR at all."""
        best = sample(["a", "x"], ["a"])
        worst = sample(["x", "a"], ["a"])

        assert (await PrecisionAtKEvaluator().evaluate(best)).score == (
            await PrecisionAtKEvaluator().evaluate(worst)
        ).score
        assert (await MRREvaluator().evaluate(best)).score > (
            await MRREvaluator().evaluate(worst)
        ).score


class TestDocumentMatching:
    async def test_objects_match_on_id_field(self) -> None:
        outcome = await RecallAtKEvaluator().evaluate(
            sample([{"id": "doc-1", "text": "anything"}], [{"id": "doc-1", "text": "other"}])
        )
        assert outcome.score == 1.0

    async def test_falls_back_to_text_when_no_id(self) -> None:
        """Datasets carrying raw passages work without inventing identifiers."""
        outcome = await RecallAtKEvaluator().evaluate(
            sample([{"text": "the capital is paris"}], [{"text": "The capital is Paris"}])
        )
        assert outcome.score == 1.0

    async def test_normalisation_survives_whitespace_and_case(self) -> None:
        """Without this a trailing newline turns a hit into a miss."""
        outcome = await RecallAtKEvaluator().evaluate(sample(["  Doc  One\n"], ["doc one"]))
        assert outcome.score == 1.0

    async def test_normalisation_can_be_disabled(self) -> None:
        evaluator = RecallAtKEvaluator(RetrievalConfig(normalise=False))
        outcome = await evaluator.evaluate(sample(["Doc One"], ["doc one"]))
        assert outcome.score == 0.0


class TestMissingFields:
    async def test_missing_retrieved_field_is_unscoreable(self) -> None:
        with pytest.raises(UnscoreableError, match="retrieved_context"):
            await PrecisionAtKEvaluator().evaluate(sample(None, ["a"]))

    async def test_missing_expected_context_is_unscoreable(self) -> None:
        with pytest.raises(UnscoreableError, match="expected_context"):
            await PrecisionAtKEvaluator().evaluate(sample(["a"], None))

    async def test_non_list_retrieved_field_is_unscoreable(self) -> None:
        bad = EvaluationSample(
            output="x", inputs={"retrieved_context": "not a list"}, expected_context=["a"]
        )
        with pytest.raises(UnscoreableError, match="must be a list"):
            await PrecisionAtKEvaluator().evaluate(bad)
