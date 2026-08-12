"""Providers, pricing, and the sampling-parameter compatibility check."""

from __future__ import annotations

from decimal import Decimal

import pytest

from lo_core.errors import ValidationError
from lo_core.evaluators.similarity import cosine_similarity
from lo_core.providers import get_embedding_provider, get_generation_provider
from lo_core.providers.base import GenerationRequest, ProviderError
from lo_core.providers.fake import FAIL_MARKER
from lo_core.providers.pricing import (
    compute_cost,
    unsupported_sampling_parameters,
)
from lo_core.schemas.prompt import RenderedMessage


def request(text: str, model: str = "fake-model") -> GenerationRequest:
    return GenerationRequest(messages=[RenderedMessage(role="user", content=text)], model=model)


class TestFakeGeneration:
    async def test_is_deterministic(self) -> None:
        """The property the whole test suite depends on."""
        provider = get_generation_provider("fake")
        first = await provider.generate(request("hello"))
        second = await provider.generate(request("hello"))
        assert first.text == second.text

    async def test_output_contains_input(self) -> None:
        """Lets tests configure real evaluators against a predictable answer."""
        provider = get_generation_provider("fake")
        response = await provider.generate(request("Where is my order?"))
        assert response.text.startswith("Where is my order?")

    async def test_different_inputs_differ(self) -> None:
        provider = get_generation_provider("fake")
        a = await provider.generate(request("one"))
        b = await provider.generate(request("two"))
        assert a.text != b.text

    async def test_fail_marker_raises(self) -> None:
        """The hook that lets runner error paths be tested without mocking."""
        provider = get_generation_provider("fake")
        with pytest.raises(ProviderError):
            await provider.generate(request(f"please {FAIL_MARKER} now"))

    async def test_reports_tokens(self) -> None:
        provider = get_generation_provider("fake")
        response = await provider.generate(request("some input text"))
        assert response.input_tokens and response.input_tokens > 0
        assert response.output_tokens and response.output_tokens > 0

    async def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown generation provider"):
            get_generation_provider("nope")


class TestFakeEmbeddings:
    async def test_identical_text_is_identical_vector(self) -> None:
        embedder = get_embedding_provider("fake")
        a, b = await embedder.embed(["the same text", "the same text"])
        assert cosine_similarity(a, b) == pytest.approx(1.0)

    async def test_shared_words_score_higher_than_unrelated(self) -> None:
        """Enough signal to test threshold logic, without pretending to be semantic."""
        embedder = get_embedding_provider("fake")
        base, overlapping, unrelated = await embedder.embed(
            [
                "the capital of france is paris",
                "paris is the capital of france indeed",
                "quantum chromodynamics lattice",
            ]
        )
        assert cosine_similarity(base, overlapping) > cosine_similarity(base, unrelated)

    async def test_empty_text_does_not_produce_zero_vector(self) -> None:
        """A zero vector would make cosine similarity undefined."""
        embedder = get_embedding_provider("fake")
        (vector,) = await embedder.embed([""])
        assert any(v != 0.0 for v in vector)

    async def test_vectors_are_unit_length(self) -> None:
        embedder = get_embedding_provider("fake")
        (vector,) = await embedder.embed(["some text here"])
        assert sum(v * v for v in vector) == pytest.approx(1.0)


class TestPricing:
    def test_known_model_cost(self) -> None:
        # claude-opus-5: $5/M input, $25/M output
        cost = compute_cost("claude-opus-5", 1_000_000, 1_000_000)
        assert cost == Decimal("30.00")

    def test_unknown_model_returns_none_not_zero(self) -> None:
        """Zero would make a dashboard total silently wrong; None is visibly incomplete."""
        assert compute_cost("some-unknown-model", 1000, 1000) is None

    def test_partial_tokens(self) -> None:
        cost = compute_cost("claude-haiku-4-5", 500_000, 100_000)
        assert cost == Decimal("1.00")  # 0.5 * $1 + 0.1 * $5


class TestSamplingCompatibility:
    def test_flags_temperature_on_rejecting_model(self) -> None:
        """The collision: prompt registry stores temperature, new models 400 on it."""
        assert unsupported_sampling_parameters(
            "claude-opus-5", {"temperature": 0.0, "model": "x"}
        ) == ["temperature"]

    def test_flags_all_sampling_parameters(self) -> None:
        found = unsupported_sampling_parameters(
            "claude-sonnet-5", {"temperature": 0.2, "top_p": 0.9, "top_k": 5}
        )
        assert found == ["temperature", "top_k", "top_p"]

    def test_older_models_still_accept_sampling(self) -> None:
        assert unsupported_sampling_parameters("claude-haiku-4-5", {"temperature": 0.5}) == []

    def test_no_sampling_parameters_is_clean(self) -> None:
        assert unsupported_sampling_parameters("claude-opus-5", {"max_tokens": 1024}) == []

    def test_validate_request_raises_with_actionable_message(self) -> None:
        from lo_core.providers.anthropic_provider import validate_request

        with pytest.raises(ValidationError, match="does not accept temperature"):
            validate_request("claude-opus-5", {"temperature": 0.0})

    def test_validate_request_passes_for_supported_model(self) -> None:
        from lo_core.providers.anthropic_provider import validate_request

        validate_request("claude-haiku-4-5", {"temperature": 0.0})
