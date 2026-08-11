"""Registry guarantees that only a real database can verify.

Marked `integration` because they need a migrated Postgres:
    make up && make migrate
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.errors import ConflictError, NotFoundError
from lo_core.schemas.prompt import (
    LabelAssign,
    Message,
    ProjectCreate,
    PromptCreate,
    PromptVersionCreate,
)
from lo_core.services import projects as project_service
from lo_core.services import prompts as service

pytestmark = pytest.mark.integration


def unique_slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def make_project(session: AsyncSession) -> uuid.UUID:
    project = await project_service.create_project(
        session, ProjectCreate(slug=unique_slug("proj"), name="Test project")
    )
    return project.id


async def make_prompt(session: AsyncSession, project_id: uuid.UUID) -> object:
    return await service.create_prompt(
        session, project_id, PromptCreate(slug=unique_slug("p"), name="Test prompt")
    )


def version_payload(content: str = "{{ question }}", **kwargs: object) -> PromptVersionCreate:
    return PromptVersionCreate(
        messages=[Message(role="user", content=content)],
        **kwargs,  # type: ignore[arg-type]
    )


class TestVersioning:
    async def test_versions_start_at_one_and_increment(self, session: AsyncSession) -> None:
        prompt = await make_prompt(session, await make_project(session))

        numbers = [
            (await service.create_version(session, prompt, version_payload(f"v{i}"))).version
            for i in range(3)
        ]
        assert numbers == [1, 2, 3]

    async def test_version_numbers_are_per_prompt(self, session: AsyncSession) -> None:
        """Two prompts each start at 1; numbering is not global."""
        project_id = await make_project(session)
        first = await make_prompt(session, project_id)
        second = await make_prompt(session, project_id)

        a = await service.create_version(session, first, version_payload())
        b = await service.create_version(session, second, version_payload())
        assert a.version == b.version == 1

    async def test_variables_are_extracted_and_persisted(self, session: AsyncSession) -> None:
        prompt = await make_prompt(session, await make_project(session))
        version = await service.create_version(
            session,
            prompt,
            PromptVersionCreate(
                messages=[
                    Message(role="system", content="You are {{ persona }}."),
                    Message(role="user", content="{% for d in docs %}{{ d }}{% endfor %}{{ q }}"),
                ]
            ),
        )
        assert [v["name"] for v in version.variables] == ["docs", "persona", "q"]

    async def test_invalid_template_is_rejected_at_write_time(self, session: AsyncSession) -> None:
        """A broken template must fail when registered, not when rendered later."""
        from lo_core.errors import TemplateSyntaxError

        prompt = await make_prompt(session, await make_project(session))
        with pytest.raises(TemplateSyntaxError):
            await service.create_version(
                session, prompt, version_payload("{% for x in y %}unclosed")
            )

    async def test_identical_content_yields_identical_hash(self, session: AsyncSession) -> None:
        """Reverting to earlier content is allowed and creates a real new version,
        but the hash lets a caller detect that nothing actually changed."""
        prompt = await make_prompt(session, await make_project(session))
        first = await service.create_version(session, prompt, version_payload("same"))
        await service.create_version(session, prompt, version_payload("different"))
        third = await service.create_version(session, prompt, version_payload("same"))

        assert first.content_hash == third.content_hash
        assert third.version == 3

    async def test_parameters_are_stored_with_the_version(self, session: AsyncSession) -> None:
        from lo_core.schemas.prompt import ModelParameters

        prompt = await make_prompt(session, await make_project(session))
        version = await service.create_version(
            session,
            prompt,
            PromptVersionCreate(
                messages=[Message(role="user", content="hi")],
                parameters=ModelParameters(model="claude-opus-5", temperature=0.2),
            ),
        )
        assert version.parameters["model"] == "claude-opus-5"
        assert version.parameters["temperature"] == 0.2

    async def test_provenance_is_recorded(self, session: AsyncSession) -> None:
        prompt = await make_prompt(session, await make_project(session))
        version = await service.create_version(
            session,
            prompt,
            version_payload(commit_sha="a" * 40, created_by="kiran", change_note="tightened tone"),
        )
        assert version.commit_sha == "a" * 40
        assert version.change_note == "tightened tone"


class TestSlugUniqueness:
    async def test_duplicate_slug_in_same_project_conflicts(self, session: AsyncSession) -> None:
        project_id = await make_project(session)
        slug = unique_slug("dup")
        await service.create_prompt(session, project_id, PromptCreate(slug=slug, name="First"))
        with pytest.raises(ConflictError):
            await service.create_prompt(session, project_id, PromptCreate(slug=slug, name="Second"))

    async def test_same_slug_allowed_in_different_projects(self, session: AsyncSession) -> None:
        """Two teams must be able to own a prompt called "summarize" independently."""
        slug = unique_slug("shared")
        first_project = await make_project(session)
        second_project = await make_project(session)

        await service.create_prompt(session, first_project, PromptCreate(slug=slug, name="A"))
        await service.create_prompt(session, second_project, PromptCreate(slug=slug, name="B"))

    async def test_lookup_is_scoped_to_project(self, session: AsyncSession) -> None:
        """Knowing a slug in another project must not be enough to read it."""
        slug = unique_slug("scoped")
        owner = await make_project(session)
        stranger = await make_project(session)
        await service.create_prompt(session, owner, PromptCreate(slug=slug, name="Owned"))

        with pytest.raises(NotFoundError):
            await service.get_prompt(session, stranger, slug)


class TestLabels:
    async def test_promotion_moves_the_pointer(self, session: AsyncSession) -> None:
        prompt = await make_prompt(session, await make_project(session))
        await service.create_version(session, prompt, version_payload("v1"))
        await service.create_version(session, prompt, version_payload("v2"))

        await service.assign_label(session, prompt, "production", LabelAssign(version=1))
        assert (await service.resolve_version(session, prompt.id, "production")).version == 1

        await service.assign_label(session, prompt, "production", LabelAssign(version=2))
        assert (await service.resolve_version(session, prompt.id, "production")).version == 2

    async def test_only_one_version_per_label(self, session: AsyncSession) -> None:
        """The uniqueness that makes "which prompt is in production?" answerable."""
        prompt = await make_prompt(session, await make_project(session))
        await service.create_version(session, prompt, version_payload("v1"))
        await service.create_version(session, prompt, version_payload("v2"))

        await service.assign_label(session, prompt, "production", LabelAssign(version=1))
        await service.assign_label(session, prompt, "production", LabelAssign(version=2))

        labels = await service.list_labels(session, prompt.id)
        assert [(x.label, x.version) for x in labels] == [("production", 2)]

    async def test_reassigning_same_version_is_idempotent(self, session: AsyncSession) -> None:
        """Deploy pipelines retry; the second call must be a no-op, not an error."""
        prompt = await make_prompt(session, await make_project(session))
        await service.create_version(session, prompt, version_payload())

        first = await service.assign_label(session, prompt, "production", LabelAssign(version=1))
        second = await service.assign_label(session, prompt, "production", LabelAssign(version=1))
        assert first.version_id == second.version_id

    async def test_multiple_labels_can_coexist(self, session: AsyncSession) -> None:
        prompt = await make_prompt(session, await make_project(session))
        await service.create_version(session, prompt, version_payload("v1"))
        await service.create_version(session, prompt, version_payload("v2"))

        await service.assign_label(session, prompt, "production", LabelAssign(version=1))
        await service.assign_label(session, prompt, "staging", LabelAssign(version=2))

        labels = {x.label: x.version for x in await service.list_labels(session, prompt.id)}
        assert labels == {"production": 1, "staging": 2}

    async def test_labelling_a_missing_version_fails(self, session: AsyncSession) -> None:
        prompt = await make_prompt(session, await make_project(session))
        with pytest.raises(NotFoundError):
            await service.assign_label(session, prompt, "production", LabelAssign(version=99))

    async def test_remove_label(self, session: AsyncSession) -> None:
        prompt = await make_prompt(session, await make_project(session))
        await service.create_version(session, prompt, version_payload())
        await service.assign_label(session, prompt, "production", LabelAssign(version=1))

        await service.remove_label(session, prompt.id, "production")
        assert await service.list_labels(session, prompt.id) == []


class TestResolveVersion:
    async def test_resolves_numeric_reference(self, session: AsyncSession) -> None:
        prompt = await make_prompt(session, await make_project(session))
        await service.create_version(session, prompt, version_payload("v1"))
        await service.create_version(session, prompt, version_payload("v2"))

        assert (await service.resolve_version(session, prompt.id, "2")).version == 2

    async def test_unknown_label_raises(self, session: AsyncSession) -> None:
        prompt = await make_prompt(session, await make_project(session))
        with pytest.raises(NotFoundError):
            await service.resolve_version(session, prompt.id, "production")


class TestRenderAndDiff:
    async def test_render_uses_stored_template(self, session: AsyncSession) -> None:
        prompt = await make_prompt(session, await make_project(session))
        await service.create_version(session, prompt, version_payload("Hello {{ name }}"))

        result = await service.render_version(session, prompt.id, "1", {"name": "Kiran"})
        assert result.messages[0].content == "Hello Kiran"

    async def test_diff_by_label_and_number(self, session: AsyncSession) -> None:
        prompt = await make_prompt(session, await make_project(session))
        await service.create_version(session, prompt, version_payload("Be terse."))
        await service.create_version(session, prompt, version_payload("Be verbose."))
        await service.assign_label(session, prompt, "production", LabelAssign(version=1))

        diff = await service.diff_prompt_versions(session, prompt.id, "production", "2")
        assert (diff.from_version, diff.to_version) == (1, 2)
        assert diff.identical is False
        assert diff.messages[0].change == "modified"


class TestListPromptReads:
    async def test_includes_latest_version_and_labels(self, session: AsyncSession) -> None:
        project_id = await make_project(session)
        prompt = await make_prompt(session, project_id)
        await service.create_version(session, prompt, version_payload("v1"))
        await service.create_version(session, prompt, version_payload("v2"))
        await service.assign_label(session, prompt, "production", LabelAssign(version=1))

        reads = await service.list_prompt_reads(session, project_id)
        assert len(reads) == 1
        assert reads[0].latest_version == 2
        assert [(x.label, x.version) for x in reads[0].labels] == [("production", 1)]

    async def test_prompt_without_versions_reports_none(self, session: AsyncSession) -> None:
        project_id = await make_project(session)
        await make_prompt(session, project_id)

        reads = await service.list_prompt_reads(session, project_id)
        assert reads[0].latest_version is None
        assert reads[0].labels == []

    async def test_only_returns_this_projects_prompts(self, session: AsyncSession) -> None:
        mine = await make_project(session)
        theirs = await make_project(session)
        await make_prompt(session, mine)
        await make_prompt(session, theirs)

        assert len(await service.list_prompt_reads(session, mine)) == 1
