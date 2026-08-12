"""Retrieval metrics: precision@k, recall@k, MRR.

These score the *retriever*, not the generator. For a RAG system that matters
enormously: when an answer is wrong, the first question is whether the model
reasoned badly or was simply never given the right passage, and an
answer-quality metric cannot tell you which.

They need no model call, which makes them the cheapest useful signal a RAG team
can gate CI on — run them on every commit and reserve the judge for the subset
where retrieval was fine and the answer still looks wrong.

**Where the retrieved set comes from.** The dataset item's `inputs` — the caller
ran their retriever and stored what it returned, alongside the ground truth in
`expected_context`. That keeps this phase honest: the platform scores retrieval
it was *given*. Phase 5's retrieval spans become the other source, at which point
these same evaluators read from a trace instead of a dataset field.

**Matching.** Comparison is by normalised exact match on a document identifier or
its text. Deliberately not fuzzy: a threshold on similarity would make the metric
depend on an embedding model, and "recall went up" would become ambiguous between
a better retriever and a different embedder — the exact ambiguity the rest of this
platform exists to remove.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lo_core.evaluators.base import (
    EvaluationSample,
    Evaluator,
    EvaluatorOutcome,
    UnscoreableError,
)
from lo_core.evaluators.registry import register

_WHITESPACE = re.compile(r"\s+")


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Where the retrieved documents live on the dataset item's inputs.
    retrieved_field: str = "retrieved_context"
    # Cut-off. Null means "score the whole retrieved list", which is what you
    # want for recall when the retriever already applies its own limit.
    k: int | None = Field(default=None, ge=1, le=1000)
    # Key to compare on when documents are objects rather than strings.
    id_field: str = "id"
    # Fold case and collapse whitespace before comparing. On by default because
    # otherwise a trailing newline makes a correct retrieval look like a miss.
    normalise: bool = True


def _key(document: Any, id_field: str, normalise: bool) -> str:
    """Reduce a document to the string used for matching."""
    if isinstance(document, dict):
        raw = document.get(id_field)
        if raw is None:
            # No id: fall back to text, so datasets that carry raw passages
            # still work without inventing identifiers.
            raw = document.get("text") or document.get("content") or ""
        value = str(raw)
    else:
        value = str(document)

    if normalise:
        value = _WHITESPACE.sub(" ", value).strip().casefold()
    return value


def _extract(sample: EvaluationSample, config: RetrievalConfig) -> tuple[list[str], list[str]]:
    raw_retrieved = sample.inputs.get(config.retrieved_field)
    if raw_retrieved is None:
        raise UnscoreableError(
            f"dataset item has no {config.retrieved_field!r} field to score retrieval against"
        )
    if not isinstance(raw_retrieved, list):
        raise UnscoreableError(f"{config.retrieved_field!r} must be a list of documents")
    if sample.expected_context is None:
        raise UnscoreableError("dataset item has no expected_context")

    retrieved = [_key(d, config.id_field, config.normalise) for d in raw_retrieved]
    if config.k is not None:
        retrieved = retrieved[: config.k]

    relevant = [_key(d, config.id_field, config.normalise) for d in sample.expected_context]
    return retrieved, relevant


def _detail(retrieved: list[str], relevant: list[str], hits: list[str]) -> dict[str, Any]:
    return {
        "retrieved_count": len(retrieved),
        "relevant_count": len(relevant),
        "hits": len(hits),
        # The passages the retriever should have found and did not. This is the
        # actionable half of a bad score.
        "missed": [r for r in relevant if r not in retrieved][:10],
    }


@register
class PrecisionAtKEvaluator(Evaluator[RetrievalConfig]):
    """Of the documents retrieved, what fraction were relevant?

    Low precision means the context window is being filled with noise, which
    costs tokens and gives the model more opportunity to ground an answer in the
    wrong passage.
    """

    name = "retrieval_precision"
    description = "precision@k: fraction of retrieved documents that are relevant."
    Config = RetrievalConfig

    async def evaluate(self, sample: EvaluationSample) -> EvaluatorOutcome:
        retrieved, relevant = _extract(sample, self.config)
        if not retrieved:
            # Nothing retrieved is a real retrieval failure, not a gap: precision
            # is 0 because the retriever returned nothing useful.
            return EvaluatorOutcome(
                score=0.0, passed=False, detail={"error": "no documents retrieved"}
            )

        relevant_set = set(relevant)
        hits = [d for d in retrieved if d in relevant_set]
        score = len(hits) / len(retrieved)
        return EvaluatorOutcome(score=score, detail=_detail(retrieved, relevant, hits))


@register
class RecallAtKEvaluator(Evaluator[RetrievalConfig]):
    """Of the relevant documents, what fraction were retrieved?

    The metric that matters most for RAG: a passage the retriever never returned
    is one the model cannot possibly use, and no amount of prompt tuning fixes it.
    """

    name = "retrieval_recall"
    description = "recall@k: fraction of relevant documents that were retrieved."
    Config = RetrievalConfig

    async def evaluate(self, sample: EvaluationSample) -> EvaluatorOutcome:
        retrieved, relevant = _extract(sample, self.config)
        if not relevant:
            # No ground truth to recall — a dataset gap, not a score of zero.
            raise UnscoreableError("dataset item lists no relevant documents")

        retrieved_set = set(retrieved)
        hits = [d for d in relevant if d in retrieved_set]
        score = len(hits) / len(relevant)
        return EvaluatorOutcome(score=score, detail=_detail(retrieved, relevant, hits))


@register
class MRREvaluator(Evaluator[RetrievalConfig]):
    """Reciprocal rank of the first relevant document.

    Rank-sensitive, unlike precision and recall: retrieving the right passage
    first scores 1.0, tenth scores 0.1. That matters because models attend
    unevenly across a long context, so a correct passage buried at position 20
    is not equivalent to the same passage at position 1.
    """

    name = "retrieval_mrr"
    description = "Mean reciprocal rank: 1/rank of the first relevant document, else 0."
    Config = RetrievalConfig

    async def evaluate(self, sample: EvaluationSample) -> EvaluatorOutcome:
        retrieved, relevant = _extract(sample, self.config)
        relevant_set = set(relevant)

        for position, document in enumerate(retrieved, start=1):
            if document in relevant_set:
                detail = _detail(retrieved, relevant, [document])
                detail["first_relevant_rank"] = position
                return EvaluatorOutcome(score=1.0 / position, detail=detail)

        detail = _detail(retrieved, relevant, [])
        detail["first_relevant_rank"] = None
        return EvaluatorOutcome(score=0.0, passed=False, detail=detail)
