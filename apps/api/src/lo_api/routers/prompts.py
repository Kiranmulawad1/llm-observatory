"""Prompt registry endpoints.

Routes are nested under `/projects/{project_slug}` because every prompt belongs
to exactly one project. Making tenancy part of the URL rather than a query
parameter means a handler cannot accidentally omit it — the dependency that
resolves the project is the same one that will enforce the API key in Phase 8.

Version references (`{ref}`) accept either a number or a label, so `.../7` and
`.../production` are both valid. That is what lets an application pin to a moving
label while CI pins to an exact version, against the same endpoint.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from lo_api.dependencies import CurrentProject, DbSession
from lo_core.schemas.prompt import (
    Label,
    LabelAssign,
    PromptCreate,
    PromptDiff,
    PromptLabelRead,
    PromptRead,
    PromptUpdate,
    PromptVersionCreate,
    PromptVersionRead,
    RenderRequest,
    RenderResponse,
)
from lo_core.services import prompts as service

router = APIRouter(prefix="/projects/{project_slug}/prompts", tags=["prompts"])

PromptSlug = Annotated[str, Path(description="Prompt slug, unique within the project")]
VersionRef = Annotated[
    str, Path(description="Version number (e.g. `7`) or label (e.g. `production`)")
]


# --- Prompts --------------------------------------------------------------


@router.post(
    "",
    response_model=PromptRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a prompt",
)
async def create_prompt(
    payload: PromptCreate,
    project: CurrentProject,
    session: DbSession,
) -> PromptRead:
    prompt = await service.create_prompt(session, project.id, payload)
    return await service.build_prompt_read(session, prompt)


@router.get("", response_model=list[PromptRead], summary="List prompts")
async def list_prompts(
    project: CurrentProject,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PromptRead]:
    return await service.list_prompt_reads(session, project.id, limit=limit, offset=offset)


@router.get("/{prompt_slug}", response_model=PromptRead, summary="Get a prompt")
async def get_prompt(
    prompt_slug: PromptSlug,
    project: CurrentProject,
    session: DbSession,
) -> PromptRead:
    prompt = await service.get_prompt(session, project.id, prompt_slug)
    return await service.build_prompt_read(session, prompt)


@router.patch("/{prompt_slug}", response_model=PromptRead, summary="Update prompt metadata")
async def update_prompt(
    prompt_slug: PromptSlug,
    payload: PromptUpdate,
    project: CurrentProject,
    session: DbSession,
) -> PromptRead:
    prompt = await service.get_prompt(session, project.id, prompt_slug)
    await service.update_prompt(session, prompt, payload)
    return await service.build_prompt_read(session, prompt)


@router.delete(
    "/{prompt_slug}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a prompt and all its versions",
)
async def delete_prompt(
    prompt_slug: PromptSlug,
    project: CurrentProject,
    session: DbSession,
) -> None:
    prompt = await service.get_prompt(session, project.id, prompt_slug)
    await service.delete_prompt(session, prompt)


# --- Versions -------------------------------------------------------------


@router.post(
    "/{prompt_slug}/versions",
    response_model=PromptVersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Append a new immutable version",
)
async def create_version(
    prompt_slug: PromptSlug,
    payload: PromptVersionCreate,
    project: CurrentProject,
    session: DbSession,
) -> PromptVersionRead:
    prompt = await service.get_prompt(session, project.id, prompt_slug)
    version = await service.create_version(session, prompt, payload)
    return PromptVersionRead.model_validate(version)


@router.get(
    "/{prompt_slug}/versions",
    response_model=list[PromptVersionRead],
    summary="List versions, newest first",
)
async def list_versions(
    prompt_slug: PromptSlug,
    project: CurrentProject,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PromptVersionRead]:
    prompt = await service.get_prompt(session, project.id, prompt_slug)
    versions = await service.list_versions(session, prompt.id, limit=limit, offset=offset)
    return [PromptVersionRead.model_validate(v) for v in versions]


@router.get(
    "/{prompt_slug}/versions/{ref}",
    response_model=PromptVersionRead,
    summary="Get one version by number or label",
)
async def get_version(
    prompt_slug: PromptSlug,
    ref: VersionRef,
    project: CurrentProject,
    session: DbSession,
) -> PromptVersionRead:
    prompt = await service.get_prompt(session, project.id, prompt_slug)
    version = await service.resolve_version(session, prompt.id, ref)
    return PromptVersionRead.model_validate(version)


@router.post(
    "/{prompt_slug}/versions/{ref}/render",
    response_model=RenderResponse,
    summary="Render a version against supplied variables",
)
async def render_version(
    prompt_slug: PromptSlug,
    ref: VersionRef,
    payload: RenderRequest,
    project: CurrentProject,
    session: DbSession,
) -> RenderResponse:
    """Render server-side.

    Useful on its own for previewing in the UI, but the real reason it exists is
    that the eval runner and the SDK must render a prompt exactly the way the
    registry does. One rendering implementation, reachable over HTTP, is what
    guarantees the prompt evaluated in CI is byte-identical to the one served in
    production.
    """
    prompt = await service.get_prompt(session, project.id, prompt_slug)
    return await service.render_version(session, prompt.id, ref, payload.variables)


# --- Diff -----------------------------------------------------------------


@router.get(
    "/{prompt_slug}/diff",
    response_model=PromptDiff,
    summary="Diff two versions",
)
async def diff_versions(
    prompt_slug: PromptSlug,
    project: CurrentProject,
    session: DbSession,
    from_ref: Annotated[str, Query(alias="from", description="Version number or label")],
    to_ref: Annotated[str, Query(alias="to", description="Version number or label")],
) -> PromptDiff:
    prompt = await service.get_prompt(session, project.id, prompt_slug)
    return await service.diff_prompt_versions(session, prompt.id, from_ref, to_ref)


# --- Labels ---------------------------------------------------------------


@router.get(
    "/{prompt_slug}/labels",
    response_model=list[PromptLabelRead],
    summary="List labels and the versions they point at",
)
async def list_labels(
    prompt_slug: PromptSlug,
    project: CurrentProject,
    session: DbSession,
) -> list[PromptLabelRead]:
    prompt = await service.get_prompt(session, project.id, prompt_slug)
    return await service.list_labels(session, prompt.id)


@router.put(
    "/{prompt_slug}/labels/{label}",
    response_model=PromptLabelRead,
    summary="Point a label at a version (promotion)",
)
async def assign_label(
    prompt_slug: PromptSlug,
    label: Annotated[Label, Path(description="e.g. production, staging, experimental")],
    payload: LabelAssign,
    project: CurrentProject,
    session: DbSession,
) -> PromptLabelRead:
    """PUT, not POST: assigning a label is idempotent.

    Sending the same promotion twice must leave the registry in the same state,
    which matters because this is the call a deploy pipeline makes — and pipelines
    get retried.
    """
    prompt = await service.get_prompt(session, project.id, prompt_slug)
    assigned = await service.assign_label(session, prompt, label, payload)
    version = await service.get_version(session, prompt.id, payload.version)
    return PromptLabelRead(
        label=assigned.label,
        version_id=assigned.version_id,
        version=version.version,
        updated_by=assigned.updated_by,
        updated_at=assigned.updated_at,
    )


@router.delete(
    "/{prompt_slug}/labels/{label}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a label",
)
async def remove_label(
    prompt_slug: PromptSlug,
    label: Annotated[Label, Path()],
    project: CurrentProject,
    session: DbSession,
) -> None:
    prompt = await service.get_prompt(session, project.id, prompt_slug)
    await service.remove_label(session, prompt.id, label)
