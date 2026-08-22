"""Provider contracts for generation and embeddings.

The eval engine never imports a vendor SDK directly. It asks a `GenerationProvider`
for text and an `EmbeddingProvider` for vectors, which buys three things that
matter more than the indirection costs:

  * **Tests are free and deterministic.** The fake provider makes the whole
    runner testable without a network call, an API key, or a bill — and without
    the flakiness of asserting on real model output.
  * **Cost accounting lives in one place.** Every provider returns token counts
    and a computed cost, so `eval_results.cost_usd` is populated the same way
    regardless of who generated the text.
  * **Adding a vendor is additive.** A second provider is a new module and a
    registry entry, not an edit to the runner.
"""

from __future__ import annotations

import abc
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from lo_core.schemas.prompt import RenderedMessage


@dataclass(frozen=True)
class GenerationRequest:
    """One completion request, provider-agnostic."""

    messages: Sequence[RenderedMessage]
    model: str
    max_tokens: int = 4096
    # Everything else the prompt version recorded (temperature, top_p, thinking,
    # …). Passed through as-is; each provider decides what it can honour and
    # rejects what it cannot. See AnthropicProvider for why that rejection is
    # explicit rather than silent.
    parameters: dict[str, Any] = field(default_factory=dict)

    # A JSON Schema the response must conform to, when set.
    #
    # This exists for the judge. Asking a model to "rate 1-5 and explain" and
    # then regex-ing a number out of the prose is how judge evals rot silently:
    # the model rephrases, the pattern stops matching, and every example scores
    # null while the run still reports success. Constraining the response
    # server-side makes a parse failure impossible rather than merely unlikely.
    response_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class GenerationResponse:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    cost_usd: Decimal | None = None
    # Provider-specific extras worth keeping: stop reason, refusal category.
    metadata: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """A provider call failed. Distinguishes retryable from permanent."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class GenerationProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def generate(self, request: GenerationRequest) -> GenerationResponse: ...

    async def generate_measured(self, request: GenerationRequest) -> GenerationResponse:
        """`generate`, wrapped in the platform's own latency and error metrics.

        Wrapped here rather than inside each provider so a new vendor is
        instrumented by existing — the same reasoning that put the
        sampling-parameter check in one shared place. Callers use this; the
        subclass only implements the API call.

        Labelled by provider, never by model: model names come from
        user-authored prompt versions, so labelling by them would let a tenant
        create unbounded series in the platform's monitoring.
        """
        from lo_core import metrics

        operation = "judge" if request.response_schema is not None else "generate"
        started = time.perf_counter()
        try:
            response = await self.generate(request)
        except ProviderError as exc:
            metrics.provider_errors.labels(
                provider=self.name, retryable=str(exc.retryable).lower()
            ).inc()
            raise
        except Exception:
            # Anything not already a ProviderError is a bug on our side rather
            # than the vendor's, but it is still a failed model call and hiding
            # it would make the error rate look better than it is.
            metrics.provider_errors.labels(provider=self.name, retryable="false").inc()
            raise
        finally:
            metrics.provider_duration.labels(provider=self.name, operation=operation).observe(
                time.perf_counter() - started
            )
        return response

    async def aclose(self) -> None:
        """Release any client resources. Called once when a run finishes.

        Concrete by design, not abstract: most providers hold nothing to close,
        and forcing every one of them to write an empty override is noise.
        """
        return None


class EmbeddingProvider(abc.ABC):
    name: str
    # Surfaced so the similarity evaluator can record which space a score came
    # from — cosine values are not comparable across embedding models, and a
    # run compared against another run that used a different embedder would
    # otherwise look like a quality change.
    dimensions: int

    @abc.abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def aclose(self) -> None:
        return None
