"""Per-request correlation id + access logging."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from lo_core.logging import get_logger

log = get_logger("http")

REQUEST_ID_HEADER = "x-request-id"


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
            duration_ms = (time.perf_counter() - start) * 1000
            log.exception(
                "request.failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration_ms, 2),
            )
            raise

        duration_ms = (time.perf_counter() - start) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id

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
