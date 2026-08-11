"""Async engine and session factory.

One engine per process, created lazily and disposed on shutdown. The API and the
worker both import this module — that shared connection handling is the whole
reason `core` exists as a package and the main reason the queue is async-native
(see docs/adr/0002-task-queue.md).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lo_core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            str(settings.database_url),
            echo=settings.database_echo,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            # Recycle below any proxy/load-balancer idle timeout. Cloud SQL's
            # connection pooler drops idle connections and SQLAlchemy will
            # otherwise hand out a dead one.
            pool_recycle=1800,
            pool_pre_ping=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,  # let handlers read attributes after commit
            autoflush=False,
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope. Commits on success, rolls back on any exception.

    Used directly by worker tasks. The API gets the same behaviour through the
    `db_session` FastAPI dependency.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close the pool. Called from the API/worker shutdown hook so pods drain
    connections cleanly instead of leaving Postgres to time them out."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
