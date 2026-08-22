"""The OpenAI-compatible provider.

The interesting behaviour is not the API call — it is everything around it that
differs from the Anthropic adapter: one module has to serve OpenAI, Groq,
Together, OpenRouter, vLLM and Ollama, which differ in whether they need a key,
whether they report usage, and what their tokens cost.

The client is stubbed rather than mocked at the HTTP layer: these assert on the
adapter's decisions, and a real request would need a key, a network and a bill.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from lo_core.errors import ValidationError
from lo_core.providers import GENERATION_PROVIDERS, get_generation_provider
from lo_core.providers.base import GenerationRequest, ProviderError
from lo_core.providers.openai_provider import DEFAULT_MODEL, OpenAIProvider, _should_price
from lo_core.schemas.prompt import RenderedMessage


@dataclass
class _Message:
    content: str | None


@dataclass
class _Choice:
    message: _Message
    finish_reason: str = "stop"


@dataclass
class _Usage:
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _Completion:
    choices: list[_Choice]
    model: str
    usage: _Usage | None


class _StubCompletions:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.captured: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.captured = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _StubClient:
    def __init__(self, result: Any) -> None:
        self.chat = type("chat", (), {"completions": _StubCompletions(result)})()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def make_provider(
    result: Any,
    *,
    base_url: str | None = None,
    api_key: str = "sk-test",
) -> tuple[OpenAIProvider, _StubClient]:
    provider = OpenAIProvider(api_key=api_key, base_url=base_url)
    client = _StubClient(result)
    provider._client = client  # type: ignore[assignment]
    provider._price = _should_price(base_url)
    return provider, client


def request(**kwargs: Any) -> GenerationRequest:
    defaults: dict[str, Any] = {
        "messages": [RenderedMessage(role="user", content="hello")],
        "model": "gpt-4.1-mini",
    }
    return GenerationRequest(**{**defaults, **kwargs})


# Sentinel so `usage=None` means "the gateway reported none", not "use the
# default" — which is exactly the case one of the tests below is about.
_DEFAULT_USAGE = _Usage(prompt_tokens=10, completion_tokens=5)


def completion(
    text: str = "hi",
    model: str = "gpt-4.1-mini",
    usage: _Usage | None = _DEFAULT_USAGE,
) -> _Completion:
    return _Completion(
        choices=[_Choice(message=_Message(content=text))],
        model=model,
        usage=usage,
    )


class TestRegistration:
    def test_provider_is_selectable_by_name(self) -> None:
        assert "openai" in GENERATION_PROVIDERS

    def test_unknown_provider_still_lists_the_options(self) -> None:
        with pytest.raises(ValidationError, match="openai"):
            get_generation_provider("groq")


class TestCredentialHandling:
    def test_missing_key_with_no_base_url_is_an_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LO_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("LO_OPENAI_BASE_URL", raising=False)
        from lo_core.config import get_settings

        get_settings.cache_clear()
        with pytest.raises(ValidationError, match="LO_OPENAI_API_KEY"):
            OpenAIProvider()

    def test_local_endpoint_needs_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ollama and vLLM authenticate by not authenticating.

        The client still demands some string, so the provider supplies one
        rather than making the operator invent a fake key.
        """
        monkeypatch.delenv("LO_OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("LO_OPENAI_BASE_URL", "http://localhost:11434/v1")
        from lo_core.config import get_settings

        get_settings.cache_clear()
        provider = OpenAIProvider()
        assert provider.name == "openai"


class TestPricingBoundary:
    """The same model name costs different amounts at different gateways."""

    @pytest.mark.parametrize(
        ("base_url", "priced"),
        [
            (None, True),
            ("https://api.openai.com/v1", True),
            ("https://api.groq.com/openai/v1", False),
            ("https://openrouter.ai/api/v1", False),
            ("http://localhost:11434/v1", False),
            ("http://localhost:8000/v1", False),
        ],
    )
    def test_only_openai_itself_is_priced(self, base_url: str | None, priced: bool) -> None:
        assert _should_price(base_url) is priced

    async def test_openai_endpoint_records_cost(self) -> None:
        provider, _ = make_provider(completion())
        response = await provider.generate(request())
        assert response.cost_usd == Decimal("10") * Decimal("0.40") / Decimal(1_000_000) + Decimal(
            "5"
        ) * Decimal("1.60") / Decimal(1_000_000)

    async def test_third_party_gateway_records_no_cost(self) -> None:
        """None, not zero. An unknown cost must not render as free."""
        provider, _ = make_provider(
            completion(model="llama-3.3-70b"), base_url="https://api.groq.com/openai/v1"
        )
        response = await provider.generate(request(model="llama-3.3-70b"))
        assert response.cost_usd is None
        assert response.input_tokens == 10

    async def test_base_url_is_recorded_on_the_response(self) -> None:
        """Months later the stored row is all that says which vendor ran this."""
        provider, _ = make_provider(completion(), base_url="https://api.groq.com/openai/v1")
        response = await provider.generate(request())
        assert response.metadata["base_url"] == "https://api.groq.com/openai/v1"


class TestUsageReporting:
    async def test_missing_usage_is_unknown_not_zero(self) -> None:
        """Several compatible gateways omit usage entirely."""
        provider, _ = make_provider(completion(usage=None))
        response = await provider.generate(request())
        assert response.input_tokens is None
        assert response.output_tokens is None
        assert response.cost_usd is None


class TestRequestShape:
    async def test_system_message_stays_in_the_message_array(self) -> None:
        """Unlike Anthropic, which takes it as a separate top-level argument."""
        provider, client = make_provider(completion())
        await provider.generate(
            request(
                messages=[
                    RenderedMessage(role="system", content="be terse"),
                    RenderedMessage(role="user", content="hi"),
                ]
            )
        )
        sent = client.chat.completions.captured["messages"]
        assert sent[0] == {"role": "system", "content": "be terse"}
        assert len(sent) == 2

    async def test_prompt_without_a_user_turn_is_rejected(self) -> None:
        provider, _ = make_provider(completion())
        with pytest.raises(ValidationError, match="user message"):
            await provider.generate(
                request(messages=[RenderedMessage(role="system", content="only system")])
            )

    async def test_known_parameters_pass_through(self) -> None:
        provider, client = make_provider(completion())
        await provider.generate(request(parameters={"temperature": 0.2, "seed": 7}))
        assert client.chat.completions.captured["temperature"] == 0.2
        assert client.chat.completions.captured["seed"] == 7

    async def test_unknown_parameters_are_dropped_not_forwarded(self) -> None:
        """A key from another vendor's vocabulary must not become a request field."""
        provider, client = make_provider(completion())
        await provider.generate(request(parameters={"top_k": 40, "thinking": True}))
        assert "top_k" not in client.chat.completions.captured
        assert "thinking" not in client.chat.completions.captured

    async def test_reasoning_model_rejects_sampling_parameters_before_the_call(self) -> None:
        """One clear error, not N provider 400s discovered mid-run."""
        provider, client = make_provider(completion(model="o3-mini"))
        with pytest.raises(ValidationError, match="temperature"):
            await provider.generate(request(model="o3-mini", parameters={"temperature": 0.5}))
        assert client.chat.completions.captured == {}

    async def test_versioned_reasoning_model_is_matched_by_prefix(self) -> None:
        provider, _ = make_provider(completion())
        with pytest.raises(ValidationError):
            await provider.generate(request(model="o3-mini-2025-01-31", parameters={"top_p": 0.9}))

    async def test_structured_output_is_requested_strictly(self) -> None:
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        provider, client = make_provider(completion())
        await provider.generate(request(response_schema=schema))
        fmt = client.chat.completions.captured["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["strict"] is True
        assert fmt["json_schema"]["schema"] == schema

    async def test_default_model_is_used_when_none_given(self) -> None:
        provider, client = make_provider(completion())
        await provider.generate(request(model=""))
        assert client.chat.completions.captured["model"] == DEFAULT_MODEL


class TestErrorMapping:
    def _status_error(self, status: int, message: str = "bad") -> Exception:
        import openai

        exc = openai.APIStatusError.__new__(openai.APIStatusError)
        exc.status_code = status  # type: ignore[attr-defined]
        exc.message = message  # type: ignore[attr-defined]
        return exc

    @pytest.mark.parametrize(("status", "retryable"), [(429, True), (500, True), (400, False)])
    async def test_retryability_follows_status(self, status: int, retryable: bool) -> None:
        provider, _ = make_provider(self._status_error(status))
        with pytest.raises(ProviderError) as caught:
            await provider.generate(request())
        assert caught.value.retryable is retryable

    async def test_unsupported_structured_output_gets_a_specific_message(self) -> None:
        """The likeliest incompatibility on a not-quite-compatible gateway."""
        provider, _ = make_provider(
            self._status_error(400, "response_format is not supported"),
            base_url="https://api.groq.com/openai/v1",
        )
        with pytest.raises(ProviderError, match="structured outputs"):
            await provider.generate(request(response_schema={"type": "object"}))

    async def test_connection_failure_is_retryable_and_names_the_endpoint(self) -> None:
        import openai

        exc = openai.APIConnectionError.__new__(openai.APIConnectionError)
        Exception.__init__(exc, "refused")
        provider, _ = make_provider(exc, base_url="http://localhost:11434/v1")
        with pytest.raises(ProviderError, match="localhost:11434") as caught:
            await provider.generate(request())
        assert caught.value.retryable is True

    async def test_empty_choices_is_a_provider_error_not_an_indexerror(self) -> None:
        """A content filter can return 200 with no choices at all."""
        provider, _ = make_provider(_Completion(choices=[], model="gpt-4.1-mini", usage=None))
        with pytest.raises(ProviderError, match="no choices"):
            await provider.generate(request())

    async def test_null_content_becomes_empty_string(self) -> None:
        provider, _ = make_provider(
            _Completion(
                choices=[_Choice(message=_Message(content=None))],
                model="gpt-4.1-mini",
                usage=None,
            )
        )
        assert (await provider.generate(request())).text == ""
