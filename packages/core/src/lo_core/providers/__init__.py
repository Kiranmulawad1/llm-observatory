"""Provider construction.

Providers are built by name so an eval run records *which* provider it used and
can be reproduced later. Construction is lazy — importing this package must not
require the Anthropic SDK or the optional embedding extras to be installed,
because the API image carries neither.
"""

from __future__ import annotations

from lo_core.errors import ValidationError
from lo_core.providers.base import (
    EmbeddingProvider,
    GenerationProvider,
    GenerationRequest,
    GenerationResponse,
    ProviderError,
)
from lo_core.providers.fake import FakeEmbeddingProvider, FakeGenerationProvider

GENERATION_PROVIDERS = ("fake", "anthropic", "openai")
EMBEDDING_PROVIDERS = ("fake", "local")


def get_generation_provider(name: str) -> GenerationProvider:
    if name == "fake":
        return FakeGenerationProvider()
    if name == "anthropic":
        from lo_core.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider()
    if name == "openai":
        # Covers every OpenAI-compatible endpoint — Groq, Together, OpenRouter,
        # vLLM, Ollama — selected by LO_OPENAI_BASE_URL rather than by name, so
        # the registry does not grow an entry per vendor.
        from lo_core.providers.openai_provider import OpenAIProvider

        return OpenAIProvider()
    raise ValidationError(
        f"unknown generation provider {name!r}; available: {', '.join(GENERATION_PROVIDERS)}"
    )


def get_embedding_provider(name: str) -> EmbeddingProvider:
    if name == "fake":
        return FakeEmbeddingProvider()
    if name == "local":
        from lo_core.providers.embeddings import LocalEmbeddingProvider

        return LocalEmbeddingProvider()
    raise ValidationError(
        f"unknown embedding provider {name!r}; available: {', '.join(EMBEDDING_PROVIDERS)}"
    )


__all__ = [
    "EMBEDDING_PROVIDERS",
    "GENERATION_PROVIDERS",
    "EmbeddingProvider",
    "FakeEmbeddingProvider",
    "FakeGenerationProvider",
    "GenerationProvider",
    "GenerationRequest",
    "GenerationResponse",
    "ProviderError",
    "get_embedding_provider",
    "get_generation_provider",
]
