"""OTLP decoding and GenAI semantic-convention mapping.

Pure translation, so these need no database. The cases are chosen around the
things that actually differ between instrumentation libraries — three
generations of token attribute, two ways of expressing a chat history, and the
id encodings — because that is where a mapping quietly loses data rather than
failing loudly.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.resource.v1 import resource_pb2
from opentelemetry.proto.trace.v1 import trace_pb2

from lo_api.otlp import OtlpEvent, OtlpSpan, decode_json, decode_protobuf, to_span_ingest
from lo_api.otlp.genai import resolve_kind
from lo_core.errors import ValidationError

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
PARENT_ID = "a1b2c3d4e5f60718"

START_NS = 1_755_000_000_000_000_000
END_NS = START_NS + 1_500_000_000  # +1.5s


def make_span(
    *,
    attributes: dict[str, object] | None = None,
    parent: str | None = PARENT_ID,
    name: str = "chat gpt-4",
    status_code: int = 0,
    status_message: str | None = None,
    events: list[OtlpEvent] | None = None,
    end_unix_nano: int | None = END_NS,
    resource_attributes: dict[str, object] | None = None,
) -> OtlpSpan:
    return OtlpSpan(
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        parent_span_id=parent,
        name=name,
        start_unix_nano=START_NS,
        end_unix_nano=end_unix_nano,
        status_code=status_code,
        status_message=status_message,
        attributes=attributes or {},
        events=events or [],
        resource_attributes=resource_attributes or {},
        scope_name=None,
    )


class TestTokenConventions:
    """Three generations of token attribute are in the wild simultaneously."""

    @pytest.mark.parametrize(
        ("prompt_key", "completion_key"),
        [
            ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"),
            ("gen_ai.usage.prompt_tokens", "gen_ai.usage.completion_tokens"),
            ("llm.usage.prompt_tokens", "llm.usage.completion_tokens"),
        ],
    )
    def test_each_generation_is_understood(self, prompt_key: str, completion_key: str) -> None:
        span = to_span_ingest(make_span(attributes={prompt_key: 120, completion_key: 45}))
        assert span.prompt_tokens == 120
        assert span.completion_tokens == 45

    def test_current_convention_wins_over_older_spelling(self) -> None:
        """An exporter mid-upgrade emits both. The new key is the one it keeps."""
        span = to_span_ingest(
            make_span(
                attributes={
                    "gen_ai.usage.input_tokens": 100,
                    "gen_ai.usage.prompt_tokens": 999,
                    "llm.usage.prompt_tokens": 888,
                }
            )
        )
        assert span.prompt_tokens == 100

    def test_string_encoded_ints_are_parsed(self) -> None:
        """int64 crosses OTLP/JSON as a string, per protobuf's JSON mapping."""
        span = to_span_ingest(make_span(attributes={"gen_ai.usage.input_tokens": "77"}))
        assert span.prompt_tokens == 77

    def test_nonsense_token_counts_are_dropped_not_crashed(self) -> None:
        span = to_span_ingest(make_span(attributes={"gen_ai.usage.input_tokens": "many", "x": 1}))
        assert span.prompt_tokens is None

    def test_negative_token_count_is_rejected(self) -> None:
        """The column is `ge=0`; a negative would fail validation on write."""
        span = to_span_ingest(make_span(attributes={"gen_ai.usage.input_tokens": -5}))
        assert span.prompt_tokens is None

    def test_bool_is_not_a_token_count(self) -> None:
        """`bool` is an `int` subclass in Python; True must not become 1 token."""
        span = to_span_ingest(make_span(attributes={"gen_ai.usage.input_tokens": True}))
        assert span.prompt_tokens is None


class TestModelAndCost:
    def test_response_model_beats_request_model(self) -> None:
        """The request says gpt-4; the response says which build actually ran."""
        span = to_span_ingest(
            make_span(
                attributes={
                    "gen_ai.request.model": "gpt-4",
                    "gen_ai.response.model": "gpt-4-0613",
                }
            )
        )
        assert span.model == "gpt-4-0613"

    def test_traceloop_namespace_is_read(self) -> None:
        span = to_span_ingest(make_span(attributes={"llm.request.model": "claude-3-opus"}))
        assert span.model == "claude-3-opus"

    def test_cost_is_captured_when_present(self) -> None:
        span = to_span_ingest(make_span(attributes={"gen_ai.usage.cost": 0.00123}))
        assert span.cost_usd == Decimal("0.00123")

    def test_no_cost_convention_means_none(self) -> None:
        assert to_span_ingest(make_span()).cost_usd is None

    def test_overlong_model_is_truncated_not_rejected(self) -> None:
        span = to_span_ingest(make_span(attributes={"gen_ai.request.model": "m" * 500}))
        assert span.model is not None
        assert len(span.model) == 100


class TestKindResolution:
    @pytest.mark.parametrize(
        ("attributes", "expected"),
        [
            ({"gen_ai.operation.name": "chat"}, "llm"),
            ({"gen_ai.operation.name": "embeddings"}, "embedding"),
            ({"gen_ai.operation.name": "rerank"}, "rerank"),
            ({"llm.request.type": "embedding"}, "embedding"),
            ({"traceloop.span.kind": "workflow"}, "chain"),
            ({"traceloop.span.kind": "tool"}, "tool"),
            ({"db.system": "qdrant"}, "retrieval"),
            ({"db.system": "pinecone"}, "retrieval"),
            ({"gen_ai.request.model": "gpt-4"}, "llm"),
            ({"gen_ai.system": "anthropic"}, "llm"),
        ],
    )
    def test_attribute_driven_kinds(self, attributes: dict[str, object], expected: str) -> None:
        assert resolve_kind(make_span(attributes=attributes)) == expected

    def test_explicit_kind_beats_inference(self) -> None:
        """A span with a model attribute is still a tool if it says it is."""
        span = make_span(
            attributes={"traceloop.span.kind": "tool", "gen_ai.request.model": "gpt-4"}
        )
        assert resolve_kind(span) == "tool"

    def test_parentless_span_defaults_to_chain(self) -> None:
        assert resolve_kind(make_span(attributes={}, parent=None)) == "chain"

    def test_unknown_child_span_is_other(self) -> None:
        assert resolve_kind(make_span(attributes={"http.method": "GET"})) == "other"

    def test_span_name_never_decides_the_kind(self) -> None:
        """Names are free text; keying behaviour off them breaks on a rename."""
        assert resolve_kind(make_span(attributes={}, name="retrieve_documents")) == "other"


class TestMessages:
    def test_indexed_prompt_attributes_become_a_message_list(self) -> None:
        """OpenLLMetry flattens a chat history into indexed attribute keys."""
        span = to_span_ingest(
            make_span(
                attributes={
                    "gen_ai.prompt.0.role": "system",
                    "gen_ai.prompt.0.content": "You are terse.",
                    "gen_ai.prompt.1.role": "user",
                    "gen_ai.prompt.1.content": "Hello",
                    "gen_ai.completion.0.role": "assistant",
                    "gen_ai.completion.0.content": "Hi.",
                }
            )
        )
        assert span.input == {
            "messages": [
                {"role": "system", "content": "You are terse."},
                {"role": "user", "content": "Hello"},
            ]
        }
        assert span.output == {"messages": [{"role": "assistant", "content": "Hi."}]}

    def test_message_order_follows_index_not_attribute_order(self) -> None:
        """Attribute maps are unordered, and 10 must not sort before 2."""
        attributes = {f"gen_ai.prompt.{i}.content": f"m{i}" for i in (10, 2, 0, 1)}
        span = to_span_ingest(make_span(attributes=attributes))
        assert span.input is not None
        assert [m["content"] for m in span.input["messages"]] == ["m0", "m1", "m2", "m10"]

    def test_traceloop_json_payload_is_unwrapped(self) -> None:
        span = to_span_ingest(
            make_span(
                attributes={"traceloop.entity.input": json.dumps({"query": "what is a hypertable"})}
            )
        )
        assert span.input == {"query": "what is a hypertable"}

    def test_plain_string_payload_is_wrapped_to_stay_an_object(self) -> None:
        """The column is JSONB and the schema expects an object, not a scalar."""
        span = to_span_ingest(make_span(attributes={"gen_ai.prompt": "just text"}))
        assert span.input == {"value": "just text"}

    def test_indexed_messages_are_not_duplicated_into_metadata(self) -> None:
        span = to_span_ingest(make_span(attributes={"gen_ai.prompt.0.content": "hi"}))
        assert not any(key.startswith("gen_ai.prompt.") for key in span.metadata)


class TestStatusAndErrors:
    def test_error_status_maps_to_error(self) -> None:
        span = to_span_ingest(make_span(status_code=2, status_message="boom"))
        assert span.status == "error"
        assert span.error_message == "boom"

    def test_unset_status_is_ok(self) -> None:
        """UNSET is the default and means "nothing went wrong", not "unknown"."""
        assert to_span_ingest(make_span(status_code=0)).status == "ok"

    def test_exception_event_supplies_type_and_message(self) -> None:
        """OTel records a throw as a span *event*, not as span attributes."""
        span = to_span_ingest(
            make_span(
                status_code=2,
                events=[
                    OtlpEvent(
                        name="exception",
                        attributes={
                            "exception.type": "RateLimitError",
                            "exception.message": "429 from provider",
                        },
                    )
                ],
            )
        )
        assert span.error_type == "RateLimitError"
        assert span.error_message == "429 from provider"

    def test_non_exception_events_are_ignored(self) -> None:
        span = to_span_ingest(
            make_span(events=[OtlpEvent(name="cache_hit", attributes={"key": "x"})])
        )
        assert span.error_type is None


class TestTimestampsAndDuration:
    def test_nanoseconds_become_utc_datetimes(self) -> None:
        span = to_span_ingest(make_span())
        assert span.started_at == datetime.fromtimestamp(START_NS / 1e9, tz=UTC)
        assert span.duration_ms == 1500

    def test_unfinished_span_has_no_end_or_duration(self) -> None:
        span = to_span_ingest(make_span(end_unix_nano=None))
        assert span.ended_at is None
        assert span.duration_ms is None

    def test_backwards_clock_yields_unknown_duration_not_zero(self) -> None:
        """Wall clocks step backwards under NTP. Zero would be a measurement."""
        span = to_span_ingest(make_span(end_unix_nano=START_NS - 5_000_000))
        assert span.duration_ms is None


class TestMetadata:
    def test_unmapped_attributes_survive(self) -> None:
        span = to_span_ingest(make_span(attributes={"my.custom.flag": "yes"}))
        assert span.metadata["my.custom.flag"] == "yes"

    def test_promoted_attributes_are_not_stored_twice(self) -> None:
        span = to_span_ingest(make_span(attributes={"gen_ai.request.model": "gpt-4"}))
        assert "gen_ai.request.model" not in span.metadata
        assert span.model == "gpt-4"

    def test_resource_attributes_are_namespaced(self) -> None:
        """A service.name on the resource must not be confused with one on the span."""
        span = to_span_ingest(
            make_span(
                attributes={"service.name": "span-level"},
                resource_attributes={"service.name": "checkout-api"},
            )
        )
        assert span.metadata["resource"]["service.name"] == "checkout-api"
        assert span.metadata["service.name"] == "span-level"

    def test_ingest_source_is_recorded(self) -> None:
        assert to_span_ingest(make_span()).metadata["otel.ingest"] == "otlp"


class TestNameHandling:
    def test_empty_name_gets_a_placeholder(self) -> None:
        """OTLP permits an empty name; our schema requires min_length=1."""
        assert to_span_ingest(make_span(name="")).name == "unnamed"

    def test_overlong_name_is_truncated(self) -> None:
        assert len(to_span_ingest(make_span(name="n" * 400)).name) == 200


# --- Wire decoding ---------------------------------------------------------


def build_protobuf_request(*, parent: bytes = b"", attrs: dict[str, str] | None = None) -> bytes:
    span = trace_pb2.Span(
        trace_id=bytes.fromhex(TRACE_ID),
        span_id=bytes.fromhex(SPAN_ID),
        parent_span_id=parent,
        name="chat",
        start_time_unix_nano=START_NS,
        end_time_unix_nano=END_NS,
        status=trace_pb2.Status(code=trace_pb2.Status.STATUS_CODE_OK),
        attributes=[
            common_pb2.KeyValue(key=k, value=common_pb2.AnyValue(string_value=v))
            for k, v in (attrs or {}).items()
        ],
    )
    return trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            trace_pb2.ResourceSpans(
                resource=resource_pb2.Resource(
                    attributes=[
                        common_pb2.KeyValue(
                            key="service.name",
                            value=common_pb2.AnyValue(string_value="checkout-api"),
                        )
                    ]
                ),
                scope_spans=[trace_pb2.ScopeSpans(spans=[span])],
            )
        ]
    ).SerializeToString()


class TestProtobufDecoding:
    def test_ids_become_hex(self) -> None:
        [span] = decode_protobuf(build_protobuf_request())
        assert span.trace_id == TRACE_ID
        assert span.span_id == SPAN_ID

    def test_all_zero_parent_means_root(self) -> None:
        """Protobuf has no null, so a root span sends eight zero bytes."""
        [span] = decode_protobuf(build_protobuf_request(parent=b"\x00" * 8))
        assert span.parent_span_id is None

    def test_real_parent_is_preserved(self) -> None:
        [span] = decode_protobuf(build_protobuf_request(parent=bytes.fromhex(PARENT_ID)))
        assert span.parent_span_id == PARENT_ID

    def test_resource_attributes_are_carried_per_span(self) -> None:
        [span] = decode_protobuf(build_protobuf_request())
        assert span.resource_attributes == {"service.name": "checkout-api"}

    def test_malformed_payload_is_a_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            decode_protobuf(b"\xff\xfe\xfd not protobuf")


class TestJsonDecoding:
    def _payload(self, span: dict[str, object]) -> bytes:
        return json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]}).encode()

    def test_hex_ids_as_the_otlp_spec_requires(self) -> None:
        [span] = decode_json(
            self._payload(
                {
                    "traceId": TRACE_ID,
                    "spanId": SPAN_ID,
                    "name": "chat",
                    "startTimeUnixNano": str(START_NS),
                    "endTimeUnixNano": str(END_NS),
                }
            )
        )
        assert (span.trace_id, span.span_id) == (TRACE_ID, SPAN_ID)
        assert span.start_unix_nano == START_NS

    def test_base64_ids_from_protobuf_json_mapping_are_accepted(self) -> None:
        """OTLP mandates hex, but exporters built on protobuf JSON emit base64."""
        [span] = decode_json(
            self._payload(
                {
                    "traceId": base64.b64encode(bytes.fromhex(TRACE_ID)).decode(),
                    "spanId": base64.b64encode(bytes.fromhex(SPAN_ID)).decode(),
                    "name": "chat",
                    "startTimeUnixNano": str(START_NS),
                }
            )
        )
        assert (span.trace_id, span.span_id) == (TRACE_ID, SPAN_ID)

    def test_anyvalue_wrappers_are_unwrapped(self) -> None:
        [span] = decode_json(
            self._payload(
                {
                    "traceId": TRACE_ID,
                    "spanId": SPAN_ID,
                    "name": "chat",
                    "startTimeUnixNano": str(START_NS),
                    "attributes": [
                        {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4"}},
                        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "42"}},
                        {"key": "stream", "value": {"boolValue": True}},
                        {"key": "temp", "value": {"doubleValue": 0.7}},
                    ],
                }
            )
        )
        assert span.attributes["gen_ai.request.model"] == "gpt-4"
        assert span.attributes["gen_ai.usage.input_tokens"] == 42
        assert span.attributes["stream"] is True
        assert span.attributes["temp"] == 0.7

    def test_garbage_id_is_rejected_clearly(self) -> None:
        with pytest.raises(ValidationError, match="traceId"):
            decode_json(self._payload({"traceId": "nope", "spanId": SPAN_ID, "name": "x"}))

    def test_malformed_json_is_a_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            decode_json(b"{not json")

    def test_empty_export_decodes_to_nothing(self) -> None:
        assert decode_json(b'{"resourceSpans": []}') == []


class TestDecompression:
    """`Content-Encoding: gzip`, which OTLP exporters commonly enable.

    Nothing in ASGI decompresses for us, so without this the compressed bytes
    reach the protobuf parser and surface as "malformed payload" — an error
    pointing at the wrong thing entirely.
    """

    def test_identity_and_absent_pass_through(self) -> None:
        from lo_api.otlp import decompress

        assert decompress(b"raw bytes", "") == b"raw bytes"
        assert decompress(b"raw bytes", "identity") == b"raw bytes"

    def test_gzip_round_trips(self) -> None:
        import gzip

        from lo_api.otlp import decompress

        payload = build_protobuf_request()
        assert decompress(gzip.compress(payload), "gzip") == payload

    def test_header_case_and_whitespace_are_tolerated(self) -> None:
        import gzip

        from lo_api.otlp import decompress

        assert decompress(gzip.compress(b"x"), " GZIP ") == b"x"

    def test_unknown_encoding_names_what_is_supported(self) -> None:
        from lo_api.otlp import decompress

        with pytest.raises(ValidationError, match="gzip or identity"):
            decompress(b"...", "br")

    def test_malformed_gzip_is_a_validation_error(self) -> None:
        from lo_api.otlp import decompress

        with pytest.raises(ValidationError, match="malformed gzip"):
            decompress(b"\x1f\x8b not really gzip", "gzip")

    def test_truncated_stream_says_so(self) -> None:
        import gzip

        from lo_api.otlp import decompress

        truncated = gzip.compress(b"a" * 5000)[:-40]
        with pytest.raises(ValidationError, match="truncated"):
            decompress(truncated, "gzip")

    def test_decompression_bomb_is_refused(self) -> None:
        """A small request must not be allowed to become a large allocation.

        The request-size limit applies to the bytes on the wire, which for a
        compressed body says nothing about what they expand to. Authentication
        is not a defence here: an ingest key runs on someone else's machine, and
        a misconfigured client trips this as easily as an attacker.
        """
        import gzip

        from lo_api.otlp import MAX_DECOMPRESSED_BYTES, decompress

        bomb = gzip.compress(b"\0" * (MAX_DECOMPRESSED_BYTES + 1024))
        # Small on the wire, far too large once expanded.
        assert len(bomb) < 100_000

        with pytest.raises(ValidationError, match="expands beyond"):
            decompress(bomb, "gzip")

    def test_payload_at_the_limit_still_decompresses(self) -> None:
        """The bound must not reject a legitimate payload just under it."""
        import gzip

        from lo_api.otlp import MAX_DECOMPRESSED_BYTES, decompress

        payload = b"\0" * (MAX_DECOMPRESSED_BYTES - 1)
        assert len(decompress(gzip.compress(payload), "gzip")) == len(payload)
