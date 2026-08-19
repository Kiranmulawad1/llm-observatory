"""Project CRUD.

Kept minimal on purpose. Phase 8 extends this with API-key issuance and
rotation; the resource itself exists now so prompts have something to belong to.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from lo_api.dependencies import AdminPrincipal, CurrentProject, DbSession
from lo_core.schemas.prompt import ProjectCreate, ProjectRead
from lo_core.services import projects as service

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post(
    "",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
)
async def create_project(
    payload: ProjectCreate, session: DbSession, _: AdminPrincipal
) -> ProjectRead:
    project = await service.create_project(session, payload)
    return ProjectRead.model_validate(project)


@router.get("", response_model=list[ProjectRead], summary="List projects")
async def list_projects(session: DbSession, _: AdminPrincipal) -> list[ProjectRead]:
    return [ProjectRead.model_validate(p) for p in await service.list_projects(session)]


@router.get("/{project_slug}", response_model=ProjectRead, summary="Get a project")
async def get_project(project: CurrentProject) -> ProjectRead:
    return ProjectRead.model_validate(project)
