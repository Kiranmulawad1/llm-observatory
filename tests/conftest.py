"""Shared test fixtures.

Environment is set before any application module is imported, so `Settings`
validates against known values rather than whatever happens to be in the
developer's shell or the CI runner's environment.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

os.environ.setdefault("LO_ENVIRONMENT", "local")
os.environ.setdefault(
    "LO_DATABASE_URL", "postgresql+asyncpg://lo:lo_dev_password@localhost:5432/llm_observatory"
)
os.environ.setdefault("LO_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("LO_API_KEY_PEPPER", "test-pepper")


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """`get_settings` is lru_cached; clear it so a test that monkeypatches the
    environment does not leak configuration into the next test."""
    from lo_core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def api_client() -> AsyncIterator[httpx.AsyncClient]:
    """In-process API client. No socket, no running server.

    ASGITransport drives the app directly, so unit tests exercise real routing,
    dependency injection and serialisation without the flakiness of binding a port.
    Note that it does not run the lifespan hook, which is what keeps these tests
    free of a database connection.
    """
    from lo_api.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
