"""OTLP/HTTP ingest, end to end.

The unit tests cover attribute mapping in isolation. These cover the things
only a real request and a real database can show: that an OpenTelemetry
exporter's actual wire format is accepted, that the spans land in the same
tables the native SDK writes to, and that a trace assembled from spans arriving
in the wrong order still comes out right.
"""

from __future__ import annotations

import gzip
import json
import uuid
from typing import Any

import httpx
import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.resource.v1 import resource_pb2
from opentelemetry.proto.trace.v1 import trace_pb2

pytestmark = pytest.mark.integration

OTLP_PATH = "/otlp/v1/traces"
PROTOBUF = "application/x-protobuf"

START_NS = 1_755_000_000_000_000_000


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from lo_core.ratelimit import RateLimitResult

    async def allow(*args: Any, **kwargs: Any) -> RateLimitResult:
        return RateLimitResult(allowed=True, limit=6000, remaining=6000, retry_after=0)

    monkeypatch.setattr("lo_api.routers.otlp.check_rate_limit", allow)


def slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def hex_id(length: int) -> str:
    return uuid.uuid4().hex[:length].ljust(length, "0")


async def make_project_with_key(client: httpx.AsyncClient) -> tuple[str, str]:
    name = slug("otlp")
    assert (await client.post("/projects", json={"slug": name, "name": "Test"})).status_code == 201
    response = await client.post(
        f"/projects/{name}/api-keys", json={"name": "otel", "scopes": ["ingest"]}
    )
    assert response.status_code == 201, response.text
    return name, response.json()["key"]


def proto_span(
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None = None,
    name: str = "op",
    start_ns: int = START_NS,
    duration_ns: int = 1_000_000_000,
    attributes: dict[str, Any] | None = None,
    status_code: int = 0,
) -> trace_pb2.Span:
    def any_value(value: Any) -> common_pb2.AnyValue:
        if isinstance(value, bool):
            return common_pb2.AnyValue(bool_value=value)
        if isinstance(value, int):
            return common_pb2.AnyValue(int_value=value)
        if isinstance(value, float):
            return common_pb2.AnyValue(double_value=value)
        return common_pb2.AnyValue(string_value=str(value))

    return trace_pb2.Span(
        trace_id=bytes.fromhex(trace_id),
        span_id=bytes.fromhex(span_id),
        parent_span_id=bytes.fromhex(parent_span_id) if parent_span_id else b"",
        name=name,
        start_time_unix_nano=start_ns,
        end_time_unix_nano=start_ns + duration_ns,
        status=trace_pb2.Status(code=status_code),
        attributes=[
            common_pb2.KeyValue(key=k, value=any_value(v)) for k, v in (attributes or {}).items()
        ],
    )


def export_request(*spans: trace_pb2.Span, service_name: str = "checkout-api") -> bytes:
    return trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            trace_pb2.ResourceSpans(
                resource=resource_pb2.Resource(
                    attributes=[
                        common_pb2.KeyValue(
                            key="service.name",
                            value=common_pb2.AnyValue(string_value=service_name),
                        )
                    ]
                ),
                scope_spans=[
                    trace_pb2.ScopeSpans(
                        scope=common_pb2.InstrumentationScope(
                            name="opentelemetry.instrumentation.anthropic"
                        ),
                        spans=list(spans),
                    )
                ],
            )
        ]
    ).SerializeToString()


class TestAuth:
    async def test_no_credential_is_401(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            OTLP_PATH,
            content=export_request(proto_span(trace_id=hex_id(32), span_id=hex_id(16))),
            headers={"content-type": PROTOBUF, "Authorization": ""},
        )
        assert response.status_code == 401

    async def test_admin_token_cannot_ingest(self, client: httpx.AsyncClient) -> None:
        """Same rule as the native endpoint: ingest needs a project key."""
        response = await client.post(
            OTLP_PATH,
            content=export_request(proto_span(trace_id=hex_id(32), span_id=hex_id(16))),
            headers={"content-type": PROTOBUF},
        )
        assert response.status_code == 403

    async def test_key_without_ingest_scope_is_403(self, client: httpx.AsyncClient) -> None:
        name = slug("otlp")
        await client.post("/projects", json={"slug": name, "name": "Test"})
        key = (
            await client.post(f"/projects/{name}/api-keys", json={"name": "ro", "scopes": ["read"]})
        ).json()["key"]

        response = await client.post(
            OTLP_PATH,
            content=export_request(proto_span(trace_id=hex_id(32), span_id=hex_id(16))),
            headers={"content-type": PROTOBUF, "Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 403


class TestProtobufIngest:
    async def test_genai_span_lands_with_columns_populated(self, client: httpx.AsyncClient) -> None:
        name, key = await make_project_with_key(client)
        trace_id, span_id = hex_id(32), hex_id(16)

        response = await client.post(
            OTLP_PATH,
            content=export_request(
                proto_span(
                    trace_id=trace_id,
                    span_id=span_id,
                    name="chat anthropic",
                    attributes={
                        "gen_ai.system": "anthropic",
                        "gen_ai.operation.name": "chat",
                        "gen_ai.request.model": "claude-3-5-sonnet",
                        "gen_ai.response.model": "claude-3-5-sonnet-20241022",
                        "gen_ai.usage.input_tokens": 1200,
                        "gen_ai.usage.output_tokens": 340,
                    },
                )
            ),
            headers={"content-type": PROTOBUF, "Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 200, response.text

        detail = await client.get(f"/projects/{name}/traces/{trace_id}")
        assert detail.status_code == 200, detail.text
        root = detail.json()["root"]
        assert root["kind"] == "llm"
        assert root["model"] == "claude-3-5-sonnet-20241022"
        assert root["prompt_tokens"] == 1200
        assert root["completion_tokens"] == 340
        assert root["metadata"]["resource"]["service.name"] == "checkout-api"

    async def test_response_is_valid_protobuf(self, client: httpx.AsyncClient) -> None:
        """An exporter parses this body; JSON here would look like data loss."""
        _, key = await make_project_with_key(client)
        response = await client.post(
            OTLP_PATH,
            content=export_request(proto_span(trace_id=hex_id(32), span_id=hex_id(16))),
            headers={"content-type": PROTOBUF, "Authorization": f"Bearer {key}"},
        )
        assert response.headers["content-type"].startswith(PROTOBUF)
        decoded = trace_service_pb2.ExportTraceServiceResponse()
        decoded.ParseFromString(response.content)
        assert decoded.partial_success.rejected_spans == 0

    async def test_empty_export_is_accepted(self, client: httpx.AsyncClient) -> None:
        """Exporters flush empty batches; this must not be an error."""
        _, key = await make_project_with_key(client)
        response = await client.post(
            OTLP_PATH,
            content=trace_service_pb2.ExportTraceServiceRequest().SerializeToString(),
            headers={"content-type": PROTOBUF, "Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 200

    async def test_unsupported_content_type_is_rejected(self, client: httpx.AsyncClient) -> None:
        _, key = await make_project_with_key(client)
        response = await client.post(
            OTLP_PATH,
            content=b"whatever",
            headers={"content-type": "text/plain", "Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 422


class TestJsonIngest:
    async def test_otlp_json_is_accepted(self, client: httpx.AsyncClient) -> None:
        name, key = await make_project_with_key(client)
        trace_id, span_id = hex_id(32), hex_id(16)

        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"stringValue": "rag-api"}}]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "name": "embed",
                                    "startTimeUnixNano": str(START_NS),
                                    "endTimeUnixNano": str(START_NS + 500_000_000),
                                    "attributes": [
                                        {
                                            "key": "gen_ai.operation.name",
                                            "value": {"stringValue": "embeddings"},
                                        },
                                        {
                                            "key": "gen_ai.usage.input_tokens",
                                            "value": {"intValue": "64"},
                                        },
                                    ],
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        response = await client.post(
            OTLP_PATH,
            content=json.dumps(payload),
            headers={"content-type": "application/json", "Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 200, response.text

        root = (await client.get(f"/projects/{name}/traces/{trace_id}")).json()["root"]
        assert root["kind"] == "embedding"
        assert root["prompt_tokens"] == 64
        assert root["duration_ms"] == 500

    async def test_json_request_gets_json_response(self, client: httpx.AsyncClient) -> None:
        _, key = await make_project_with_key(client)
        response = await client.post(
            OTLP_PATH,
            content=json.dumps({"resourceSpans": []}),
            headers={"content-type": "application/json", "Authorization": f"Bearer {key}"},
        )
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {}


class TestOutOfOrderArrival:
    async def test_child_arriving_before_its_parent_still_builds_the_tree(
        self, client: httpx.AsyncClient
    ) -> None:
        """The normal case, not an edge case.

        Spans complete innermost-first, and a batching exporter flushes on a
        timer — so a child routinely reaches the collector in an earlier batch
        than the parent that encloses it.
        """
        name, key = await make_project_with_key(client)
        trace_id = hex_id(32)
        root_id, child_id, grandchild_id = hex_id(16), hex_id(16), hex_id(16)
        headers = {"content-type": PROTOBUF, "Authorization": f"Bearer {key}"}

        # Deepest span first, root last — the reverse of tree order.
        for span in (
            proto_span(
                trace_id=trace_id,
                span_id=grandchild_id,
                parent_span_id=child_id,
                name="embed",
                start_ns=START_NS + 200_000_000,
                duration_ns=100_000_000,
                attributes={"gen_ai.operation.name": "embeddings"},
            ),
            proto_span(
                trace_id=trace_id,
                span_id=child_id,
                parent_span_id=root_id,
                name="retrieve",
                start_ns=START_NS + 100_000_000,
                duration_ns=400_000_000,
                attributes={"db.system": "qdrant"},
            ),
            proto_span(
                trace_id=trace_id,
                span_id=root_id,
                name="answer_question",
                start_ns=START_NS,
                duration_ns=2_000_000_000,
            ),
        ):
            assert (
                await client.post(OTLP_PATH, content=export_request(span), headers=headers)
            ).status_code == 200

        detail = (await client.get(f"/projects/{name}/traces/{trace_id}")).json()

        # The rollup names the trace after the root, even though the root was
        # the last span to arrive.
        assert detail["name"] == "answer_question"
        assert detail["span_count"] == 3

        root = detail["root"]
        assert root["span_id"] == root_id
        assert root["kind"] == "chain"
        [child] = root["children"]
        assert child["span_id"] == child_id
        assert child["kind"] == "retrieval"
        [grandchild] = child["children"]
        assert grandchild["span_id"] == grandchild_id
        assert grandchild["kind"] == "embedding"

    async def test_late_span_updates_the_rollup(self, client: httpx.AsyncClient) -> None:
        """A trace's totals must not be frozen by the first batch that arrives."""
        name, key = await make_project_with_key(client)
        trace_id, root_id, late_id = hex_id(32), hex_id(16), hex_id(16)
        headers = {"content-type": PROTOBUF, "Authorization": f"Bearer {key}"}

        await client.post(
            OTLP_PATH,
            content=export_request(
                proto_span(
                    trace_id=trace_id,
                    span_id=root_id,
                    name="answer",
                    attributes={"gen_ai.usage.input_tokens": 100},
                )
            ),
            headers=headers,
        )
        first = (await client.get(f"/projects/{name}/traces/{trace_id}")).json()
        assert first["total_prompt_tokens"] == 100

        await client.post(
            OTLP_PATH,
            content=export_request(
                proto_span(
                    trace_id=trace_id,
                    span_id=late_id,
                    parent_span_id=root_id,
                    name="second call",
                    attributes={"gen_ai.usage.input_tokens": 50},
                )
            ),
            headers=headers,
        )
        second = (await client.get(f"/projects/{name}/traces/{trace_id}")).json()
        assert second["total_prompt_tokens"] == 150
        assert second["span_count"] == 2

    async def test_error_anywhere_in_the_tree_marks_the_trace_failed(
        self, client: httpx.AsyncClient
    ) -> None:
        name, key = await make_project_with_key(client)
        trace_id, root_id, child_id = hex_id(32), hex_id(16), hex_id(16)
        headers = {"content-type": PROTOBUF, "Authorization": f"Bearer {key}"}

        # The failing child arrives first; the healthy root arrives after.
        await client.post(
            OTLP_PATH,
            content=export_request(
                proto_span(
                    trace_id=trace_id,
                    span_id=child_id,
                    parent_span_id=root_id,
                    name="llm call",
                    status_code=trace_pb2.Status.STATUS_CODE_ERROR,
                )
            ),
            headers=headers,
        )
        await client.post(
            OTLP_PATH,
            content=export_request(proto_span(trace_id=trace_id, span_id=root_id, name="answer")),
            headers=headers,
        )

        detail = (await client.get(f"/projects/{name}/traces/{trace_id}")).json()
        assert detail["status"] == "error"


class TestTenancyAndIdempotency:
    async def test_spans_are_scoped_to_the_keys_project(self, client: httpx.AsyncClient) -> None:
        """The project comes from the key, never from the payload."""
        name_a, key_a = await make_project_with_key(client)
        name_b, _ = await make_project_with_key(client)
        trace_id = hex_id(32)

        await client.post(
            OTLP_PATH,
            content=export_request(proto_span(trace_id=trace_id, span_id=hex_id(16))),
            headers={"content-type": PROTOBUF, "Authorization": f"Bearer {key_a}"},
        )

        assert (await client.get(f"/projects/{name_a}/traces/{trace_id}")).status_code == 200
        assert (await client.get(f"/projects/{name_b}/traces/{trace_id}")).status_code == 404

    async def test_retried_export_is_idempotent(self, client: httpx.AsyncClient) -> None:
        """Exporters retry without knowing whether the first attempt landed."""
        name, key = await make_project_with_key(client)
        trace_id, span_id = hex_id(32), hex_id(16)
        body = export_request(proto_span(trace_id=trace_id, span_id=span_id))
        headers = {"content-type": PROTOBUF, "Authorization": f"Bearer {key}"}

        for _ in range(3):
            assert (await client.post(OTLP_PATH, content=body, headers=headers)).status_code == 200

        detail = (await client.get(f"/projects/{name}/traces/{trace_id}")).json()
        assert detail["span_count"] == 1


class TestCompressedIngest:
    """What a real exporter actually sends.

    The OpenTelemetry Collector's `otlphttp` exporter enables gzip by default,
    and several language SDKs do when `OTEL_EXPORTER_OTLP_COMPRESSION=gzip` is
    set. Nothing in the ASGI stack decompresses, so before this the compressed
    bytes reached the protobuf parser and came back as "malformed payload" —
    failing before any mapping ran, and blaming the wrong thing.
    """

    async def test_gzipped_protobuf_is_ingested(self, client: httpx.AsyncClient) -> None:
        name, key = await make_project_with_key(client)
        trace_id, span_id = hex_id(32), hex_id(16)

        response = await client.post(
            OTLP_PATH,
            content=gzip.compress(
                export_request(
                    proto_span(
                        trace_id=trace_id,
                        span_id=span_id,
                        name="chat",
                        attributes={"gen_ai.usage.input_tokens": 42},
                    )
                )
            ),
            headers={
                "content-type": PROTOBUF,
                "content-encoding": "gzip",
                "Authorization": f"Bearer {key}",
            },
        )
        assert response.status_code == 200, response.text

        root = (await client.get(f"/projects/{name}/traces/{trace_id}")).json()["root"]
        assert root["span_id"] == span_id
        assert root["prompt_tokens"] == 42

    async def test_gzipped_json_is_ingested(self, client: httpx.AsyncClient) -> None:
        name, key = await make_project_with_key(client)
        trace_id, span_id = hex_id(32), hex_id(16)

        payload = {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "name": "chat",
                                    "startTimeUnixNano": str(START_NS),
                                    "endTimeUnixNano": str(START_NS + 1_000_000_000),
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        response = await client.post(
            OTLP_PATH,
            content=gzip.compress(json.dumps(payload).encode()),
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
                "Authorization": f"Bearer {key}",
            },
        )
        assert response.status_code == 200, response.text
        assert (await client.get(f"/projects/{name}/traces/{trace_id}")).status_code == 200

    async def test_unsupported_encoding_is_rejected_clearly(
        self, client: httpx.AsyncClient
    ) -> None:
        """Not silently parsed as if it were uncompressed."""
        _, key = await make_project_with_key(client)
        response = await client.post(
            OTLP_PATH,
            content=b"whatever",
            headers={
                "content-type": PROTOBUF,
                "content-encoding": "br",
                "Authorization": f"Bearer {key}",
            },
        )
        assert response.status_code == 422
        assert "gzip" in response.text

    async def test_uncompressed_still_works(self, client: httpx.AsyncClient) -> None:
        """Compression is optional; absence of the header must change nothing."""
        name, key = await make_project_with_key(client)
        trace_id = hex_id(32)
        response = await client.post(
            OTLP_PATH,
            content=export_request(proto_span(trace_id=trace_id, span_id=hex_id(16))),
            headers={"content-type": PROTOBUF, "Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 200
        assert (await client.get(f"/projects/{name}/traces/{trace_id}")).status_code == 200
