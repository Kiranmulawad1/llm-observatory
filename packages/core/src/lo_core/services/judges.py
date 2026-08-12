"""Judge rubric seeding and resolution.

Rubrics ship as content but live as data: `seed_builtin_rubrics` writes each
built-in rubric into a project's prompt registry as a `kind="judge"` prompt at
version 1, labelled `production`.

After seeding the platform has no special relationship with them. A team edits a
rubric through the ordinary prompt endpoints, which appends version 2 and leaves
version 1 intact for every run that referenced it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.db.models.prompt import Prompt, PromptVersion
from lo_core.errors import NotFoundError, ValidationError
from lo_core.evaluators.rubrics import BUILTIN_RUBRICS
from lo_core.schemas.prompt import (
    LabelAssign,
    Message,
    PromptCreate,
    PromptVersionCreate,
)
from lo_core.services import prompts as prompt_service


async def seed_builtin_rubrics(session: AsyncSession, project_id: uuid.UUID) -> list[Prompt]:
    """Install the built-in rubrics into a project, skipping any already present.

    Idempotent by slug rather than by content: a project that already has
    `judge-faithfulness` is left completely alone, including any edits its team
    has made. Re-seeding on deploy must never overwrite someone's rubric — that
    would silently change the meaning of every subsequent score.
    """
    existing = set(
        (
            await session.execute(
                select(Prompt.slug).where(Prompt.project_id == project_id, Prompt.kind == "judge")
            )
        )
        .scalars()
        .all()
    )

    created: list[Prompt] = []
    for rubric in BUILTIN_RUBRICS:
        if rubric.slug in existing:
            continue

        prompt = await prompt_service.create_prompt(
            session,
            project_id,
            PromptCreate(
                slug=rubric.slug,
                name=rubric.name,
                description=rubric.description,
                kind="judge",
            ),
        )
        version = await prompt_service.create_version(
            session,
            prompt,
            PromptVersionCreate(
                messages=[
                    Message(role="system", content=rubric.system),
                    Message(role="user", content=rubric.user),
                ],
                created_by="platform",
                change_note="Built-in rubric, seeded by the platform.",
            ),
        )
        # Labelled so run configs can reference the rubric without pinning a
        # number, and a team promoting an edited v2 changes what runs use
        # without touching a single run config.
        await prompt_service.assign_label(
            session,
            prompt,
            "production",
            LabelAssign(version=version.version, updated_by="platform"),
        )
        created.append(prompt)

    return created


async def resolve_rubric(
    session: AsyncSession,
    project_id: uuid.UUID,
    slug: str,
    ref: str | None,
) -> PromptVersion:
    """Resolve a judge rubric slug (+ optional version/label) to a version.

    Rejects a non-judge prompt explicitly. Rendering an application prompt as a
    rubric would "work" — it is a template with variables — and produce
    confidently meaningless scores, which is worse than an error.
    """
    result = await session.execute(
        select(Prompt).where(Prompt.project_id == project_id, Prompt.slug == slug)
    )
    prompt = result.scalar_one_or_none()
    if prompt is None:
        raise NotFoundError(
            f"judge rubric {slug!r} not found; seed the built-ins with "
            f"POST /projects/{{project}}/judges/seed"
        )
    if prompt.kind != "judge":
        raise ValidationError(f"prompt {slug!r} is an application prompt, not a judge rubric")

    return await prompt_service.resolve_version(session, prompt.id, ref or "production")


async def list_judges(session: AsyncSession, project_id: uuid.UUID) -> list[Prompt]:
    result = await session.execute(
        select(Prompt)
        .where(Prompt.project_id == project_id, Prompt.kind == "judge")
        .order_by(Prompt.slug)
    )
    return list(result.scalars().all())
