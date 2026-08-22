"""Map OpenTelemetry GenAI semantic conventions onto `SpanIngest`.

The span model was designed against OpenTelemetry from the start (see the
docstring in `lo_core.db.models.telemetry`), so this is a translation and not a
second data model. Ids, the flat parent-pointer tree, and the status vocabulary
already line up; what needs interpreting is the *attribute* layer, because the
GenAI conventions are young and every instrumentation library sits at a slightly
different point in their history.

Three generations are handled, because all three are in the wild right now:

  gen_ai.usage.input_tokens      current OTel GenAI conventions
  gen_ai.usage.prompt_tokens     earlier spelling, still emitted widely
  llm.usage.prompt_tokens        OpenLLMetry / Traceloop's own namespace

Reading all three is a few lines here and saves every user of this platform from
patching their instrumentation. Where they disagree, the most current spelling
wins — an exporter emitting both is mid-upgrade, and the new key is the one it
will keep.

Nothing is discarded. Attributes that are not promoted to a column land in
`metadata`, so an unrecognised convention degrades to "still searchable" rather
than "silently dropped".
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from lo_api.otlp.decode import STATUS_ERROR, STATUS_OK, OtlpSpan
from lo_core.db.models.telemetry import SpanKind, SpanStatus
from lo_core.schemas.telemetry import SpanIngest

# Column widths from the model. Over-long values are truncated rather than
# rejected: losing a span because someone named an operation verbosely is a
# worse outcome than storing a clipped name.
MAX_NAME = 200
MAX_MODEL = 100
MAX_ERROR_TYPE = 200

NANOS_PER_MS = 1_000_000

# `gen_ai.prompt.0.content` and friends.
_INDEXED_MESSAGE = re.compile(
    r"^(?:gen_ai|llm)\.(?P<role_group>prompt|completion)\.(?P<index>\d+)\.(?P<field>\w+)$"
)

# Vector stores, as reported in `db.system`. Presence of one is a far more
# reliable retrieval signal than anything in the span name.
_VECTOR_DB_SYSTEMS = frozenset(
    {
        "chroma",
        "chromadb",
        "elasticsearch",
        "lancedb",
        "marqo",
        "milvus",
        "opensearch",
        "pgvector",
        "pinecone",
        "qdrant",
        "redis",
        "weaviate",
    }
)

# Traceloop's own span taxonomy, which is the closest thing to an explicit
# statement of intent that any of these libraries emit.
_TRACELOOP_KINDS: dict[str, SpanKind] = {
    "workflow": "chain",
    "task": "chain",
    "agent": "chain",
    "tool": "tool",
    "llm": "llm",
}

_OPERATION_KINDS: dict[str, SpanKind] = {
    "chat": "llm",
    "text_completion": "llm",
    "generate_content": "llm",
    "embeddings": "embedding",
    "embedding": "embedding",
    "rerank": "rerank",
    "execute_tool": "tool",
}


def _first(attributes: dict[str, Any], *keys: str) -> Any:
    """Return the first key that is present and not empty.

    Order encodes precedence: pass the current convention first, older
    spellings after.
    """
    for key in keys:
        value = attributes.get(key)
        if value is not None and value != "":
            return value
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):  # bool is an int subclass; not a token count
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _truncate(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text[:limit] if text else None


def _timestamp(unix_nano: int) -> datetime:
    return datetime.fromtimestamp(unix_nano / 1_000_000_000, tz=UTC)


def resolve_kind(span: OtlpSpan) -> SpanKind:
    """Decide which of our span kinds an OTLP span represents.

    Ordered most-explicit first. The one heuristic — a parentless span being a
    `chain` — is last, so it only applies when nothing in the attributes said
    otherwise. Span *names* are deliberately never consulted: they are free text
    chosen by whoever wrote the instrumentation, and keying behaviour off them
    produces a classifier that silently changes meaning when someone renames a
    function.
    """
    attributes = span.attributes

    traceloop_kind = attributes.get("traceloop.span.kind")
    if isinstance(traceloop_kind, str):
        mapped = _TRACELOOP_KINDS.get(traceloop_kind.lower())
        if mapped is not None:
            return mapped

    operation = _first(attributes, "gen_ai.operation.name", "llm.request.type")
    if isinstance(operation, str):
        mapped = _OPERATION_KINDS.get(operation.lower())
        if mapped is not None:
            return mapped

    db_system = attributes.get("db.system")
    if isinstance(db_system, str) and db_system.lower() in _VECTOR_DB_SYSTEMS:
        return "retrieval"

    # Any model attribute at all means a model was called.
    if (
        _first(
            attributes,
            "gen_ai.system",
            "gen_ai.request.model",
            "gen_ai.response.model",
            "llm.request.model",
            "llm.response.model",
        )
        is not None
    ):
        return "llm"

    if span.parent_span_id is None:
        return "chain"

    return "other"


def _extract_messages(attributes: dict[str, Any], group: str) -> list[dict[str, Any]] | None:
    """Rebuild `gen_ai.prompt.{i}.{field}` attribute families into a message list.

    OpenTelemetry attributes are flat, so instrumentation libraries encode a
    chat history by flattening the index into the key. Reassembling it here is
    what turns an unreadable pile of `gen_ai.prompt.3.content` keys into
    something the trace viewer can render as a conversation.
    """
    collected: dict[int, dict[str, Any]] = {}
    for key, value in attributes.items():
        match = _INDEXED_MESSAGE.match(key)
        if match is None or match.group("role_group") != group:
            continue
        collected.setdefault(int(match.group("index")), {})[match.group("field")] = value

    if not collected:
        return None
    return [collected[index] for index in sorted(collected)]


def _maybe_json(value: Any) -> Any:
    """Traceloop puts a JSON document in a string attribute. Unwrap it if so."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _payload(attributes: dict[str, Any], group: str, *plain_keys: str) -> dict[str, Any] | None:
    messages = _extract_messages(attributes, group)
    if messages is not None:
        return {"messages": messages}

    plain = _first(attributes, *plain_keys)
    if plain is None:
        return None
    unwrapped = _maybe_json(plain)
    # The column is JSONB, so a bare string has to be wrapped to stay an object.
    return unwrapped if isinstance(unwrapped, dict) else {"value": unwrapped}


def _error_from_events(span: OtlpSpan) -> tuple[str | None, str | None]:
    """Pull the exception out of the `exception` span event.

    OTel records a thrown exception as an event with `exception.type` and
    `exception.message`, not as span attributes. A span can carry several; the
    first is the one that matters, since later ones are usually the same error
    re-raised on the way out.
    """
    for event in span.events:
        if event.name != "exception":
            continue
        return (
            _truncate(event.attributes.get("exception.type"), MAX_ERROR_TYPE),
            _truncate(event.attributes.get("exception.message"), 10_000),
        )
    return None, None


# Attribute keys promoted to real columns. Kept out of `metadata` so the same
# fact is not stored twice under two names.
_PROMOTED_KEYS = frozenset(
    {
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.usage.completion_tokens",
        "gen_ai.usage.cost",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.prompt_tokens",
        "gen_ai.usage.total_cost",
        "llm.request.model",
        "llm.request.type",
        "llm.response.model",
        "llm.usage.completion_tokens",
        "llm.usage.prompt_tokens",
        "llm.usage.total_tokens",
        "traceloop.entity.input",
        "traceloop.entity.output",
        "traceloop.span.kind",
    }
)


def _metadata(span: OtlpSpan) -> dict[str, Any]:
    """Everything not promoted to a column, plus the resource identity.

    Resource attributes are namespaced under `resource` rather than merged, so
    that a `service.name` set on the resource cannot be confused with one set on
    the span — and so the trace viewer can show "which service emitted this"
    without guessing.
    """
    metadata: dict[str, Any] = {
        key: value
        for key, value in span.attributes.items()
        if key not in _PROMOTED_KEYS and _INDEXED_MESSAGE.match(key) is None
    }

    if span.resource_attributes:
        metadata["resource"] = dict(span.resource_attributes)
    if span.scope_name:
        metadata["otel.scope"] = span.scope_name
    # Recorded so a trace can be traced back to how it arrived, which matters
    # the first time a mapping looks wrong and nobody remembers the source.
    metadata["otel.ingest"] = "otlp"
    return metadata


def to_span_ingest(span: OtlpSpan) -> SpanIngest:
    """Translate one decoded OTLP span into the platform's ingest schema."""
    attributes = span.attributes

    duration_ms: int | None = None
    if span.end_unix_nano is not None:
        delta = span.end_unix_nano - span.start_unix_nano
        # Wall-clock time can step backwards mid-span (NTP), which is exactly
        # why the native SDK measures duration from a monotonic clock instead.
        # An OTLP span gives us only the two timestamps, so a negative result is
        # reported as "unknown" rather than clamped to zero — zero would be a
        # measurement, and this is the absence of one.
        duration_ms = delta // NANOS_PER_MS if delta >= 0 else None

    error_type, error_message = _error_from_events(span)
    status: SpanStatus = "ok"
    if span.status_code == STATUS_ERROR:
        status = "error"
        if error_message is None:
            error_message = span.status_message
    elif span.status_code == STATUS_OK:
        status = "ok"

    prompt_tokens = _as_int(
        _first(
            attributes,
            "gen_ai.usage.input_tokens",
            "gen_ai.usage.prompt_tokens",
            "llm.usage.prompt_tokens",
        )
    )
    completion_tokens = _as_int(
        _first(
            attributes,
            "gen_ai.usage.output_tokens",
            "gen_ai.usage.completion_tokens",
            "llm.usage.completion_tokens",
        )
    )

    return SpanIngest(
        trace_id=span.trace_id,
        span_id=span.span_id,
        parent_span_id=span.parent_span_id,
        # A span must have a name to be readable in a list; OTLP permits an
        # empty one, so fall back rather than dropping the span.
        name=_truncate(span.name, MAX_NAME) or "unnamed",
        kind=resolve_kind(span),
        status=status,
        started_at=_timestamp(span.start_unix_nano),
        ended_at=_timestamp(span.end_unix_nano) if span.end_unix_nano else None,
        duration_ms=duration_ms,
        input=_payload(attributes, "prompt", "gen_ai.prompt", "traceloop.entity.input"),
        output=_payload(attributes, "completion", "gen_ai.completion", "traceloop.entity.output"),
        metadata=_metadata(span),
        # Response model over request model: the request may say "gpt-4" while
        # the response says "gpt-4-0613", and the one that answers "what
        # actually served this call" is the response.
        model=_truncate(
            _first(
                attributes,
                "gen_ai.response.model",
                "gen_ai.request.model",
                "llm.response.model",
                "llm.request.model",
            ),
            MAX_MODEL,
        ),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        # Not an OTel convention; emitted by several libraries and too useful to
        # ignore when it is there.
        cost_usd=_as_decimal(_first(attributes, "gen_ai.usage.cost", "gen_ai.usage.total_cost")),
        error_type=error_type,
        error_message=error_message,
    )
