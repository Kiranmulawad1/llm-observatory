"""End-to-end HTTP tests for the registry.

These drive the real app — routing, validation, serialisation and the domain
error handlers — with only the transaction boundary swapped for a rolled-back
one. That is what makes them worth having alongside the service-level tests:
they are the only place the error-code mapping and the request schemas are
actually exercised.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration


def unique_slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def create_project(client: httpx.AsyncClient) -> str:
    slug = unique_slug("proj")
    response = await client.post("/projects", json={"slug": slug, "name": "Test project"})
    assert response.status_code == 201, response.text
    return slug


async def create_prompt(client: httpx.AsyncClient, project: str) -> str:
    slug = unique_slug("prompt")
    response = await client.post(
        f"/projects/{project}/prompts", json={"slug": slug, "name": "Test prompt"}
    )
    assert response.status_code == 201, response.text
    return slug


async def add_version(
    client: httpx.AsyncClient,
    project: str,
    prompt: str,
    content: str,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.post(
        f"/projects/{project}/prompts/{prompt}/versions",
        json={
            "messages": [{"role": "user", "content": content}],
            "parameters": parameters or {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestProjects:
    async def test_create_and_fetch(self, client: httpx.AsyncClient) -> None:
        slug = await create_project(client)
        response = await client.get(f"/projects/{slug}")
        assert response.status_code == 200
        assert response.json()["slug"] == slug

    async def test_duplicate_slug_returns_409(self, client: httpx.AsyncClient) -> None:
        slug = await create_project(client)
        response = await client.post("/projects", json={"slug": slug, "name": "Again"})
        assert response.status_code == 409
        assert response.json()["code"] == "conflict"

    async def test_unknown_project_returns_404(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/projects/does-not-exist")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    async def test_invalid_slug_rejected_by_schema(self, client: httpx.AsyncClient) -> None:
        """Uppercase and spaces are not URL-safe; the pattern rejects them before
        they reach the database check constraint."""
        response = await client.post("/projects", json={"slug": "Not A Slug", "name": "x"})
        assert response.status_code == 422


class TestPromptCrud:
    async def test_new_prompt_has_no_versions(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)

        body = (await client.get(f"/projects/{project}/prompts/{prompt}")).json()
        assert body["latest_version"] is None
        assert body["labels"] == []

    async def test_patch_updates_metadata_only(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)

        response = await client.patch(
            f"/projects/{project}/prompts/{prompt}", json={"name": "Renamed"}
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

    async def test_list_is_scoped_to_project(self, client: httpx.AsyncClient) -> None:
        mine = await create_project(client)
        theirs = await create_project(client)
        await create_prompt(client, mine)
        await create_prompt(client, theirs)

        body = (await client.get(f"/projects/{mine}/prompts")).json()
        assert len(body) == 1

    async def test_delete_removes_prompt(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        await add_version(client, project, prompt, "hi")

        assert (await client.delete(f"/projects/{project}/prompts/{prompt}")).status_code == 204
        assert (await client.get(f"/projects/{project}/prompts/{prompt}")).status_code == 404

    async def test_duplicate_prompt_slug_returns_409(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        response = await client.post(
            f"/projects/{project}/prompts", json={"slug": prompt, "name": "Again"}
        )
        assert response.status_code == 409


class TestVersions:
    async def test_versions_increment_and_list_newest_first(
        self, client: httpx.AsyncClient
    ) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        await add_version(client, project, prompt, "v1")
        await add_version(client, project, prompt, "v2")

        body = (await client.get(f"/projects/{project}/prompts/{prompt}/versions")).json()
        assert [v["version"] for v in body] == [2, 1]

    async def test_declared_variables_are_returned(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        created = await add_version(client, project, prompt, "{{ question }} / {{ context }}")

        assert [v["name"] for v in created["variables"]] == ["context", "question"]

    async def test_broken_template_returns_422(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)

        response = await client.post(
            f"/projects/{project}/prompts/{prompt}/versions",
            json={"messages": [{"role": "user", "content": "{% for x in y %}oops"}]},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_request"

    async def test_empty_message_list_rejected(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        response = await client.post(
            f"/projects/{project}/prompts/{prompt}/versions", json={"messages": []}
        )
        assert response.status_code == 422

    async def test_unknown_version_returns_404(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        response = await client.get(f"/projects/{project}/prompts/{prompt}/versions/42")
        assert response.status_code == 404


class TestLabels:
    async def test_promotion_and_lookup_by_label(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        await add_version(client, project, prompt, "v1")
        await add_version(client, project, prompt, "v2")

        response = await client.put(
            f"/projects/{project}/prompts/{prompt}/labels/production", json={"version": 2}
        )
        assert response.status_code == 200
        assert response.json()["version"] == 2

        by_label = await client.get(f"/projects/{project}/prompts/{prompt}/versions/production")
        assert by_label.json()["version"] == 2

    async def test_promotion_is_idempotent(self, client: httpx.AsyncClient) -> None:
        """A retried deploy must not fail."""
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        await add_version(client, project, prompt, "v1")

        url = f"/projects/{project}/prompts/{prompt}/labels/production"
        first = await client.put(url, json={"version": 1})
        second = await client.put(url, json={"version": 1})
        assert first.status_code == second.status_code == 200
        assert first.json()["version_id"] == second.json()["version_id"]

    async def test_label_appears_on_prompt_read(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        await add_version(client, project, prompt, "v1")
        await client.put(
            f"/projects/{project}/prompts/{prompt}/labels/production", json={"version": 1}
        )

        body = (await client.get(f"/projects/{project}/prompts/{prompt}")).json()
        assert body["labels"][0]["label"] == "production"
        assert body["latest_version"] == 1

    async def test_delete_label(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        await add_version(client, project, prompt, "v1")
        url = f"/projects/{project}/prompts/{prompt}/labels/production"
        await client.put(url, json={"version": 1})

        assert (await client.delete(url)).status_code == 204
        assert (await client.get(f"/projects/{project}/prompts/{prompt}/labels")).json() == []

    async def test_labelling_missing_version_returns_404(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        response = await client.put(
            f"/projects/{project}/prompts/{prompt}/labels/production", json={"version": 5}
        )
        assert response.status_code == 404


class TestRender:
    async def test_renders_with_supplied_variables(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        await add_version(client, project, prompt, "Hello {{ name }}")

        response = await client.post(
            f"/projects/{project}/prompts/{prompt}/versions/1/render",
            json={"variables": {"name": "Kiran"}},
        )
        assert response.status_code == 200
        assert response.json()["messages"][0]["content"] == "Hello Kiran"

    async def test_missing_variable_returns_422(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        await add_version(client, project, prompt, "Hello {{ name }}")

        response = await client.post(
            f"/projects/{project}/prompts/{prompt}/versions/1/render", json={"variables": {}}
        )
        assert response.status_code == 422

    async def test_render_by_label(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        await add_version(client, project, prompt, "v1 {{ x }}")
        await client.put(
            f"/projects/{project}/prompts/{prompt}/labels/production", json={"version": 1}
        )

        response = await client.post(
            f"/projects/{project}/prompts/{prompt}/versions/production/render",
            json={"variables": {"x": "ok"}},
        )
        assert response.json()["messages"][0]["content"] == "v1 ok"


class TestDiffEndpoint:
    async def test_diff_between_numbers(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        await add_version(client, project, prompt, "Be terse.")
        await add_version(client, project, prompt, "Be verbose.")

        response = await client.get(
            f"/projects/{project}/prompts/{prompt}/diff", params={"from": "1", "to": "2"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["identical"] is False
        assert "-Be terse." in body["messages"][0]["unified"]

    async def test_diff_production_against_candidate(self, client: httpx.AsyncClient) -> None:
        """The question this endpoint exists to answer: what am I about to ship?"""
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        await add_version(client, project, prompt, "v1")
        await add_version(client, project, prompt, "v2")
        await client.put(
            f"/projects/{project}/prompts/{prompt}/labels/production", json={"version": 1}
        )

        response = await client.get(
            f"/projects/{project}/prompts/{prompt}/diff",
            params={"from": "production", "to": "2"},
        )
        assert response.json()["from_version"] == 1

    async def test_parameter_only_change_is_detected(self, client: httpx.AsyncClient) -> None:
        project = await create_project(client)
        prompt = await create_prompt(client, project)
        await add_version(client, project, prompt, "same", {"temperature": 0.0})
        await add_version(client, project, prompt, "same", {"temperature": 0.9})

        body = (
            await client.get(
                f"/projects/{project}/prompts/{prompt}/diff", params={"from": "1", "to": "2"}
            )
        ).json()
        assert body["identical"] is False
        assert all(m["change"] == "unchanged" for m in body["messages"])
        changed = [p for p in body["parameters"] if p["change"] == "modified"]
        assert changed[0]["key"] == "temperature"
