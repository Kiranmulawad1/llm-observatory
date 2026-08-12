"""Span construction and the active-span context.

Kept separate from the client so the data model stays independent of transport.
"""

from __future__ import annotations

import contextvars
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# W3C Trace Context id widths, in bytes.
_TRACE_ID_BYTES = 16
_SPAN_ID_BYTES = 8

MAX_PAYLOAD_CHARS = 32_000


def new_trace_id() -> str:
    return os.urandom(_TRACE_ID_BYTES).hex()


def new_span_id() -> str:
    return os.urandom(_SPAN_ID_BYTES).hex()


@dataclass
class Span:
    """One timed operation.

    Duration comes from `time.perf_counter()`, a monotonic clock, rather than
    from subtracting the two wall-clock timestamps. Wall time can step backwards
    mid-span when NTP corrects the system clock, which would otherwise produce
    negative durations that quietly poison every latency percentile.
    """

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    kind: str = "other"
    status: str = "ok"

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    duration_ms: int | None = None

    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    prompt_version_id: str | None = None

    error_type: str | None = None
    error_message: str | None = None

    _monotonic_start: float = field(default_factory=time.perf_counter, repr=False)

    # --- the API a user actually calls ------------------------------------

    def set_input(self, value: Any) -> Span:
        self.input = _wrap(value)
        return self

    def set_output(self, value: Any) -> Span:
        self.output = _wrap(value)
        return self

    def set_metadata(self, **values: Any) -> Span:
        self.metadata.update(values)
        return self

    def set_model(self, model: str, prompt_version_id: str | None = None) -> Span:
        self.model = model
        if prompt_version_id is not None:
            self.prompt_version_id = prompt_version_id
        return self

    def set_tokens(
        self, prompt_tokens: int | None = None, completion_tokens: int | None = None
    ) -> Span:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        return self

    def set_cost(self, cost_usd: float) -> Span:
        self.cost_usd = cost_usd
        return self

    def record_error(self, exc: BaseException) -> Span:
        self.status = "error"
        self.error_type = type(exc).__name__
        self.error_message = str(exc)[:2000]
        return self

    def finish(self) -> Span:
        if self.ended_at is None:
            self.ended_at = datetime.now(UTC)
            self.duration_ms = int((time.perf_counter() - self._monotonic_start) * 1000)
        return self

    def to_payload(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name[:200],
            "kind": self.kind,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_ms": self.duration_ms,
            "input": self.input,
            "output": self.output,
            "metadata": self.metadata,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": self.cost_usd,
            "prompt_version_id": self.prompt_version_id,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


def _wrap(value: Any) -> dict[str, Any]:
    """Normalise arbitrary user values into a JSON-safe object.

    Truncated, because a span's input can be an entire retrieved corpus and
    nobody wants their observability tool to be the reason a request body is
    50 MB. Non-serialisable values fall back to `repr` rather than raising —
    instrumentation must never be the thing that breaks a working application.
    """
    if isinstance(value, dict):
        payload = value
    else:
        payload = {"value": value}

    try:
        import json

        text = json.dumps(payload, default=repr, ensure_ascii=False)
    except Exception:
        return {"value": repr(payload)[:MAX_PAYLOAD_CHARS], "truncated": True}

    if len(text) > MAX_PAYLOAD_CHARS:
        return {"value": text[:MAX_PAYLOAD_CHARS], "truncated": True}
    return payload


# The currently-open span, per execution context.
#
# `ContextVar`, not a thread-local: a ContextVar is inherited by asyncio tasks,
# so a span opened before an `await` is still the parent of spans opened after
# it, and concurrent tasks each get their own view. A thread-local would give
# every coroutine on one event-loop thread the same parent — producing a trace
# tree that is confidently wrong.
_current_span: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "lo_current_span", default=None
)


def get_current_span() -> Span | None:
    return _current_span.get()


def set_current_span(span: Span | None) -> contextvars.Token[Span | None]:
    return _current_span.set(span)


def reset_current_span(token: contextvars.Token[Span | None]) -> None:
    _current_span.reset(token)
