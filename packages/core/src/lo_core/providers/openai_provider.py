"""OpenAI-compatible generation provider.

One adapter, many vendors. The OpenAI Chat Completions API became the de-facto
interface for text generation, so Groq, Together, OpenRouter, Fireworks, vLLM
and Ollama all speak it. Pointing `base_url` at any of them reuses this module
unchanged:

    LO_GENERATION_PROVIDER=openai
    LO_OPENAI_BASE_URL=https://api.groq.com/openai/v1     # or Together, vLLM, …
    LO_OPENAI_API_KEY=...

Writing six near-identical adapters would mean six places to fix the next time
a response field moves. Writing one means the differences between vendors show
up as *configuration*, which is where they belong — and it makes the platform
usable against a laptop running Ollama, with no account and no spend anywhere.

### What is deliberately not shared with the Anthropic provider

Cost. See `_should_price` below: the same model name costs different amounts at
different gateways, so pricing is only claimed when talking to OpenAI itself.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

from lo_core.config import get_settings
from lo_core.errors import ValidationError
from lo_core.providers.base import (
    GenerationProvider,
    GenerationRequest,
    GenerationResponse,
    ProviderError,
)
from lo_core.providers.pricing import assert_sampling_supported, compute_cost

DEFAULT_MODEL = "gpt-4.1-mini"

# Hosts whose pricing the table in `pricing.py` actually describes.
CANONICAL_OPENAI_HOSTS = frozenset({"api.openai.com"})

# Parameters mapped onto explicit request fields. Anything else in a prompt
# version's `parameters` is ignored rather than forwarded, so a stray key from
# another provider's vocabulary cannot become an unexpected request field.
_PASSTHROUGH: frozenset[str] = frozenset(
    {"temperature", "top_p", "frequency_penalty", "presence_penalty", "seed", "stop"}
)

# Local servers authenticate by not authenticating. The OpenAI client still
# requires *some* string, so supply one rather than making the user invent it.
_PLACEHOLDER_KEY = "not-required"


def _should_price(base_url: str | None) -> bool:
    """Whether the pricing table can be trusted for this endpoint.

    `llama-3.3-70b` costs one thing on Groq, another on Together, and nothing at
    all on a vLLM server you are running yourself. A single table keyed on model
    name cannot express that, and a confidently wrong cost figure on a dashboard
    is worse than an absent one — it is the number someone quotes in a meeting.

    So cost is computed only against OpenAI's own endpoint, and everything else
    records `None`, which the schema already treats as "unknown" rather than
    "free" (see `compute_cost`).
    """
    if base_url is None:
        return True
    host = urlsplit(base_url).hostname
    return host in CANONICAL_OPENAI_HOSTS


class OpenAIProvider(GenerationProvider):
    name = "openai"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        # Imported lazily so importing the providers package does not require
        # the SDK, and so a missing key is a readable error here rather than an
        # obscure one at the first request.
        from openai import AsyncOpenAI

        settings = get_settings()
        if base_url is None:
            base_url = settings.openai_base_url

        if api_key is None:
            secret = settings.openai_api_key
            if secret is not None:
                api_key = secret.get_secret_value()
            elif base_url is not None:
                # A custom endpoint with no key is the normal case for Ollama
                # and for a self-hosted vLLM server.
                api_key = _PLACEHOLDER_KEY
            else:
                raise ValidationError(
                    "LO_OPENAI_API_KEY is not set. Set it, or set LO_OPENAI_BASE_URL "
                    "to a local server (Ollama, vLLM) that does not require one, or "
                    "use the 'fake' provider for offline runs."
                )

        self._base_url = base_url
        self._price = _should_price(base_url)
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    def _build_messages(self, request: GenerationRequest) -> list[dict[str, str]]:
        """Chat Completions takes the system prompt as a message in the array.

        Unlike Anthropic's Messages API, which takes it as a separate top-level
        argument — so the stored chat structure passes through almost verbatim
        here and has to be split there.
        """
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        if not any(m["role"] in ("user", "assistant") for m in messages):
            raise ValidationError("prompt must contain at least one user message")
        return messages

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        import openai

        model = request.model or DEFAULT_MODEL

        assert_sampling_supported(model, request.parameters)

        kwargs: dict[str, Any] = {
            k: v for k, v in request.parameters.items() if k in _PASSTHROUGH and v is not None
        }

        if request.response_schema is not None:
            # Structured outputs, so the judge's response is valid JSON by
            # construction rather than by hope (see GenerationRequest).
            #
            # `strict` requires the schema to set additionalProperties: false
            # and list every property as required. JUDGE_RESPONSE_SCHEMA already
            # does; a schema that does not will be rejected by the API with a
            # clear message, which is better than silently unconstrained output.
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }

        started = time.perf_counter()
        try:
            completion = await self._client.chat.completions.create(
                model=model,
                messages=self._build_messages(request),  # type: ignore[arg-type]
                max_completion_tokens=request.max_tokens,
                **kwargs,
            )
        except openai.APIStatusError as exc:
            # 429 and 5xx are worth another attempt; a 400 fails identically
            # every time and retrying it only burns the run's retry budget.
            retryable = exc.status_code == 429 or exc.status_code >= 500
            detail = str(getattr(exc, "message", "")) or str(exc)
            if not retryable and "response_format" in detail:
                # The most likely incompatibility when pointing at a gateway
                # that is only mostly OpenAI-compatible. Name it, rather than
                # letting the operator debug a bare 400.
                raise ProviderError(
                    f"the endpoint at {self._base_url or 'api.openai.com'} rejected "
                    f"structured outputs ({detail}). Not every OpenAI-compatible "
                    "gateway implements response_format json_schema; use a provider "
                    "that does for judge evaluations.",
                    retryable=False,
                ) from exc
            raise ProviderError(
                f"openai-compatible endpoint returned {exc.status_code}: {detail}",
                retryable=retryable,
            ) from exc
        except openai.APIConnectionError as exc:
            raise ProviderError(
                f"could not reach {self._base_url or 'api.openai.com'}: {exc}", retryable=True
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        if not completion.choices:
            # A content filter can return 200 with no choices. Indexing [0]
            # blindly would surface as an IndexError crash rather than as the
            # provider-side refusal it is.
            raise ProviderError("endpoint returned no choices", retryable=False)

        choice = completion.choices[0]
        text = choice.message.content or ""

        # Usage is optional in the OpenAI schema, and several compatible
        # gateways omit it. None means unknown, and must not become 0 — a run
        # reporting zero tokens looks free rather than unmeasured.
        usage = completion.usage
        input_tokens = usage.prompt_tokens if usage is not None else None
        output_tokens = usage.completion_tokens if usage is not None else None

        cost = None
        if self._price and input_tokens is not None and output_tokens is not None:
            cost = compute_cost(completion.model or model, input_tokens, output_tokens)

        return GenerationResponse(
            text=text,
            model=completion.model or model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            cost_usd=cost,
            metadata={
                "provider": "openai",
                "finish_reason": choice.finish_reason,
                # Recorded so a run against Groq is distinguishable from one
                # against OpenAI months later, when only the stored row remains.
                "base_url": self._base_url or "https://api.openai.com/v1",
            },
        )

    async def aclose(self) -> None:
        await self._client.close()
