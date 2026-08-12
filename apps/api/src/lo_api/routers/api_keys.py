"""API key management."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, status

from lo_api.dependencies import CurrentProject, DbSession
from lo_core.schemas.telemetry import ApiKeyCreate, ApiKeyCreated, ApiKeyRead
from lo_core.services import api_keys as service

router = APIRouter(prefix="/projects/{project_slug}/api-keys", tags=["api keys"])


@router.post(
    "",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Issue an API key",
)
async def create_api_key(
    payload: ApiKeyCreate,
    project: CurrentProject,
    session: DbSession,
) -> ApiKeyCreated:
    """Issue a key. **The plaintext is returned once and never again.**

    Only a peppered hash is stored, so there is no endpoint that can show it
    later and no database dump that yields a working credential. If it is lost,
    the answer is to revoke and reissue.
    """
    key, plaintext = await service.create_api_key(
        session,
        project.id,
        name=payload.name,
        scopes=payload.scopes,
        expires_at=payload.expires_at,
        description=payload.description,
    )
    return ApiKeyCreated(**ApiKeyRead.model_validate(key).model_dump(), key=plaintext)


@router.get("", response_model=list[ApiKeyRead], summary="List API keys")
async def list_api_keys(project: CurrentProject, session: DbSession) -> list[ApiKeyRead]:
    """List keys. Returns metadata and the clear prefix — never the key itself."""
    keys = await service.list_api_keys(session, project.id)
    return [ApiKeyRead.model_validate(k) for k in keys]


@router.delete(
    "/{key_id}",
    response_model=ApiKeyRead,
    summary="Revoke an API key",
)
async def revoke_api_key(
    key_id: Annotated[uuid.UUID, Path()],
    project: CurrentProject,
    session: DbSession,
) -> ApiKeyRead:
    """Revoke by stamping `revoked_at`, rather than deleting the row.

    Deleting would leave traces referencing a credential nobody can account for.
    Revoking keeps the audit trail and records when it stopped working.
    """
    key = await service.revoke_api_key(session, project.id, key_id)
    return ApiKeyRead.model_validate(key)
