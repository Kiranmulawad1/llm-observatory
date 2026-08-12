"""Embedding-similarity evaluator.

Cosine similarity between the output and the expected output. This is the
workhorse for free-text answers, where exact match is uselessly strict — "Paris"
and "The capital is Paris." are the same answer and score 0.0 under equality.

Prioritised over BLEU/ROUGE deliberately. Those measure n-gram overlap, so they
reward matching *wording* rather than matching *meaning*, and a correct answer
phrased differently scores badly. For LLM output, where paraphrase is the norm,
that produces evals that punish the model for not memorising the reference.

Unlike the deterministic evaluators, this one needs a provider, so it is
constructed with one rather than by config alone.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lo_core.evaluators.base import (
    EvaluationSample,
    Evaluator,
    EvaluatorOutcome,
)
from lo_core.evaluators.registry import register
from lo_core.providers.base import EmbeddingProvider


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class EmbeddingSimilarityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Score at or above which the example counts as passing. Stored separately
    # from the raw score so the threshold can be re-examined later without
    # re-running the embeddings.
    threshold: float = Field(default=0.85, ge=0.0, le=1.0)


@register
class EmbeddingSimilarityEvaluator(Evaluator[EmbeddingSimilarityConfig]):
    name = "embedding_similarity"
    description = (
        "Cosine similarity between the output and the expected output, "
        "using an embedding model. Score is the similarity, clamped to 0.0-1.0."
    )
    Config = EmbeddingSimilarityConfig

    def __init__(
        self,
        config: BaseModel | None = None,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        super().__init__(config)
        self._embedder = embedder

    def bind(self, embedder: EmbeddingProvider) -> None:
        """Attach the provider. Called by the runner after construction.

        The registry builds evaluators from config alone, so the provider is
        injected afterwards rather than being a constructor argument — that
        keeps `build()` uniform across every evaluator.
        """
        self._embedder = embedder

    async def evaluate(self, sample: EvaluationSample) -> EvaluatorOutcome:
        if self._embedder is None:  # pragma: no cover - runner always binds
            raise RuntimeError("embedding_similarity evaluator was not bound to a provider")

        expected = self.require_expected(sample)

        # One call for both texts: batching halves the round trips against a
        # hosted embedder and the per-call overhead against a local one.
        vectors = await self._embedder.embed([sample.output, expected])
        raw = cosine_similarity(vectors[0], vectors[1])

        # Cosine ranges over [-1, 1], but scores are contractually 0.0-1.0 (see
        # evaluators/base.py) and the score column has a CHECK constraint
        # enforcing it. Negative similarity means "unrelated", which is the same
        # actionable signal as zero, so clamping loses nothing.
        score = max(0.0, min(1.0, raw))

        detail: dict[str, Any] = {
            "cosine": round(raw, 6),
            "threshold": self.config.threshold,
            # Recorded because cosine values are not comparable across embedding
            # models — without this, comparing two runs that used different
            # embedders would read as a quality change.
            "embedding_provider": self._embedder.name,
            "dimensions": self._embedder.dimensions,
        }

        return EvaluatorOutcome(
            score=score,
            passed=score >= self.config.threshold,
            detail=detail,
        )
