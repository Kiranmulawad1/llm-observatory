"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from lo_api.errors import register_exception_handlers
from lo_api.middleware.request_context import RequestContextMiddleware
from lo_api.queue import close_pool
from lo_api.routers import (
    api_keys,
    datasets,
    evaluation,
    health,
    projects,
    prompts,
    traces,
)
from lo_core.config import get_settings
from lo_core.db import dispose_engine
from lo_core.logging import configure_logging, get_logger
from lo_core.ratelimit import close_client as close_rate_limiter

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()
    # Crash-loop loudly on an unsafe config rather than serving with dev defaults.
    settings.assert_production_safe()
    log.info("api.startup", environment=settings.environment)
    yield
    # Drain both pools so a terminating pod releases its Postgres and Redis
    # connections rather than leaving the servers to time them out.
    await close_pool()
    await close_rate_limiter()
    await dispose_engine()
    log.info("api.shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="llm-observatory",
        version="0.1.0",
        summary="Evaluation and observability platform for LLM applications",
        lifespan=lifespan,
        # Interactive docs are a local-dev convenience, not a production surface.
        docs_url="/docs" if settings.is_local else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.is_local else None,
    )

    app.add_middleware(RequestContextMiddleware)

    # The browser never talks to this API directly — the Next.js BFF does, so the
    # platform API key stays server-side. CORS is therefore only opened for the
    # local dev server.
    if settings.is_local:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:3000"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(projects.router)
    app.include_router(prompts.router)
    app.include_router(datasets.router)
    app.include_router(evaluation.router)
    app.include_router(api_keys.router)
    app.include_router(traces.router)

    return app


app = create_app()
