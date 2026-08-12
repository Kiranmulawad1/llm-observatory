"""Deterministic providers for tests, CI and offline demos.

These are not a testing shortcut bolted on afterwards — they are the reason the
whole eval engine can be tested at all. A test suite that called a real model
would be non-deterministic (the same prompt gives different text), slow, and
billed on every CI run, so nobody would run it on every commit. These make the
runner's actual logic — concurrency, resume, aggregation, dead-lettering —
verifiable without any of that.

Determinism comes from hashing the input, so the same request always yields the
same output and a test can assert on exact values.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from typing import Any

from lo_core.providers.base import (
    EmbeddingProvider,
    GenerationProvider,
    GenerationRequest,
    GenerationResponse,
    ProviderError,
)
from lo_core.providers.pricing import compute_cost

# A caller can force a failure by including this marker in the prompt, which is
# how the runner's error handling and dead-letter paths get exercised without
# mocking the provider itself.
FAIL_MARKER = "__FAIL__"

FAKE_EMBEDDING_DIMENSIONS = 256


class FakeGenerationProvider(GenerationProvider):
    """Echoes a deterministic transformation of the final user message.

    The output deliberately *contains* the input rather than being pure noise:
    it lets a test configure a real evaluator (regex, exact match) against a
    predictable answer instead of asserting that scoring produced nothing.
    """

    name = "fake"

    def __init__(self, model: str = "fake-model") -> None:
        self._model = model

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        joined = "\n".join(m.content for m in request.messages)

        if FAIL_MARKER in joined:
            raise ProviderError("fake provider was asked to fail", retryable=False)

        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == "user"),
            joined,
        )
        digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:8]

        if request.response_schema is not None:
            text = self._structured(request.response_schema, joined)
        else:
            text = f"{last_user.strip()} [fake:{digest}]"

        # Rough but stable token estimate. Not accurate against any real
        # tokenizer, and not meant to be — its job is to make the cost and token
        # columns exercisable end to end.
        input_tokens = max(1, len(joined) // 4)
        output_tokens = max(1, len(text) // 4)

        return GenerationResponse(
            text=text,
            model=request.model or self._model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=0,
            cost_usd=compute_cost(request.model, input_tokens, output_tokens),
            metadata={"provider": "fake"},
        )

    @staticmethod
    def _structured(schema: dict[str, Any], seed_text: str) -> str:
        """Emit deterministic JSON conforming to `schema`.

        Only handles the shallow object-of-scalars shape the judge uses — this
        is a test double, not a schema-driven data generator, and pretending
        otherwise would be a lot of code nobody exercises.

        Values are derived from a hash of the prompt so a judge test gets a
        stable score it can assert on, while different inputs still produce
        different scores.
        """
        digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
        properties: dict[str, Any] = schema.get("properties", {})
        payload: dict[str, Any] = {}

        for offset, (key, spec) in enumerate(properties.items()):
            kind = spec.get("type")
            if kind == "integer":
                low = int(spec.get("minimum", 0))
                high = int(spec.get("maximum", low + 4))
                span = max(1, high - low + 1)
                payload[key] = low + (digest[offset % len(digest)] % span)
            elif kind == "number":
                payload[key] = round((digest[offset % len(digest)] % 101) / 100, 2)
            elif kind == "boolean":
                payload[key] = digest[offset % len(digest)] % 2 == 0
            else:
                payload[key] = f"fake {key} for {digest[:4].hex()}"

        return json.dumps(payload)


class FakeEmbeddingProvider(EmbeddingProvider):
    """Hash-based embeddings with a real bag-of-words signal.

    Deliberately more than random noise: vectors are built from token hashes, so
    identical text embeds identically (cosine 1.0) and texts sharing words score
    higher than unrelated ones. That makes the similarity evaluator's *logic*
    testable — thresholds, ordering, normalisation — while keeping tests free and
    deterministic. It is emphatically not semantic: paraphrases score low, so
    this is never a substitute for a real embedder in production.
    """

    name = "fake"
    dimensions = FAKE_EMBEDDING_DIMENSIONS

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in re.findall(r"\w+", text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            # Sign from an independent byte so different tokens can cancel,
            # rather than every vector drifting positive.
            vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0

        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            # Empty or punctuation-only input. A zero vector would make cosine
            # similarity undefined, so return a fixed unit vector instead.
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]
