"""Translation of domain errors into HTTP responses.

Registered once as exception handlers rather than wrapped in try/except in every
route. Handlers stay free of error-mapping boilerplate, and — more importantly —
a new service function cannot forget to translate `NotFoundError`, which is the
usual way a domain error escapes as an opaque 500.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from lo_core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    UnauthorizedError,
    ValidationError,
)
from lo_core.logging import get_logger

log = get_logger(__name__)


def _problem(status_code: int, code: str, detail: str) -> JSONResponse:
    """A consistent error body across every endpoint.

    Shaped after RFC 9457 (`application/problem+json`): a machine-readable `code`
    the SDK can branch on, plus human `detail`. Clients that switch on prose
    error messages break the moment the wording is improved.
    """
    return JSONResponse(status_code=status_code, content={"code": code, "detail": detail})


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return _problem(status.HTTP_404_NOT_FOUND, "not_found", str(exc))

    @app.exception_handler(ConflictError)
    async def _conflict(request: Request, exc: ConflictError) -> JSONResponse:
        return _problem(status.HTTP_409_CONFLICT, "conflict", str(exc))

    @app.exception_handler(UnauthorizedError)
    async def _unauthorized(request: Request, exc: UnauthorizedError) -> JSONResponse:
        response = _problem(status.HTTP_401_UNAUTHORIZED, "unauthorized", str(exc))
        # RFC 9110 requires this header on a 401. Without it a client cannot
        # tell which scheme to authenticate with.
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @app.exception_handler(ForbiddenError)
    async def _forbidden(request: Request, exc: ForbiddenError) -> JSONResponse:
        return _problem(status.HTTP_403_FORBIDDEN, "forbidden", str(exc))

    @app.exception_handler(RateLimitError)
    async def _rate_limited(request: Request, exc: RateLimitError) -> JSONResponse:
        response = _problem(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited", str(exc))
        # Tells the SDK exactly how long to back off, instead of leaving it to
        # guess — and a guessing client under load is how a rate limit turns
        # into a thundering herd.
        response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: ValidationError) -> JSONResponse:
        # Covers TemplateSyntaxError and TemplateRenderError, which subclass it.
        # 422 rather than 400: the request was well-formed JSON that failed a
        # semantic check.
        return _problem(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_request", str(exc))
