"""API key auth, trace ingestion, and span-tree assembly."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass Redis. Rate limiting has its own tests; these are about ingest."""
    from lo_core.ratelimit import RateLimitResult

    async def allow(*args: Any, **kwargs: Any) -> RateLimitResult:
        return RateLimitResult(allowed=True, limit=6000, remaining=6000, retry_after=0)

    monkeypatch.setattr("lo_api.routers.traces.check_rate_limit", allow)


def slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def hex_id(length: int) -> str:
    return uuid.uuid4().hex[:length].ljust(length, "0")


async def make_project_with_key(client: httpx.AsyncClient) -> tuple[str, str]:
    """Create a project and issue an ingest key. Returns (slug, plaintext key)."""
    name = slug("proj")
    assert (await client.post("/projects", json={"slug": name, "name": "Test"})).status_code == 201

    response = await client.post(
        f"/projects/{name}/api-keys", json={"name": "sdk", "scopes": ["ingest"]}
    )
    assert response.status_code == 201, response.text
    return name, response.json()["key"]


def span_payload(
    trace_id: str,
    span_id: str,
    parent_span_id: str | None = None,
    name: str = "op",
    kind: str = "other",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "name": name,
        "kind": kind,
        "started_at": datetime.now(UTC).isoformat(),
        "ended_at": (datetime.now(UTC) + timedelta(milliseconds=50)).isoformat(),
        "duration_ms": 50,
        **extra,
    }


class TestApiKeys:
    async def test_plaintext_is_returned_once_and_never_again(
        self, client: httpx.AsyncClient
    ) -> None:
        """The security property: no endpoint can show the key later."""
        project, key = await make_project_with_key(client)
        assert key.startswith("lo_live_")

        listed = (await client.get(f"/projects/{project}/api-keys")).json()
        assert "key" not in listed[0]
        # Only the clear prefix, which is useless on its own.
        assert listed[0]["key_prefix"] == key[:16]

    async def test_valid_key_authenticates(self, client: httpx.AsyncClient) -> None:
        _, key = await make_project_with_key(client)
        trace_id, span_id = hex_id(32), hex_id(16)

        response = await client.post(
            "/v1/traces",
            json={"spans": [span_payload(trace_id, span_id)]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 202, response.text

    async def test_missing_header_is_401_with_www_authenticate(
        self, client: httpx.AsyncClient
    ) -> None:
        # The fixture authenticates as the operator by default, so "no
        # credential" has to be stated explicitly — an empty Authorization
        # header is what an unauthenticated client actually sends.
        response = await client.post(
            "/v1/traces",
            json={"spans": [span_payload(hex_id(32), hex_id(16))]},
            headers={"Authorization": ""},
        )
        assert response.status_code == 401
        assert response.json()["code"] == "unauthorized"
        assert response.headers["WWW-Authenticate"] == "Bearer"

    async def test_garbage_key_is_401(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/v1/traces",
            json={"spans": [span_payload(hex_id(32), hex_id(16))]},
            headers={"Authorization": "Bearer lo_live_not-a-real-key"},
        )
        assert response.status_code == 401

    async def test_revoked_key_stops_working(self, client: httpx.AsyncClient) -> None:
        project, key = await make_project_with_key(client)
        listed = (await client.get(f"/projects/{project}/api-keys")).json()

        revoked = await client.delete(f"/projects/{project}/api-keys/{listed[0]['id']}")
        assert revoked.status_code == 200
        # Revoked, not deleted — the audit trail survives.
        assert revoked.json()["revoked_at"] is not None

        response = await client.post(
            "/v1/traces",
            json={"spans": [span_payload(hex_id(32), hex_id(16))]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 401

    async def test_key_without_ingest_scope_is_403_not_401(self, client: httpx.AsyncClient) -> None:
        """403, because we know who they are — retrying will never help."""
        project = slug("proj")
        await client.post("/projects", json={"slug": project, "name": "T"})
        created = await client.post(
            f"/projects/{project}/api-keys", json={"name": "readonly", "scopes": ["read"]}
        )
        key = created.json()["key"]

        response = await client.post(
            "/v1/traces",
            json={"spans": [span_payload(hex_id(32), hex_id(16))]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "forbidden"


class TestIngest:
    async def test_spans_land_and_a_trace_rollup_appears(self, client: httpx.AsyncClient) -> None:
        project, key = await make_project_with_key(client)
        trace_id, root_id = hex_id(32), hex_id(16)

        response = await client.post(
            "/v1/traces",
            json={"spans": [span_payload(trace_id, root_id, name="answer_question")]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert response.json() == {"accepted": 1, "duplicates": 0, "traces_touched": 1}

        traces = (await client.get(f"/projects/{project}/traces")).json()
        assert len(traces) == 1
        # The root span's name becomes the trace's, so a list is readable.
        assert traces[0]["name"] == "answer_question"
        assert traces[0]["span_count"] == 1

    async def test_ingest_is_idempotent(self, client: httpx.AsyncClient) -> None:
        """The SDK retries without knowing whether the first attempt landed."""
        _, key = await make_project_with_key(client)
        payload = {"spans": [span_payload(hex_id(32), hex_id(16))]}
        headers = {"Authorization": f"Bearer {key}"}

        first = (await client.post("/v1/traces", json=payload, headers=headers)).json()
        second = (await client.post("/v1/traces", json=payload, headers=headers)).json()

        assert first["accepted"] == 1 and first["duplicates"] == 0
        # Counted rather than hidden, so a client with broken retries can see it.
        assert second["accepted"] == 0 and second["duplicates"] == 1

    async def test_project_comes_from_the_key_not_the_body(self, client: httpx.AsyncClient) -> None:
        """A client cannot write into a project it holds no key for."""
        project_a, key_a = await make_project_with_key(client)
        project_b, _ = await make_project_with_key(client)

        await client.post(
            "/v1/traces",
            json={"spans": [span_payload(hex_id(32), hex_id(16))]},
            headers={"Authorization": f"Bearer {key_a}"},
        )

        assert len((await client.get(f"/projects/{project_a}/traces")).json()) == 1
        assert len((await client.get(f"/projects/{project_b}/traces")).json()) == 0

    async def test_totals_are_summed_across_spans(self, client: httpx.AsyncClient) -> None:
        project, key = await make_project_with_key(client)
        trace_id, root_id = hex_id(32), hex_id(16)

        await client.post(
            "/v1/traces",
            json={
                "spans": [
                    span_payload(trace_id, root_id, name="chain", kind="chain"),
                    span_payload(
                        trace_id,
                        hex_id(16),
                        root_id,
                        name="call1",
                        kind="llm",
                        prompt_tokens=100,
                        completion_tokens=50,
                        cost_usd="0.001",
                    ),
                    span_payload(
                        trace_id,
                        hex_id(16),
                        root_id,
                        name="call2",
                        kind="llm",
                        prompt_tokens=200,
                        completion_tokens=75,
                        cost_usd="0.002",
                    ),
                ]
            },
            headers={"Authorization": f"Bearer {key}"},
        )

        trace = (await client.get(f"/projects/{project}/traces")).json()[0]
        assert trace["total_prompt_tokens"] == 300
        assert trace["total_completion_tokens"] == 125
        assert float(trace["total_cost_usd"]) == pytest.approx(0.003)

    async def test_any_errored_span_makes_the_trace_an_error(
        self, client: httpx.AsyncClient
    ) -> None:
        """A request whose retrieval failed is not a success, even if it answered."""
        project, key = await make_project_with_key(client)
        trace_id, root_id = hex_id(32), hex_id(16)

        await client.post(
            "/v1/traces",
            json={
                "spans": [
                    span_payload(trace_id, root_id, name="chain"),
                    span_payload(
                        trace_id,
                        hex_id(16),
                        root_id,
                        name="retrieval",
                        status="error",
                        error_type="TimeoutError",
                    ),
                ]
            },
            headers={"Authorization": f"Bearer {key}"},
        )

        trace = (await client.get(f"/projects/{project}/traces")).json()[0]
        assert trace["status"] == "error"
        assert trace["error_count"] == 1

    async def test_malformed_trace_id_is_rejected(self, client: httpx.AsyncClient) -> None:
        _, key = await make_project_with_key(client)
        response = await client.post(
            "/v1/traces",
            json={"spans": [span_payload("not-hex", hex_id(16))]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 422

    async def test_empty_batch_is_rejected(self, client: httpx.AsyncClient) -> None:
        _, key = await make_project_with_key(client)
        response = await client.post(
            "/v1/traces", json={"spans": []}, headers={"Authorization": f"Bearer {key}"}
        )
        assert response.status_code == 422


class TestSpanTree:
    async def test_tree_is_assembled_from_parent_pointers(self, client: httpx.AsyncClient) -> None:
        project, key = await make_project_with_key(client)
        trace_id = hex_id(32)
        root_id, retrieval_id, rerank_id, llm_id = (hex_id(16) for _ in range(4))

        # A realistic RAG shape: chain -> (retrieval -> rerank), generation
        await client.post(
            "/v1/traces",
            json={
                "spans": [
                    span_payload(trace_id, root_id, None, "answer", "chain"),
                    span_payload(trace_id, retrieval_id, root_id, "retrieval", "retrieval"),
                    span_payload(trace_id, rerank_id, retrieval_id, "rerank", "rerank"),
                    span_payload(trace_id, llm_id, root_id, "generation", "llm"),
                ]
            },
            headers={"Authorization": f"Bearer {key}"},
        )

        detail = (await client.get(f"/projects/{project}/traces/{trace_id}")).json()
        root = detail["root"]

        assert root["name"] == "answer"
        assert {c["name"] for c in root["children"]} == {"retrieval", "generation"}

        retrieval = next(c for c in root["children"] if c["name"] == "retrieval")
        assert [c["name"] for c in retrieval["children"]] == ["rerank"]
        assert detail["orphans"] == []

    async def test_spans_arriving_out_of_order_still_form_a_tree(
        self, client: httpx.AsyncClient
    ) -> None:
        """Children finish before parents, so they are flushed first."""
        project, key = await make_project_with_key(client)
        trace_id, root_id, child_id = hex_id(32), hex_id(16), hex_id(16)
        headers = {"Authorization": f"Bearer {key}"}

        await client.post(
            "/v1/traces",
            json={"spans": [span_payload(trace_id, child_id, root_id, "child")]},
            headers=headers,
        )
        await client.post(
            "/v1/traces",
            json={"spans": [span_payload(trace_id, root_id, None, "root")]},
            headers=headers,
        )

        detail = (await client.get(f"/projects/{project}/traces/{trace_id}")).json()
        assert detail["root"]["name"] == "root"
        assert [c["name"] for c in detail["root"]["children"]] == ["child"]

    async def test_span_with_a_missing_parent_is_an_orphan_not_dropped(
        self, client: httpx.AsyncClient
    ) -> None:
        """Hiding it would make a partial trace look complete."""
        project, key = await make_project_with_key(client)
        trace_id, root_id = hex_id(32), hex_id(16)

        await client.post(
            "/v1/traces",
            json={
                "spans": [
                    span_payload(trace_id, root_id, None, "root"),
                    span_payload(trace_id, hex_id(16), hex_id(16), "lost"),
                ]
            },
            headers={"Authorization": f"Bearer {key}"},
        )

        detail = (await client.get(f"/projects/{project}/traces/{trace_id}")).json()
        assert [o["name"] for o in detail["orphans"]] == ["lost"]

    async def test_unknown_trace_is_404(self, client: httpx.AsyncClient) -> None:
        project, _ = await make_project_with_key(client)
        response = await client.get(f"/projects/{project}/traces/{hex_id(32)}")
        assert response.status_code == 404

    async def test_traces_are_scoped_to_their_project(self, client: httpx.AsyncClient) -> None:
        _, key = await make_project_with_key(client)
        stranger, _ = await make_project_with_key(client)
        trace_id = hex_id(32)

        await client.post(
            "/v1/traces",
            json={"spans": [span_payload(trace_id, hex_id(16))]},
            headers={"Authorization": f"Bearer {key}"},
        )

        # Knowing the id is not enough to read it from another project.
        response = await client.get(f"/projects/{stranger}/traces/{trace_id}")
        assert response.status_code == 404
