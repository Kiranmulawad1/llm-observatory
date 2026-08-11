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
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("LO_ENVIRONMENT", "local")
os.environ.setdefault("LO_API_KEY_PEPPER", "test-pepper")

# LO_DATABASE_URL and LO_REDIS_URL are deliberately *not* defaulted here.
#
# They used to be, and the hardcoded value drifted away from .env the first time
# a developer moved Postgres off port 5432 to avoid a clash. The tests then
# connected to whatever unrelated database happened to occupy the old port —
# failing on authentication if they were lucky, and silently writing to the wrong
# database if they were not.
#
# The DSN now has exactly one source per context: .env locally (read by
# pydantic-settings), and explicit workflow env vars in CI. A missing value
# fails fast at Settings validation with a readable error, which is strictly
# better than connecting somewhere unintended.


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


# --- Database-backed fixtures --------------------------------------------
#
# Every test that touches Postgres runs inside a transaction that is rolled back
# on teardown, so tests neither see each other's rows nor need a truncate step.
#
# The mechanism is worth understanding: an outer transaction is opened on a
# connection, and the session is bound to that *connection* with
# `join_transaction_mode="create_savepoint"`. A `commit()` inside application
# code then releases a SAVEPOINT rather than committing the outer transaction —
# so the code under test exercises its real commit path, and the rollback here
# still undoes everything. Truncating tables between tests would be slower and
# would not survive tests running in parallel.
#
# These require a migrated database: `make up && make migrate`.


@pytest.fixture
async def db_connection() -> AsyncIterator[AsyncConnection]:
    from lo_core.config import get_settings

    # NullPool: the engine is per-test, so pooled connections would just be
    # opened and discarded, and a lingering pool can outlive the event loop.
    engine = create_async_engine(str(get_settings().database_url), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                yield connection
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(
        bind=db_connection,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    async with maker() as db_session:
        yield db_session


@pytest.fixture
async def client(db_connection: AsyncConnection) -> AsyncIterator[httpx.AsyncClient]:
    """API client whose requests run inside the test's rolled-back transaction.

    The `db_session` dependency is overridden rather than the engine, so routing,
    validation, serialisation and the exception handlers are all the real ones —
    only the transaction boundary is swapped.
    """
    from lo_api.dependencies import db_session as db_session_dependency
    from lo_api.main import create_app

    maker = async_sessionmaker(
        bind=db_connection,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )

    async def _override() -> AsyncIterator[AsyncSession]:
        async with maker() as request_session:
            try:
                yield request_session
                await request_session.commit()
            except Exception:
                await request_session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[db_session_dependency] = _override

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
