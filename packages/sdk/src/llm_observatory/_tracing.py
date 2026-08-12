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
    """Wrap an Anthropic client so every model call becomes a span.

        from anthropic import Anthropic
        from llm_observatory import instrument

        client = instrument(Anthropic())

    After this, `client.messages.create(...)` records model, token counts,
    latency and stop reason with no further code. That is the difference between
    telemetry that exists and telemetry that depends on every engineer
    remembering to call `set_tokens` at every call site.

    Returns a proxy, not a subclass — the vendor SDK's surface is large and
    changes, and proxying by attribute delegation means we do not have to track
    it. Anything we do not explicitly wrap passes straight through.
    """
    return _InstrumentedClient(client)


class _InstrumentedClient:
    """Attribute-delegating proxy around a vendor client."""

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped

    def __getattr__(self, item: str) -> Any:
        value = getattr(self._wrapped, item)
        if item == "messages":
            return _InstrumentedMessages(value)
        return value


class _InstrumentedMessages:
    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped

    def __getattr__(self, item: str) -> Any:
        return getattr(self._wrapped, item)

    def create(self, **kwargs: Any) -> Any:
        model = kwargs.get("model", "unknown")
        with span("anthropic.messages.create", kind="llm") as current:
            current.set_model(model)
            current.set_input(
                {
                    "messages": kwargs.get("messages"),
                    "system": kwargs.get("system"),
                    "max_tokens": kwargs.get("max_tokens"),
                }
            )
            response = self._wrapped.create(**kwargs)
            _record_response(current, response)
            return response

    async def acreate(self, **kwargs: Any) -> Any:  # pragma: no cover - async client path
        model = kwargs.get("model", "unknown")
        with span("anthropic.messages.create", kind="llm") as current:
            current.set_model(model)
            response = await self._wrapped.create(**kwargs)
            _record_response(current, response)
            return response


def _record_response(current: Span, response: Any) -> None:
    """Pull usage off a vendor response.

    Defensive throughout: a vendor SDK upgrade that renames a field must degrade
    to a span with less detail, never raise into the caller's request. Recording
    a span without token counts is a small loss; breaking the application's model
    call is not.
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is not None:
            current.set_tokens(
                getattr(usage, "input_tokens", None),
                getattr(usage, "output_tokens", None),
            )
        text = "".join(
            block.text for block in getattr(response, "content", []) if block.type == "text"
        )
        current.set_output({"text": text})
        current.set_metadata(stop_reason=getattr(response, "stop_reason", None))
    except Exception:  # noqa: S110 - see the docstring: never raise into the caller
        pass


def current_trace_id() -> str | None:
    """The active trace id, for correlating your own logs with a trace."""
    active = get_current_span()
    return active.trace_id if active else None
