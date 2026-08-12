"""Dataset endpoints, including file upload."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Path, Query, UploadFile, status

from lo_api.dependencies import CurrentProject, DbSession
from lo_core.schemas.evaluation import (
    DatasetCreate,
    DatasetItemRead,
    DatasetRead,
    DatasetVersionCreate,
    DatasetVersionRead,
)
from lo_core.services import datasets as service

router = APIRouter(prefix="/projects/{project_slug}/datasets", tags=["datasets"])

DatasetSlug = Annotated[str, Path(description="Dataset slug, unique within the project")]


@router.post(
    "", response_model=DatasetRead, status_code=status.HTTP_201_CREATED, summary="Create a dataset"
)
async def create_dataset(
    payload: DatasetCreate, project: CurrentProject, session: DbSession
) -> DatasetRead:
    dataset = await service.create_dataset(session, project.id, payload)
    return DatasetRead.model_validate(dataset)


@router.get("", response_model=list[DatasetRead], summary="List datasets")
async def list_datasets(
    project: CurrentProject,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DatasetRead]:
    return await service.list_dataset_reads(session, project.id, limit=limit, offset=offset)


@router.get("/{dataset_slug}", response_model=DatasetRead, summary="Get a dataset")
async def get_dataset(
    dataset_slug: DatasetSlug, project: CurrentProject, session: DbSession
) -> DatasetRead:
    dataset = await service.get_dataset(session, project.id, dataset_slug)
    reads = await service.list_dataset_reads(session, project.id, limit=1000)
    return next(r for r in reads if r.id == dataset.id)


@router.delete(
    "/{dataset_slug}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a dataset"
)
async def delete_dataset(
    dataset_slug: DatasetSlug, project: CurrentProject, session: DbSession
) -> None:
    dataset = await service.get_dataset(session, project.id, dataset_slug)
    await service.delete_dataset(session, dataset)


@router.post(
    "/{dataset_slug}/versions",
    response_model=DatasetVersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Append a dataset version from JSON",
)
async def create_version(
    dataset_slug: DatasetSlug,
    payload: DatasetVersionCreate,
    project: CurrentProject,
    session: DbSession,
) -> DatasetVersionRead:
    dataset = await service.get_dataset(session, project.id, dataset_slug)
    version = await service.create_version(session, dataset, payload)
    return DatasetVersionRead.model_validate(version)


@router.post(
    "/{dataset_slug}/versions/upload",
    response_model=DatasetVersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Append a dataset version from a JSON/JSONL/CSV file",
)
async def upload_version(
    dataset_slug: DatasetSlug,
    project: CurrentProject,
    session: DbSession,
    file: Annotated[UploadFile, File(description="JSON array, JSONL, or CSV with a header row")],
    created_by: Annotated[str | None, Form()] = None,
    change_note: Annotated[str | None, Form()] = None,
) -> DatasetVersionRead:
    """Upload a dataset file.

    A separate endpoint from the JSON one rather than a content-type branch:
    multipart and JSON have different request models, and splitting them keeps
    both signatures honest in the OpenAPI schema instead of documenting a body
    that is sometimes one shape and sometimes another.
    """
    raw = await file.read()
    items = service.parse_upload(raw, file.filename)

    dataset = await service.get_dataset(session, project.id, dataset_slug)
    version = await service.create_version(
        session,
        dataset,
        DatasetVersionCreate(items=items, created_by=created_by, change_note=change_note),
    )
    return DatasetVersionRead.model_validate(version)


@router.get(
    "/{dataset_slug}/versions",
    response_model=list[DatasetVersionRead],
    summary="List dataset versions, newest first",
)
async def list_versions(
    dataset_slug: DatasetSlug,
    project: CurrentProject,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DatasetVersionRead]:
    dataset = await service.get_dataset(session, project.id, dataset_slug)
    versions = await service.list_versions(session, dataset.id, limit=limit, offset=offset)
    return [DatasetVersionRead.model_validate(v) for v in versions]


@router.get(
    "/{dataset_slug}/versions/{version}/items",
    response_model=list[DatasetItemRead],
    summary="List the items in a dataset version",
)
async def list_items(
    dataset_slug: DatasetSlug,
    version: int,
    project: CurrentProject,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DatasetItemRead]:
    dataset = await service.get_dataset(session, project.id, dataset_slug)
    dataset_version = await service.get_version(session, dataset.id, version)
    items = await service.list_items(session, dataset_version.id, limit=limit, offset=offset)
    return [DatasetItemRead.model_validate(i) for i in items]
