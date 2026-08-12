"""Anthropic generation provider.

Uses the official SDK rather than raw HTTP so retry/backoff, typed errors and
streaming come from the vendor rather than being reimplemented here.

The interesting part of this module is not the API call — it is the parameter
validation above it, which exists because of a direct collision between two
design decisions:

  * ADR 0004 stores decoding parameters *with* the prompt version, so a version
    authored months ago still carries the `temperature` it was written with.
  * Current Anthropic models **reject** `temperature`, `top_p` and `top_k` with a
    400 rather than ignoring them.

So a perfectly valid stored prompt plus a newer model is a guaranteed failure,
and without a check it fails once per example, mid-run, after the run has
already been paid for. `validate_request` turns that into one clear error at run
request time.
"""

from __future__ import annotations

import time
from typing import Any

from lo_core.config import get_settings
from lo_core.errors import ValidationError
from lo_core.providers.base import (
    GenerationProvider,
    GenerationRequest,
    GenerationResponse,
    ProviderError,
)
from lo_core.providers.pricing import compute_cost, unsupported_sampling_parameters

DEFAULT_MODEL = "claude-opus-5"

# Parameters this provider maps onto explicit SDK arguments. Anything else in a
# prompt version's `parameters` is ignored rather than forwarded, so a stray key
# cannot turn into an unexpected request field.
_PASSTHROUGH: frozenset[str] = frozenset({"temperature", "top_p", "top_k", "stop_sequences"})


def validate_request(model: str, parameters: dict[str, Any]) -> None:
    """Reject a model/parameter combination the API will refuse.

    Called when an eval run is *requested* so the failure is a 422 on one API
    call, rather than N provider 400s discovered partway through a run.
    """
    rejected = unsupported_sampling_parameters(model, parameters)
    if rejected:
        raise ValidationError(
            f"model {model!r} does not accept {', '.join(rejected)}. "
            "This prompt version records decoding parameters that newer models "
            "reject; remove them from the version's parameters, or run against a "
            "model that still accepts them."
        )


class AnthropicProvider(GenerationProvider):
    name = "anthropic"

    def __init__(self, api_key: str | None = None) -> None:
        # Imported lazily so that merely importing the providers package does
        # not require the SDK to be installed, and so a missing key surfaces
        # here with a readable message rather than at the first request.
        from anthropic import AsyncAnthropic

        if api_key is None:
            settings = get_settings()
            secret = settings.anthropic_api_key
            if secret is None:
                raise ValidationError(
                    "LO_ANTHROPIC_API_KEY is not set; configure it or use the "
                    "'fake' provider for offline runs"
                )
            api_key = secret.get_secret_value()

        self._client = AsyncAnthropic(api_key=api_key)

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        import anthropic

        model = request.model or DEFAULT_MODEL
        validate_request(model, request.parameters)

        # The Messages API takes the system prompt as a top-level argument, not
        # as a message with role "system" — so the stored chat structure has to
        # be split here rather than passed through verbatim.
        system_parts = [m.content for m in request.messages if m.role == "system"]
        turns = [
            {"role": m.role, "content": m.content}
            for m in request.messages
            if m.role in ("user", "assistant")
        ]
        if not turns:
            raise ValidationError("prompt must contain at least one user message")

        kwargs: dict[str, Any] = {
            k: v for k, v in request.parameters.items() if k in _PASSTHROUGH and v is not None
        }
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)

        started = time.perf_counter()
        try:
            message = await self._client.messages.create(
                model=model,
                max_tokens=request.max_tokens,
                messages=turns,  # type: ignore[arg-type]
                **kwargs,
            )
        except anthropic.APIStatusError as exc:
            # 429 and 5xx are worth another attempt; a 400 will fail identically
            # every time, and retrying it just burns the run's retry budget.
            retryable = exc.status_code == 429 or exc.status_code >= 500
            raise ProviderError(
                f"anthropic returned {exc.status_code}: {exc.message}", retryable=retryable
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"anthropic connection error: {exc}", retryable=True) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        # A safety refusal is a successful HTTP 200 with an empty content list.
        # Reading content[0] unconditionally would raise IndexError and be
        # reported as a crash rather than as what it is.
        if message.stop_reason == "refusal":
            raise ProviderError("model declined the request (stop_reason=refusal)", retryable=False)

        text = "".join(block.text for block in message.content if block.type == "text")

        input_tokens = message.usage.input_tokens
        output_tokens = message.usage.output_tokens

        return GenerationResponse(
            text=text,
            model=message.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            cost_usd=compute_cost(message.model, input_tokens, output_tokens),
            metadata={"provider": "anthropic", "stop_reason": message.stop_reason},
        )

    async def aclose(self) -> None:
        await self._client.close()
