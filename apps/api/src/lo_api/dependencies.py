"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.db import get_sessionmaker
from lo_core.db.models.project import Project
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
