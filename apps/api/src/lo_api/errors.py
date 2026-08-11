"""Translation of domain errors into HTTP responses.

Registered once as exception handlers rather than wrapped in try/except in every
route. Handlers stay free of error-mapping boilerplate, and — more importantly —
a new service function cannot forget to translate `NotFoundError`, which is the
usual way a domain error escapes as an opaque 500.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from lo_core.errors import ConflictError, NotFoundError, ValidationError
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

    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: ValidationError) -> JSONResponse:
        # Covers TemplateSyntaxError and TemplateRenderError, which subclass it.
        # 422 rather than 400: the request was well-formed JSON that failed a
        # semantic check.
        return _problem(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_request", str(exc))
