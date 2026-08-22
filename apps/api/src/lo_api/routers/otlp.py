"""OTLP/HTTP ingest.

The point of this endpoint is that an application already instrumented with
OpenTelemetry — directly, or through OpenLLMetry, Logfire, or the Collector —
can send its traces here by changing configuration and nothing else:

    OTEL_EXPORTER_OTLP_ENDPOINT=https://<host>/otlp
    OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer lo_live_...

### Why `/otlp/v1/traces` and not `/v1/traces`

`/v1/traces` is OTLP's standard path, and it is already taken by this
platform's own SDK with a different JSON body. Serving both on one path means
guessing which schema arrived, and OTLP's own JSON encoding makes that guess
ambiguous rather than merely ugly.

`OTEL_EXPORTER_OTLP_ENDPOINT` is a *base* URL: every OTLP/HTTP exporter appends
`/v1/traces` to it. So mounting the collector-compatible endpoint under `/otlp`
gives exporters exactly the URL they expect, costs the user one config line
they were setting anyway, and leaves the native endpoint untouched. See ADR 0012.

### Both encodings

Protobuf is what the OpenTelemetry SDKs send by default over HTTP; supporting
only JSON would turn "change one setting" into "change one setting and override
the encoding", which is precisely the friction this endpoint exists to remove.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request, Response, status
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

from lo_api.dependencies import DbSession, IngestPrincipal
from lo_api.otlp import decode_json, decode_protobuf, decompress, to_span_ingest
from lo_core.errors import ForbiddenError, RateLimitError, ValidationError
from lo_core.ratelimit import check_rate_limit
from lo_core.schemas.telemetry import MAX_SPANS_PER_BATCH
from lo_core.services import traces as service

router = APIRouter(prefix="/otlp", tags=["otlp"])

PROTOBUF_CONTENT_TYPE = "application/x-protobuf"
JSON_CONTENT_TYPE = "application/json"

# The OTLP spec caps request bodies at the receiver's discretion. This mirrors
# the native endpoint's per-batch limit so both paths refuse the same volume.
MAX_BODY_BYTES = 4 * 1024 * 1024


@router.post(
    "/v1/traces",
    status_code=status.HTTP_200_OK,
    summary="Ingest OpenTelemetry spans (OTLP/HTTP)",
    # The response is an OTLP ExportTraceServiceResponse, not one of our
    # schemas, so the generated OpenAPI body model would be a lie.
    response_model=None,
)
async def export_traces(
    request: Request,
    principal: IngestPrincipal,
    session: DbSession,
) -> Response:
    """Accept an OTLP export request and store its spans.

    200 with an `ExportTraceServiceResponse` body, because that is what the OTLP
    specification requires and what an exporter parses. Spans that cannot be
    mapped are reported in `partialSuccess` rather than failing the whole batch:
    an exporter that gets a 4xx will retry the identical payload forever, so
    rejecting one malformed span out of five hundred would cost the other 499
    on every attempt.
    """
    if principal.key is None:
        raise ForbiddenError("trace ingestion requires a project API key, not the admin token")
    project_id = principal.key.project_id

    body = await request.body()
    # Checked against the wire bytes, before decompression. `decompress` applies
    # its own, separate ceiling to what those bytes expand into.
    if len(body) > MAX_BODY_BYTES:
        raise ValidationError(f"OTLP payload exceeds {MAX_BODY_BYTES} bytes")

    body = decompress(body, request.headers.get("content-encoding", ""))

    # Content-Type may carry parameters (`application/json; charset=utf-8`).
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == PROTOBUF_CONTENT_TYPE:
        otlp_spans = decode_protobuf(body)
    elif content_type == JSON_CONTENT_TYPE:
        otlp_spans = decode_json(body)
    else:
        raise ValidationError(
            f"unsupported Content-Type {content_type!r}; "
            f"expected {PROTOBUF_CONTENT_TYPE} or {JSON_CONTENT_TYPE}"
        )

    if not otlp_spans:
        # An empty export is legal and means "nothing to report". Answering 200
        # with an empty partial success is what the spec asks for.
        return _response(content_type, rejected=0, error_message="")

    if len(otlp_spans) > MAX_SPANS_PER_BATCH:
        raise ValidationError(
            f"OTLP batch of {len(otlp_spans)} spans exceeds the "
            f"{MAX_SPANS_PER_BATCH}-span limit; lower the exporter's batch size"
        )

    # Cost is the span count, matching the native endpoint — a client should not
    # get a larger budget by switching wire formats.
    limit = await check_rate_limit(project_id, cost=len(otlp_spans))
    if not limit.allowed:
        raise RateLimitError(
            f"ingest rate limit of {limit.limit} spans/min exceeded",
            retry_after=limit.retry_after,
        )

    mapped = []
    rejected = 0
    reasons: list[str] = []
    for otlp_span in otlp_spans:
        try:
            mapped.append(to_span_ingest(otlp_span))
        # Broad on purpose: one unmappable span must not sink the batch.
        except Exception as exc:
            rejected += 1
            if len(reasons) < 3:
                reasons.append(f"{otlp_span.span_id or '<no id>'}: {exc}")

    if mapped:
        # The same service the native endpoint uses. Out-of-order spans need no
        # special handling here because `refresh_trace` recomputes a trace's
        # rollup from all of its stored spans rather than patching deltas — a
        # child arriving before its parent is already the normal case.
        await service.ingest_spans(session, project_id, mapped)

    return _response(
        content_type,
        rejected=rejected,
        error_message="; ".join(reasons),
    )


def _response(content_type: str, *, rejected: int, error_message: str) -> Response:
    """Build an `ExportTraceServiceResponse` in the encoding the client used.

    Answering protobuf with JSON (or the reverse) makes an exporter log a parse
    error on every successful export, which looks exactly like data loss to
    whoever is watching their logs.
    """
    if content_type == PROTOBUF_CONTENT_TYPE:
        message = trace_service_pb2.ExportTraceServiceResponse()
        if rejected:
            message.partial_success.rejected_spans = rejected
            message.partial_success.error_message = error_message
        return Response(content=message.SerializeToString(), media_type=PROTOBUF_CONTENT_TYPE)

    payload: dict[str, object] = {}
    if rejected:
        payload["partialSuccess"] = {
            "rejectedSpans": rejected,
            "errorMessage": error_message,
        }
    return Response(
        content=json.dumps(payload),
        media_type=JSON_CONTENT_TYPE,
    )
