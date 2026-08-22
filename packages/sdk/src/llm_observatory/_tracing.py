"""The public tracing API: `span()`, `@trace`, and `instrument()`."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from llm_observatory._client import get_client
from llm_observatory._span import (
    Span,
    get_current_span,
    new_span_id,
    new_trace_id,
    reset_current_span,
    set_current_span,
)

F = TypeVar("F", bound=Callable[..., Any])


@contextmanager
def span(
    name: str,
    kind: str = "other",
    *,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Span]:
    """Open a span. Nests automatically under whatever span is already open.

        with span("retrieval", kind="retrieval") as s:
            docs = retriever(question)
            s.set_output(docs)

    Parent linkage comes from a `ContextVar`, so nesting is implicit — you never
    pass a parent around. It works across `await` boundaries and keeps concurrent
    tasks separate.

    The span is always submitted, including when the body raises: a failed
    operation is the single most interesting thing to have a record of, and the
    exception is re-raised untouched so the caller's error handling is unchanged.
    """
    parent = get_current_span()
    current = Span(
        name=name,
        kind=kind,
        trace_id=parent.trace_id if parent else new_trace_id(),
        span_id=new_span_id(),
        parent_span_id=parent.span_id if parent else None,
        metadata=dict(metadata or {}),
    )
    if input is not None:
        current.set_input(input)

    token = set_current_span(current)
    try:
        yield current
    except BaseException as exc:
        current.record_error(exc)
        raise
    finally:
        reset_current_span(token)
        current.finish()
        get_client().submit(current.to_payload())


def trace(
    name: str | None = None,
    kind: str = "chain",
    *,
    capture_input: bool = True,
    capture_output: bool = True,
) -> Callable[[F], F]:
    """Decorator that wraps a function in a span.

        @trace("answer_question")
        def answer(question: str) -> str:
            ...

    Works on sync and async functions alike — the wrapper is chosen by
    inspecting the target, so an async function stays awaitable rather than
    being silently turned into a coroutine-returning sync call.

    Defaults to `kind="chain"` because a decorated function is usually the
    top-level operation; inner steps use `span()` with a more specific kind.
    """

    def decorator(func: F) -> F:
        span_name = name or func.__qualname__

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with span(span_name, kind=kind) as current:
                    if capture_input:
                        current.set_input(_describe_call(func, args, kwargs))
                    result = await func(*args, **kwargs)
                    if capture_output:
                        current.set_output(result)
                    return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with span(span_name, kind=kind) as current:
                if capture_input:
                    current.set_input(_describe_call(func, args, kwargs))
                result = func(*args, **kwargs)
                if capture_output:
                    current.set_output(result)
                return result

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def _describe_call(func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Turn call arguments into a recorded payload.

    Bound to the signature so arguments appear under their parameter names
    rather than as `args[0]` — which is the difference between a readable trace
    and one you have to cross-reference against source.

    `self` is dropped: a method's receiver is almost never the interesting part
    and its repr is often enormous.
    """
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        return {k: v for k, v in bound.arguments.items() if k not in ("self", "cls")}
    except Exception:
        return {"args": list(args), "kwargs": kwargs}


def instrument(client: Any) -> Any:
    """Wrap a vendor client so every model call becomes a span.

        from anthropic import Anthropic
        from openai import OpenAI
        from llm_observatory import instrument

        client = instrument(Anthropic())      # or OpenAI(), or their async twins

    After this, the client's usual call records model, token counts, latency and
    finish reason with no further code. That is the difference between telemetry
    that exists and telemetry that depends on every engineer remembering to call
    `set_tokens` at every call site.

    OpenAI here means *any* OpenAI-compatible endpoint — Groq, Together,
    OpenRouter, vLLM, Ollama — because they all use the same client and the same
    `chat.completions.create` surface. One branch, many vendors, exactly as on
    the platform side.

    Returns a proxy, not a subclass: the vendor SDK's surface is large and
    changes, and delegating by attribute means we do not have to track it.
    Anything not explicitly wrapped passes straight through.

    **Raises on a client it does not recognise.** The SDK's rule is that
    telemetry must never break the host application, and that rule is about the
    request path — a span that cannot be recorded is dropped. Setup is
    different. Returning a proxy that silently traces nothing would leave
    someone staring at an empty dashboard with no error to search for, which is
    the failure this call previously had for OpenAI clients.
    """
    # Duck-typed rather than isinstance. Importing anthropic or openai to
    # identify a client would put them in this package's dependency floor, and
    # ADR 0001 keeps that at httpx alone so a consumer's tree stays small.
    messages = getattr(client, "messages", None)
    if messages is not None and hasattr(messages, "create"):
        return _ClientProxy(client, "messages", _AnthropicMessagesProxy)

    chat = getattr(client, "chat", None)
    if chat is not None and hasattr(getattr(chat, "completions", None), "create"):
        return _ClientProxy(client, "chat", _OpenAIChatProxy)

    raise TypeError(
        f"instrument() does not recognise {type(client).__module__}."
        f"{type(client).__qualname__}. Supported: Anthropic clients (.messages.create) "
        "and OpenAI-compatible clients (.chat.completions.create), sync or async. "
        "For anything else, use the `span()` context manager directly."
    )


class _ClientProxy:
    """Attribute-delegating proxy that wraps one attribute of a vendor client."""

    def __init__(self, wrapped: Any, attribute: str, proxy: type) -> None:
        self._wrapped = wrapped
        self._attribute = attribute
        self._proxy = proxy

    def __getattr__(self, item: str) -> Any:
        value = getattr(self._wrapped, item)
        if item == self._attribute:
            return self._proxy(value)
        return value


class _CreateProxy:
    """Wraps a `create` call, sync or async, leaving everything else alone.

    Whether to await is decided at *call* time from the wrapped callable, not
    baked into a method definition. The previous version defined `create` as
    sync and `acreate` as async, which is not how any of these SDKs are shaped:
    an async client's method is also called `create`, it just returns a
    coroutine.

    The consequence of getting that wrong was silent and bad. The sync wrapper
    would call `create`, receive an un-awaited coroutine, find no `usage` on it,
    close the span, and hand the coroutine back for the caller to await — so
    every async call produced a span with no tokens and a duration of roughly
    zero, while the real call went untraced. Wrong data is worse than no data,
    because nobody investigates a dashboard that looks fine.
    """

    _span_name = "llm"

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped

    def __getattr__(self, item: str) -> Any:
        return getattr(self._wrapped, item)

    def _record(self, current: Span, response: Any) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _describe_request(self, kwargs: dict[str, Any]) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def create(self) -> Any:
        inner = self._wrapped.create

        if inspect.iscoroutinefunction(inner):

            async def acreate(**kwargs: Any) -> Any:
                with span(self._span_name, kind="llm") as current:
                    current.set_model(kwargs.get("model", "unknown"))
                    current.set_input(self._describe_request(kwargs))
                    response = await inner(**kwargs)
                    self._record(current, response)
                    return response

            return acreate

        def create(**kwargs: Any) -> Any:
            with span(self._span_name, kind="llm") as current:
                current.set_model(kwargs.get("model", "unknown"))
                current.set_input(self._describe_request(kwargs))
                response = inner(**kwargs)
                self._record(current, response)
                return response

        return create


class _AnthropicMessagesProxy(_CreateProxy):
    _span_name = "anthropic.messages.create"

    def _describe_request(self, kwargs: dict[str, Any]) -> Any:
        return {
            "messages": kwargs.get("messages"),
            # Anthropic takes the system prompt as a top-level argument rather
            # than as a message, so it would otherwise be missing from the span.
            "system": kwargs.get("system"),
            "max_tokens": kwargs.get("max_tokens"),
        }

    def _record(self, current: Span, response: Any) -> None:
        _safely(current, response, _record_anthropic)


class _OpenAIChatProxy(_CreateProxy):
    _span_name = "openai.chat.completions.create"

    def __getattr__(self, item: str) -> Any:
        value = getattr(self._wrapped, item)
        # `client.chat.completions` is the object that owns `create`.
        if item == "completions":
            return _OpenAICompletionsProxy(value)
        return value


class _OpenAICompletionsProxy(_CreateProxy):
    _span_name = "openai.chat.completions.create"

    def _describe_request(self, kwargs: dict[str, Any]) -> Any:
        return {
            # OpenAI carries the system prompt inside the message array, so
            # unlike Anthropic there is nothing extra to pull out.
            "messages": kwargs.get("messages"),
            "max_tokens": kwargs.get("max_tokens") or kwargs.get("max_completion_tokens"),
        }

    def _record(self, current: Span, response: Any) -> None:
        _safely(current, response, _record_openai)


def _safely(current: Span, response: Any, recorder: Any) -> None:
    """Run a recorder, swallowing anything it raises.

    A vendor SDK upgrade that renames a field must degrade to a span with less
    detail, never raise into the caller's request. Losing token counts is small;
    breaking somebody's model call is not.
    """
    try:
        recorder(current, response)
    except Exception:  # noqa: S110 - see the docstring
        pass


def _record_anthropic(current: Span, response: Any) -> None:
    usage = getattr(response, "usage", None)
    if usage is not None:
        current.set_tokens(
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
        )
    text = "".join(block.text for block in getattr(response, "content", []) if block.type == "text")
    current.set_output({"text": text})
    # The model the response reports, not the one requested: "claude-sonnet-5"
    # goes out and a dated build comes back, and the latter is what ran.
    if getattr(response, "model", None):
        current.set_model(response.model)
    current.set_metadata(stop_reason=getattr(response, "stop_reason", None))


def _record_openai(current: Span, response: Any) -> None:
    usage = getattr(response, "usage", None)
    if usage is not None:
        # Different field names from Anthropic's, for the same two numbers.
        current.set_tokens(
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
        )
    choices = getattr(response, "choices", None) or []
    if choices:
        message = getattr(choices[0], "message", None)
        current.set_output({"text": getattr(message, "content", None) or ""})
        current.set_metadata(finish_reason=getattr(choices[0], "finish_reason", None))
    if getattr(response, "model", None):
        current.set_model(response.model)


def current_trace_id() -> str | None:
    """The active trace id, for correlating your own logs with a trace."""
    active = get_current_span()
    return active.trace_id if active else None
