"""HTTP tests for the dataset and eval endpoints.

`enqueue` is stubbed throughout. These tests run inside a rolled-back
transaction, so a really-enqueued job would be picked up by whatever worker is
attached to the same Redis and would fail looking for a run it cannot see — the
job would then dead-letter and pollute an unrelated table. Stubbing keeps the
test to what it is actually about: the HTTP contract.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_enqueue(function: str, *args: Any) -> str:
        return f"job-{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr("lo_api.routers.evaluation.enqueue", fake_enqueue)


def slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def make_project(client: httpx.AsyncClient) -> str:
    name = slug("proj")
    response = await client.post("/projects", json={"slug": name, "name": "Test"})
    assert response.status_code == 201, response.text
    return name


async def make_dataset(
    client: httpx.AsyncClient, project: str, items: list[dict[str, Any]] | None = None
) -> str:
    name = slug("ds")
    assert (
        await client.post(
            f"/projects/{project}/datasets", json={"slug": name, "name": "Test dataset"}
        )
    ).status_code == 201

    payload = items or [{"inputs": {"question": "q1"}, "expected_output": "a1"}]
    response = await client.post(
        f"/projects/{project}/datasets/{name}/versions", json={"items": payload}
    )
    assert response.status_code == 201, response.text
    return name


async def make_prompt(client: httpx.AsyncClient, project: str) -> str:
    name = slug("pr")
    assert (
        await client.post(
            f"/projects/{project}/prompts", json={"slug": name, "name": "Test prompt"}
        )
    ).status_code == 201
    response = await client.post(
        f"/projects/{project}/prompts/{name}/versions",
        json={
            "messages": [{"role": "user", "content": "{{ question }}"}],
            "parameters": {"model": "fake-model"},
        },
    )
    assert response.status_code == 201, response.text
    return name


class TestEvaluatorDiscovery:
    async def test_lists_evaluators_with_config_schemas(self, client: httpx.AsyncClient) -> None:
        """The UI builds config forms from this instead of hardcoding a copy."""
        body = (await client.get("/evaluators")).json()
        by_type = {e["type"]: e for e in body}
        assert {"exact_match", "regex_match", "json_schema", "embedding_similarity"} <= set(by_type)
        assert "pattern" in by_type["regex_match"]["config_schema"]["properties"]


class TestDatasets:
    async def test_create_and_version(self, client: httpx.AsyncClient) -> None:
        project = await make_project(client)
        dataset = await make_dataset(client, project)

        body = (await client.get(f"/projects/{project}/datasets/{dataset}")).json()
        assert body["latest_version"] == 1

    async def test_versions_are_immutable_and_increment(self, client: httpx.AsyncClient) -> None:
        project = await make_project(client)
        dataset = await make_dataset(client, project)
        second = await client.post(
            f"/projects/{project}/datasets/{dataset}/versions",
            json={"items": [{"inputs": {"question": "q2"}}]},
        )
        assert second.json()["version"] == 2

        versions = (await client.get(f"/projects/{project}/datasets/{dataset}/versions")).json()
        assert [v["version"] for v in versions] == [2, 1]

    async def test_content_hash_detects_unchanged_upload(self, client: httpx.AsyncClient) -> None:
        """Lets a CI job skip creating a version when the file did not change."""
        project = await make_project(client)
        dataset = await make_dataset(client, project)
        again = await client.post(
            f"/projects/{project}/datasets/{dataset}/versions",
            json={"items": [{"inputs": {"question": "q1"}, "expected_output": "a1"}]},
        )
        first = (await client.get(f"/projects/{project}/datasets/{dataset}/versions")).json()[-1]
        assert again.json()["content_hash"] == first["content_hash"]

    async def test_csv_upload(self, client: httpx.AsyncClient) -> None:
        project = await make_project(client)
        name = slug("ds")
        await client.post(f"/projects/{project}/datasets", json={"slug": name, "name": "D"})

        response = await client.post(
            f"/projects/{project}/datasets/{name}/versions/upload",
            files={"file": ("data.csv", b"question,answer\nq1,a1\nq2,a2\n", "text/csv")},
        )
        assert response.status_code == 201, response.text
        assert response.json()["item_count"] == 2

        items = (await client.get(f"/projects/{project}/datasets/{name}/versions/1/items")).json()
        assert items[0]["inputs"] == {"question": "q1"}
        assert items[0]["expected_output"] == "a1"

    async def test_malformed_upload_returns_422(self, client: httpx.AsyncClient) -> None:
        project = await make_project(client)
        name = slug("ds")
        await client.post(f"/projects/{project}/datasets", json={"slug": name, "name": "D"})

        response = await client.post(
            f"/projects/{project}/datasets/{name}/versions/upload",
            files={"file": ("data.json", b"[{broken}]", "application/json")},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_request"

    async def test_empty_item_list_rejected(self, client: httpx.AsyncClient) -> None:
        project = await make_project(client)
        name = slug("ds")
        await client.post(f"/projects/{project}/datasets", json={"slug": name, "name": "D"})
        response = await client.post(
            f"/projects/{project}/datasets/{name}/versions", json={"items": []}
        )
        assert response.status_code == 422

    async def test_datasets_scoped_to_project(self, client: httpx.AsyncClient) -> None:
        mine, theirs = await make_project(client), await make_project(client)
        await make_dataset(client, mine)
        await make_dataset(client, theirs)
        assert len((await client.get(f"/projects/{mine}/datasets")).json()) == 1


class TestEvalRuns:
    async def test_create_returns_202_and_pending_run(self, client: httpx.AsyncClient) -> None:
        """202, not 201: accepted for execution, not completed."""
        project = await make_project(client)
        dataset = await make_dataset(client, project)
        prompt = await make_prompt(client, project)

        response = await client.post(
            f"/projects/{project}/eval/runs",
            json={
                "dataset": dataset,
                "prompt": prompt,
                "prompt_version": "1",
                "evaluators": [{"type": "exact_match"}],
            },
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "pending"
        assert body["total_items"] == 1
        assert body["job_id"] is not None

    async def test_bad_evaluator_config_rejected_before_enqueue(
        self, client: httpx.AsyncClient
    ) -> None:
        project = await make_project(client)
        dataset = await make_dataset(client, project)
        prompt = await make_prompt(client, project)

        response = await client.post(
            f"/projects/{project}/eval/runs",
            json={
                "dataset": dataset,
                "prompt": prompt,
                "prompt_version": "1",
                "evaluators": [{"type": "regex_match", "config": {"pattern": "(unclosed"}}],
            },
        )
        assert response.status_code == 422
        assert "invalid config" in response.json()["detail"]

    async def test_unknown_dataset_returns_404(self, client: httpx.AsyncClient) -> None:
        project = await make_project(client)
        prompt = await make_prompt(client, project)
        response = await client.post(
            f"/projects/{project}/eval/runs",
            json={
                "dataset": "no-such-dataset",
                "prompt": prompt,
                "evaluators": [{"type": "exact_match"}],
            },
        )
        assert response.status_code == 404

    async def test_run_without_prompt_needs_explicit_model(self, client: httpx.AsyncClient) -> None:
        project = await make_project(client)
        dataset = await make_dataset(client, project)
        response = await client.post(
            f"/projects/{project}/eval/runs",
            json={"dataset": dataset, "evaluators": [{"type": "exact_match"}]},
        )
        assert response.status_code == 422
        assert "no model specified" in response.json()["detail"]

    async def test_list_and_fetch_run(self, client: httpx.AsyncClient) -> None:
        project = await make_project(client)
        dataset = await make_dataset(client, project)
        prompt = await make_prompt(client, project)

        created = (
            await client.post(
                f"/projects/{project}/eval/runs",
                json={
                    "dataset": dataset,
                    "prompt": prompt,
                    "prompt_version": "1",
                    "evaluators": [{"type": "exact_match"}],
                },
            )
        ).json()

        listed = (await client.get(f"/projects/{project}/eval/runs")).json()
        assert [r["id"] for r in listed] == [created["id"]]

        detail = (await client.get(f"/projects/{project}/eval/runs/{created['id']}")).json()
        assert detail["id"] == created["id"]
        # Nothing has executed yet, so there are no per-example results.
        assert detail["results"] == []

    async def test_cancel_pending_run(self, client: httpx.AsyncClient) -> None:
        project = await make_project(client)
        dataset = await make_dataset(client, project)
        prompt = await make_prompt(client, project)
        created = (
            await client.post(
                f"/projects/{project}/eval/runs",
                json={
                    "dataset": dataset,
                    "prompt": prompt,
                    "prompt_version": "1",
                    "evaluators": [{"type": "exact_match"}],
                },
            )
        ).json()

        cancelled = await client.post(f"/projects/{project}/eval/runs/{created['id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        # Cancelling twice is a conflict, not a silent no-op.
        again = await client.post(f"/projects/{project}/eval/runs/{created['id']}/cancel")
        assert again.status_code == 409

    async def test_run_from_another_project_is_404(self, client: httpx.AsyncClient) -> None:
        """Knowing a run id must not be enough to read it."""
        owner = await make_project(client)
        stranger = await make_project(client)
        dataset = await make_dataset(client, owner)
        prompt = await make_prompt(client, owner)
        created = (
            await client.post(
                f"/projects/{owner}/eval/runs",
                json={
                    "dataset": dataset,
                    "prompt": prompt,
                    "prompt_version": "1",
                    "evaluators": [{"type": "exact_match"}],
                },
            )
        ).json()

        response = await client.get(f"/projects/{stranger}/eval/runs/{created['id']}")
        assert response.status_code == 404


class TestDeadLetters:
    async def test_endpoint_returns_a_list(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/dead-letters")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
