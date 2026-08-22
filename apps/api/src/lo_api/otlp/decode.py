"""Decode an OTLP/HTTP export request into a flat list of spans.

Two wire encodings have to be supported, and they are not interchangeable:

**Protobuf** (`application/x-protobuf`) is what the OpenTelemetry SDKs send by
default over HTTP. Supporting it is the difference between "point your exporter
here" and "point your exporter here *and* set the encoding".

**JSON** (`application/json`) is optional in the spec but common in hand-rolled
exporters and in the Collector's `otlphttp` exporter when configured for it.

The trap is that OTLP/JSON is *not* protobuf's canonical JSON mapping. Protobuf
would serialise a `bytes` field as base64; the OTLP specification overrides that
for `trace_id`/`span_id` and requires lowercase hex, because a hex trace id is
the thing every other tool in the ecosystem prints. So the JSON path cannot be
implemented by handing the payload to `json_format.ParseDict` — it decodes ids
itself, and tolerates base64 as well, since exporters built directly on the
protobuf JSON mapping do emit it.

Both paths converge on `OtlpSpan`, so the semantic-convention mapping in
`genai.py` is written once and cannot drift between encodings.
"""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from dataclasses import dataclass, field
from typing import Any

from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2

from lo_core.errors import ValidationError

# Ceiling on the *decompressed* payload.
#
# The request-size limit applies to the bytes on the wire, which for a
# compressed body says nothing about what it becomes in memory: gzip reaches
# roughly 1000:1 on repetitive input, so a 4 MiB request can expand to
# gigabytes. That is a decompression bomb, and an authenticated endpoint is not
# a defence — a project key belongs to code running on someone else's
# infrastructure, and a misconfigured client can do this by accident as easily
# as an attacker can on purpose.
#
# 16 MiB is generous for the 500-span batch limit (roughly 32 KiB per span, with
# prompt and completion text included) while bounding what one request can cost
# a pod that has a 512 MiB memory limit. A client that trips it gets the same
# advice as one that exceeds the span limit: send smaller batches.
MAX_DECOMPRESSED_BYTES = 16 * 1024 * 1024

# gzip's window size, offset to select the gzip wrapper rather than raw deflate.
_GZIP_WBITS = 16 + zlib.MAX_WBITS

SUPPORTED_ENCODINGS = frozenset({"", "identity", "gzip"})

# W3C Trace Context sizes, in hex characters.
TRACE_ID_HEX = 32
SPAN_ID_HEX = 16

# OTLP status codes (opentelemetry/proto/trace/v1/trace.proto, Status.StatusCode).
STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2


@dataclass(frozen=True)
class OtlpEvent:
    """A span event. Only `exception` events are interesting to us today."""

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OtlpSpan:
    """One decoded span, independent of the encoding it arrived in."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_unix_nano: int
    end_unix_nano: int | None
    status_code: int
    status_message: str | None
    attributes: dict[str, Any]
    events: list[OtlpEvent]
    # Resource-level attributes (service.name, deployment.environment, …).
    # Carried per span because a span is what we store; the resource grouping
    # exists only on the wire.
    resource_attributes: dict[str, Any]
    scope_name: str | None


def decompress(body: bytes, content_encoding: str) -> bytes:
    """Undo `Content-Encoding`, with a bound on the result.

    OTLP/HTTP exporters commonly enable gzip — the Collector's `otlphttp`
    exporter does by default — and nothing in ASGI decompresses for us. Without
    this the compressed bytes reach the protobuf parser, which reports a
    malformed payload: a confusing error that points at the wrong thing and
    fails before any of the mapping runs. It is the most likely first contact a
    real user has with this endpoint.

    Decompression is incremental and capped rather than a single
    `gzip.decompress`, which would happily allocate whatever the stream
    expands to.
    """
    encoding = content_encoding.strip().lower()
    if encoding in ("", "identity"):
        return body
    if encoding != "gzip":
        raise ValidationError(
            f"unsupported Content-Encoding {encoding!r}; expected gzip or identity"
        )

    decompressor = zlib.decompressobj(_GZIP_WBITS)
    try:
        # max_length caps the output; anything left over stays in
        # `unconsumed_tail` instead of being allocated.
        decompressed = decompressor.decompress(body, MAX_DECOMPRESSED_BYTES)
    except zlib.error as exc:
        raise ValidationError(f"malformed gzip payload: {exc}") from exc

    if decompressor.unconsumed_tail:
        raise ValidationError(
            f"gzip payload expands beyond {MAX_DECOMPRESSED_BYTES} bytes; "
            "lower the exporter's batch size"
        )
    if not decompressor.eof:
        # Input consumed but the stream never ended: a truncated body, which is
        # a different fault from an oversized one and deserves its own message.
        raise ValidationError("gzip payload is truncated")

    return decompressed


def _hex_from_bytes(raw: bytes) -> str:
    return raw.hex()


def _optional_parent(value: str) -> str | None:
    """A root span's parent id is absent, and "absent" has two encodings.

    Protobuf has no null: an unset `bytes` field arrives as empty, but plenty of
    exporters serialise the field explicitly as eight zero bytes instead. Both
    mean "no parent". Treating the zeros literally would store a span pointing
    at a parent that can never exist, which quietly breaks the tree walk — the
    span is neither a root nor reachable from one, so it vanishes from the trace
    view while still counting towards the rollup.
    """
    if not value or set(value) == {"0"}:
        return None
    return value


def _normalise_id(value: str, expected_hex_len: int, *, field_name: str) -> str:
    """Accept the hex the OTLP spec mandates, and the base64 protobuf produces.

    An exporter written against the protobuf JSON mapping rather than the OTLP
    specification emits base64 for id fields. Rejecting those would be defensible
    and unhelpful — the id is unambiguous either way, because the two encodings
    have different lengths for the same number of bytes.
    """
    if len(value) == expected_hex_len:
        try:
            int(value, 16)
        except ValueError:
            raise ValidationError(f"{field_name} is not valid hex: {value!r}") from None
        return value.lower()

    # base64 of 16 bytes is 24 chars with padding, of 8 bytes is 12.
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError(
            f"{field_name} must be {expected_hex_len} hex characters, got {value!r}"
        ) from None

    if len(decoded) * 2 != expected_hex_len:
        raise ValidationError(
            f"{field_name} must be {expected_hex_len} hex characters, got {value!r}"
        )
    return decoded.hex()


def _anyvalue_to_python(value: common_pb2.AnyValue) -> Any:
    """Unwrap OTLP's `AnyValue` union into an ordinary Python value."""
    which = value.WhichOneof("value")
    if which == "string_value":
        return value.string_value
    if which == "bool_value":
        return value.bool_value
    if which == "int_value":
        return value.int_value
    if which == "double_value":
        return value.double_value
    if which == "array_value":
        return [_anyvalue_to_python(v) for v in value.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _anyvalue_to_python(kv.value) for kv in value.kvlist_value.values}
    if which == "bytes_value":
        return value.bytes_value.hex()
    # An AnyValue with nothing set is legal on the wire and means "no value".
    return None


def _attributes_from_proto(attributes: Any) -> dict[str, Any]:
    return {kv.key: _anyvalue_to_python(kv.value) for kv in attributes}


def _anyvalue_from_json(value: Any) -> Any:
    """The JSON encoding of `AnyValue`: a single-key object naming the type.

    `{"stringValue": "gpt-4"}` rather than a bare `"gpt-4"`. Values that are
    already plain (some exporters cut this corner) pass through unchanged.
    """
    if not isinstance(value, dict):
        return value
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "intValue" in value:
        # int64 is a *string* in protobuf's JSON mapping, because JSON numbers
        # cannot represent the full range. Coerce back, and leave anything
        # unparseable alone rather than losing it.
        raw = value["intValue"]
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if "doubleValue" in value:
        return value["doubleValue"]
    if "arrayValue" in value:
        return [_anyvalue_from_json(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return {
            kv.get("key"): _anyvalue_from_json(kv.get("value"))
            for kv in value["kvlistValue"].get("values", [])
        }
    if "bytesValue" in value:
        return value["bytesValue"]
    return None


def _attributes_from_json(attributes: Any) -> dict[str, Any]:
    if not isinstance(attributes, list):
        return {}
    out: dict[str, Any] = {}
    for kv in attributes:
        if isinstance(kv, dict) and "key" in kv:
            out[kv["key"]] = _anyvalue_from_json(kv.get("value"))
    return out


def _int_or_none(value: Any) -> int | None:
    """Nanosecond timestamps are strings in protobuf's JSON mapping (uint64)."""
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def decode_protobuf(body: bytes) -> list[OtlpSpan]:
    request = trace_service_pb2.ExportTraceServiceRequest()
    try:
        request.ParseFromString(body)
    except (DecodeError, ValueError) as exc:
        raise ValidationError(f"malformed OTLP protobuf payload: {exc}") from exc

    spans: list[OtlpSpan] = []
    for resource_spans in request.resource_spans:
        resource_attributes = _attributes_from_proto(resource_spans.resource.attributes)
        for scope_spans in resource_spans.scope_spans:
            scope_name = scope_spans.scope.name or None
            for span in scope_spans.spans:
                spans.append(
                    OtlpSpan(
                        trace_id=_hex_from_bytes(span.trace_id),
                        span_id=_hex_from_bytes(span.span_id),
                        parent_span_id=_optional_parent(_hex_from_bytes(span.parent_span_id)),
                        name=span.name,
                        start_unix_nano=span.start_time_unix_nano,
                        end_unix_nano=span.end_time_unix_nano or None,
                        status_code=span.status.code,
                        status_message=span.status.message or None,
                        attributes=_attributes_from_proto(span.attributes),
                        events=[
                            OtlpEvent(
                                name=event.name,
                                attributes=_attributes_from_proto(event.attributes),
                            )
                            for event in span.events
                        ],
                        resource_attributes=resource_attributes,
                        scope_name=scope_name,
                    )
                )
    return spans


def decode_json(body: bytes) -> list[OtlpSpan]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"malformed OTLP JSON payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("OTLP JSON payload must be an object")

    spans: list[OtlpSpan] = []
    for resource_spans in payload.get("resourceSpans", []) or []:
        if not isinstance(resource_spans, dict):
            continue
        resource = resource_spans.get("resource") or {}
        resource_attributes = _attributes_from_json(resource.get("attributes"))
        for scope_spans in resource_spans.get("scopeSpans", []) or []:
            if not isinstance(scope_spans, dict):
                continue
            scope_name = (scope_spans.get("scope") or {}).get("name") or None
            for span in scope_spans.get("spans", []) or []:
                if not isinstance(span, dict):
                    continue
                parent = span.get("parentSpanId") or None
                spans.append(
                    OtlpSpan(
                        trace_id=_normalise_id(
                            str(span.get("traceId", "")), TRACE_ID_HEX, field_name="traceId"
                        ),
                        span_id=_normalise_id(
                            str(span.get("spanId", "")), SPAN_ID_HEX, field_name="spanId"
                        ),
                        parent_span_id=(
                            _optional_parent(
                                _normalise_id(str(parent), SPAN_ID_HEX, field_name="parentSpanId")
                            )
                            if parent
                            else None
                        ),
                        name=str(span.get("name") or ""),
                        start_unix_nano=_int_or_none(span.get("startTimeUnixNano")) or 0,
                        end_unix_nano=_int_or_none(span.get("endTimeUnixNano")),
                        status_code=int((span.get("status") or {}).get("code") or STATUS_UNSET),
                        status_message=(span.get("status") or {}).get("message") or None,
                        attributes=_attributes_from_json(span.get("attributes")),
                        events=[
                            OtlpEvent(
                                name=str(event.get("name") or ""),
                                attributes=_attributes_from_json(event.get("attributes")),
                            )
                            for event in span.get("events", []) or []
                            if isinstance(event, dict)
                        ],
                        resource_attributes=resource_attributes,
                        scope_name=scope_name,
                    )
                )
    return spans
