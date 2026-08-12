"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Path
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.db import get_sessionmaker
from lo_core.db.models.api_key import ApiKey
from lo_core.db.models.project import Project
from lo_core.errors import ForbiddenError, UnauthorizedError
from lo_core.services import api_keys as api_key_service
from lo_core.services import projects as project_service


async def db_session() -> AsyncIterator[AsyncSession]:
    """One transaction per request, committed on success.

    Scoping the transaction to the request — rather than committing inside each
    service function — is what makes a multi-step handler atomic. Creating a
    prompt and its first version either both land or neither does, without the
    service layer needing to know it was called as part of a larger operation.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DbSession = Annotated[AsyncSession, Depends(db_session)]


async def resolve_project(
    session: DbSession,
    project_slug: Annotated[str, Path(description="Project slug")],
) -> Project:
    """Resolve the `{project_slug}` path parameter to a Project.

    In Phase 8 this becomes the tenancy enforcement point: the API key presented
    on the request must grant access to this project, and a mismatch will 404
    rather than 403 so the API does not confirm the existence of projects the
    caller cannot see.
    """
    return await project_service.get_project_by_slug(session, project_slug)


CurrentProject = Annotated[Project, Depends(resolve_project)]


# --- API key authentication ----------------------------------------------
#
# `auto_error=False` so a missing header reaches our handler and gets the
# platform's standard error body, rather than FastAPI's default shape. Clients
# branch on `code`, so every error must look the same.
_bearer = HTTPBearer(auto_error=False, description="Project API key")


async def authenticated_key(
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> ApiKey:
    """Resolve `Authorization: Bearer <key>` to a project API key.

    This is the authentication surface that faces the public internet — the SDK
    inside someone else's application is the only caller. Everything it protects
    is scoped to the key's project.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("missing Authorization header")

    key = await api_key_service.verify_api_key(session, credentials.credentials)
    if key is None:
        # One message for every failure mode — unknown, revoked, expired,
        # malformed. Distinguishing them tells an attacker which keys exist.
        raise UnauthorizedError("invalid or revoked API key")

    await api_key_service.touch_last_used(session, key)
    return key


AuthenticatedKey = Annotated[ApiKey, Depends(authenticated_key)]


def require_scope(scope: str) -> Callable[[ApiKey], Awaitable[ApiKey]]:
    """Dependency factory enforcing a scope on the presented key.

    Scopes exist so the key embedded in a customer's application can send traces
    and do nothing else — not read other projects' eval results, not mint further
    keys.
    """

    async def check(key: AuthenticatedKey) -> ApiKey:
        if scope not in key.scopes:
            raise ForbiddenError(f"this API key lacks the {scope!r} scope")
        return key

    return check


IngestKey = Annotated[ApiKey, Depends(require_scope("ingest"))]
