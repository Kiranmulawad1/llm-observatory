"""Per-request correlation id + access logging."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from lo_core import metrics
from lo_core.logging import get_logger

log = get_logger("http")

REQUEST_ID_HEADER = "x-request-id"

# Paths recorded without a matching route. A request that matches nothing has no
# template, and recording its raw path would let anyone mint unbounded label
# values in our own monitoring by spraying 404s at random URLs.
UNMATCHED = "<unmatched>"


def _route_template(request: Request) -> str:
    """The route's path template, not the resolved URL.

    `/projects/{project_slug}/traces/{trace_id}` rather than
    `/projects/acme/traces/4bf92f...`. The resolved path would create a new
    Prometheus series per project and per trace id — the cardinality trap that
    `lo_core.metrics` exists to avoid, arriving through the back door.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else UNMATCHED


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds a request id into the structlog contextvars for the whole request.

    Honours an inbound `x-request-id` so a trace can be correlated across the
    Next.js BFF, this API, and the worker job it enqueues — rather than each hop
    inventing its own id and breaking the chain.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = time.perf_counter() - start
            # An unhandled exception never reaches the response path below, so
            # without this the metric that matters most — requests that blew up
            # — would be the one metric with no data.
            metrics.http_requests.labels(
                method=request.method, path=_route_template(request), status="500"
            ).inc()
            log.exception(
                "request.failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration * 1000, 2),
            )
            raise

        duration = time.perf_counter() - start
        duration_ms = duration * 1000
        response.headers[REQUEST_ID_HEADER] = request_id

        template = _route_template(request)
        metrics.http_requests.labels(
            method=request.method, path=template, status=str(response.status_code)
        ).inc()
        metrics.http_duration.labels(method=request.method, path=template).observe(duration)

        # Health probes fire every few seconds; logging them buries real traffic.
        if request.url.path not in ("/healthz", "/readyz"):
            log.info(
                "request.completed",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round(duration_ms, 2),
            )
        return response
