"""Authentication and authorisation across the API.

The property worth testing is not "auth exists" but **auth cannot be skipped**.
Every project-scoped endpoint resolves its project through one dependency, and
that dependency authenticates — so the coverage test below walks the live
OpenAPI schema rather than a hand-maintained list, and a new endpoint added
tomorrow is covered without anyone remembering to add it here.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

ADMIN = os.environ["LO_ADMIN_TOKEN"]
NO_AUTH: dict[str, str] = {"Authorization": ""}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def make_project(client: httpx.AsyncClient) -> str:
    name = slug("proj")
    response = await client.post("/projects", json={"slug": name, "name": "Auth test"})
    assert response.status_code == 201, response.text
    return name


async def make_key(client: httpx.AsyncClient, project: str, scopes: list[str]) -> str:
    response = await client.post(
        f"/projects/{project}/api-keys", json={"name": "test", "scopes": scopes}
    )
    assert response.status_code == 201, response.text
    return response.json()["key"]


class TestUnauthenticated:
    async def test_every_project_endpoint_rejects_a_missing_credential(
        self, client: httpx.AsyncClient
    ) -> None:
        """Walks the OpenAPI schema, so a new endpoint is covered automatically.

        This is the test that makes "auth cannot be skipped" a fact rather than
        a claim — it fails the moment someone adds a project route that does not
        go through the authenticating dependency.
        """
        from lo_api.main import create_app

        spec = create_app().openapi()
        checked = 0

        for path, operations in spec["paths"].items():
            if "{project_slug}" not in path:
                continue
            # Substitute placeholders; auth is checked before the value matters.
            url = path.replace("{project_slug}", "anything")
            for placeholder in (
                "{prompt_slug}",
                "{dataset_slug}",
                "{trace_id}",
                "{ref}",
                "{label}",
                "{version}",
                "{item_id}",
                "{run_id}",
                "{key_id}",
                "{rule_id}",
            ):
                url = url.replace(
                    placeholder, "x" if "id" not in placeholder else str(uuid.uuid4())
                )

            for method in operations:
                if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    continue
                response = await client.request(method.upper(), url, json={}, headers=NO_AUTH)
                assert response.status_code == 401, (
                    f"{method.upper()} {path} returned {response.status_code}, not 401 — "
                    "it is not going through the authenticating dependency"
                )
                checked += 1

        # Guard against the loop silently matching nothing.
        assert checked > 25, f"only {checked} endpoints checked"

    async def test_project_creation_rejects_a_missing_credential(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post("/projects", json={"slug": "x", "name": "x"}, headers=NO_AUTH)
        assert response.status_code == 401

    async def test_garbage_token_is_401(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/projects", headers=bearer("not-a-real-token"))
        assert response.status_code == 401

    async def test_health_probes_stay_open(self, client: httpx.AsyncClient) -> None:
        """A liveness probe that needs a credential fails during a credential
        outage — which is exactly when Kubernetes should not restart the fleet."""
        assert (await client.get("/healthz", headers=NO_AUTH)).status_code == 200
        assert (await client.get("/readyz", headers=NO_AUTH)).status_code in (200, 503)


class TestAdminOnly:
    async def test_a_project_key_cannot_create_projects(self, client: httpx.AsyncClient) -> None:
        """Otherwise a tenant could mint themselves unlimited tenancy."""
        project = await make_project(client)
        key = await make_key(client, project, ["read", "write", "admin"])

        response = await client.post(
            "/projects", json={"slug": slug("sneaky"), "name": "x"}, headers=bearer(key)
        )
        assert response.status_code == 403
        assert response.json()["code"] == "forbidden"

    async def test_a_project_key_cannot_list_all_projects(self, client: httpx.AsyncClient) -> None:
        project = await make_project(client)
        key = await make_key(client, project, ["read", "write", "admin"])

        assert (await client.get("/projects", headers=bearer(key))).status_code == 403

    async def test_dead_letters_are_operator_only(self, client: httpx.AsyncClient) -> None:
        project = await make_project(client)
        key = await make_key(client, project, ["read"])

        assert (await client.get("/dead-letters", headers=bearer(key))).status_code == 403
        assert (await client.get("/dead-letters")).status_code == 200


class TestTenancyIsolation:
    async def test_a_key_cannot_reach_another_project(self, client: httpx.AsyncClient) -> None:
        """404, not 403.

        A 403 would confirm the project exists, letting someone enumerate other
        tenants by guessing slugs.
        """
        mine = await make_project(client)
        theirs = await make_project(client)
        my_key = await make_key(client, mine, ["read", "write"])

        assert (
            await client.get(f"/projects/{mine}/prompts", headers=bearer(my_key))
        ).status_code == 200

        response = await client.get(f"/projects/{theirs}/prompts", headers=bearer(my_key))
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    async def test_a_key_cannot_write_to_another_project(self, client: httpx.AsyncClient) -> None:
        mine = await make_project(client)
        theirs = await make_project(client)
        my_key = await make_key(client, mine, ["read", "write"])

        response = await client.post(
            f"/projects/{theirs}/prompts",
            json={"slug": "x", "name": "X"},
            headers=bearer(my_key),
        )
        assert response.status_code == 404

    async def test_the_admin_token_reaches_every_project(self, client: httpx.AsyncClient) -> None:
        a, b = await make_project(client), await make_project(client)
        assert (await client.get(f"/projects/{a}/prompts")).status_code == 200
        assert (await client.get(f"/projects/{b}/prompts")).status_code == 200


class TestScopes:
    async def test_read_scope_cannot_write(self, client: httpx.AsyncClient) -> None:
        """The scope requirement is derived from the HTTP method, in one place."""
        project = await make_project(client)
        key = await make_key(client, project, ["read"])

        assert (
            await client.get(f"/projects/{project}/prompts", headers=bearer(key))
        ).status_code == 200

        response = await client.post(
            f"/projects/{project}/prompts",
            json={"slug": "x", "name": "X"},
            headers=bearer(key),
        )
        assert response.status_code == 403
        assert "write" in response.json()["detail"]

    async def test_write_scope_cannot_read(self, client: httpx.AsyncClient) -> None:
        """Scopes are flat apart from one documented implication.

        `write` does not imply `read`. Only `admin` implies both, because a
        project administrator that cannot read its own project is nonsense.
        """
        project = await make_project(client)
        key = await make_key(client, project, ["write"])

        assert (
            await client.get(f"/projects/{project}/prompts", headers=bearer(key))
        ).status_code == 403

    async def test_ingest_only_key_cannot_read_the_project(self, client: httpx.AsyncClient) -> None:
        """The property that matters for the SDK: a key embedded in a customer's
        application can write spans and nothing else."""
        project = await make_project(client)
        key = await make_key(client, project, ["ingest"])

        assert (
            await client.get(f"/projects/{project}/traces", headers=bearer(key))
        ).status_code == 403
        assert (
            await client.get(f"/projects/{project}/prompts", headers=bearer(key))
        ).status_code == 403
        assert (
            await client.get(f"/projects/{project}/eval/runs", headers=bearer(key))
        ).status_code == 403

    async def test_a_write_key_cannot_manage_api_keys(self, client: httpx.AsyncClient) -> None:
        """Key management needs `admin`, so a compromised write key cannot mint
        itself a longer-lived credential."""
        project = await make_project(client)
        key = await make_key(client, project, ["read", "write"])

        response = await client.post(
            f"/projects/{project}/api-keys",
            json={"name": "escalation", "scopes": ["admin"]},
            headers=bearer(key),
        )
        assert response.status_code == 403

    async def test_admin_scope_implies_read_and_write(self, client: httpx.AsyncClient) -> None:
        """The one implication in the model, and the reason it exists."""
        project = await make_project(client)
        key = await make_key(client, project, ["admin"])

        assert (
            await client.get(f"/projects/{project}/prompts", headers=bearer(key))
        ).status_code == 200
        assert (
            await client.post(
                f"/projects/{project}/prompts",
                json={"slug": "made-by-admin", "name": "X"},
                headers=bearer(key),
            )
        ).status_code == 201

    async def test_an_admin_scoped_key_can_manage_its_own_project_keys(
        self, client: httpx.AsyncClient
    ) -> None:
        project = await make_project(client)
        key = await make_key(client, project, ["admin"])

        response = await client.post(
            f"/projects/{project}/api-keys",
            json={"name": "second", "scopes": ["ingest"]},
            headers=bearer(key),
        )
        assert response.status_code == 201

    async def test_ingest_requires_a_project_key_not_the_admin_token(
        self, client: httpx.AsyncClient
    ) -> None:
        """The operator token has no project of its own, so it cannot say which
        project a span belongs to."""
        payload: dict[str, Any] = {
            "spans": [
                {
                    "trace_id": uuid.uuid4().hex,
                    "span_id": uuid.uuid4().hex[:16],
                    "name": "op",
                    "started_at": "2026-08-19T00:00:00Z",
                }
            ]
        }
        response = await client.post("/v1/traces", json=payload)
        assert response.status_code == 403
        assert "project API key" in response.json()["detail"]


class TestRevocation:
    async def test_a_revoked_key_stops_working_everywhere(self, client: httpx.AsyncClient) -> None:
        project = await make_project(client)
        key = await make_key(client, project, ["read", "write"])

        listed = (await client.get(f"/projects/{project}/api-keys")).json()
        key_id = next(k["id"] for k in listed if k["name"] == "test")
        await client.delete(f"/projects/{project}/api-keys/{key_id}")

        assert (
            await client.get(f"/projects/{project}/prompts", headers=bearer(key))
        ).status_code == 401
