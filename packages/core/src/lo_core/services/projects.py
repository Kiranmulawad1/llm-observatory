"""Project service."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.db.models.project import Project
from lo_core.errors import ConflictError, NotFoundError
from lo_core.schemas.prompt import ProjectCreate


async def create_project(session: AsyncSession, payload: ProjectCreate) -> Project:
    project = Project(
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
    )
    session.add(project)
    try:
        # Flush rather than commit: the caller's transaction scope decides when
        # to commit, but we need the INSERT to reach Postgres now so a duplicate
        # slug surfaces here as a catchable ConflictError instead of exploding
        # later at commit time, outside any handler that knows what it meant.
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"project slug {payload.slug!r} already exists") from exc
    return project


async def get_project_by_slug(session: AsyncSession, slug: str) -> Project:
    result = await session.execute(select(Project).where(Project.slug == slug))
    project = result.scalar_one_or_none()
    if project is None:
        raise NotFoundError(f"project {slug!r} not found")
    return project


async def get_project(session: AsyncSession, project_id: uuid.UUID) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"project {project_id} not found")
    return project


async def list_projects(session: AsyncSession) -> list[Project]:
    result = await session.execute(select(Project).order_by(Project.slug))
    return list(result.scalars().all())
