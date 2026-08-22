"""OTLP/HTTP ingest: decoding the wire format and mapping it onto `SpanIngest`."""

from lo_api.otlp.decode import OtlpEvent, OtlpSpan, decode_json, decode_protobuf
from lo_api.otlp.genai import resolve_kind, to_span_ingest

__all__ = [
    "OtlpEvent",
    "OtlpSpan",
    "decode_json",
    "decode_protobuf",
    "resolve_kind",
    "to_span_ingest",
]
