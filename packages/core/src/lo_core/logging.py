"""Structured logging.

JSON to stdout in deployed environments, so the container runtime ships logs
without a sidecar parser and fields stay queryable. Human-readable colour in
local dev, because nobody wants to read JSON in a terminal.

`request_id` / `trace_id` are bound into a contextvar by the API middleware and
appear on every log line emitted while handling that request, without being
threaded through function signatures.
"""

from __future__ import annotations

import logging
import sys

import structlog

from lo_core.config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.typing.Processor = (
        structlog.dev.ConsoleRenderer()
        if settings.is_local
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib loggers (uvicorn, sqlalchemy, arq) through the same pipeline
    # so the output is uniformly parseable.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
