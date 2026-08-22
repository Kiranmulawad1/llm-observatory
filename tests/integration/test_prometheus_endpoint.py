"""`/metrics`, served by the real application.

The interesting assertions are about what is *absent*: no credential required,
and no tenant identifier in the output. Those two facts depend on each other —
see ADR 0014 — so they are tested together rather than in separate files where
one could be changed without the other.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration


def slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@pytest.fixture(autouse=True)
def _stub_queue_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis is not the subject here, and a scrape must not need it."""

    async def depth(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"arq:queue": 7}

    monkeypatch.setattr("lo_api.routers.prometheus.queue_depth", depth)


class TestExposition:
    async def test_returns_prometheus_text_format(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "# TYPE lo_http_requests_total counter" in response.text

    async def test_queue_depth_is_sampled_at_scrape_time(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics")
        assert 'lo_queue_depth{queue="arq:queue"} 7.0' in response.text

    async def test_scrape_survives_redis_being_down(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rest of the registry is exactly what someone debugging a Redis
        outage needs, so a scrape must not fail with it."""

        async def boom(*args: Any, **kwargs: Any) -> dict[str, int]:
            raise ConnectionError("redis is down")

        monkeypatch.setattr("lo_api.routers.prometheus.queue_depth", boom)
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert "lo_http_requests_total" in response.text


class TestAuthPosture:
    async def test_metrics_is_deliberately_unauthenticated(self, client: httpx.AsyncClient) -> None:
        """Every other endpoint requires a credential (ADR 0010).

        This one does not, for the same reason /healthz does not: the scraper is
        infrastructure and giving Prometheus an operator token to poll every
        fifteen seconds would put that credential in a config file on a
        schedule. The access control is the NetworkPolicy.
        """
        response = await client.get("/metrics", headers={"Authorization": ""})
        assert response.status_code == 200

    async def test_no_tenant_identifier_appears_in_the_output(
        self, client: httpx.AsyncClient
    ) -> None:
        """What makes the missing auth *safe* rather than merely convenient.

        If a project slug ever reached this output, the lack of a credential
        would stop being a considered trade and become a leak.
        """
        name = slug("secretco")
        assert (
            await client.post("/projects", json={"slug": name, "name": "Secret"})
        ).status_code == 201
        await client.get(f"/projects/{name}/prompts")

        body = (await client.get("/metrics")).text
        assert name not in body
        for label in ("project_id=", "project_slug=", "model=", "trace_id="):
            assert label not in body


class TestHttpInstrumentation:
    async def test_requests_are_counted_by_route_template(self, client: httpx.AsyncClient) -> None:
        """`/projects/{project_slug}/prompts`, not `/projects/acme/prompts`.

        The resolved path would mint a new series per project — the same
        cardinality trap, arriving through the middleware instead of the metric
        definition.
        """
        name = slug("proj")
        await client.post("/projects", json={"slug": name, "name": "T"})
        await client.get(f"/projects/{name}/prompts")

        body = (await client.get("/metrics")).text
        assert 'path="/projects/{project_slug}/prompts"' in body

    async def test_unmatched_paths_do_not_create_series(self, client: httpx.AsyncClient) -> None:
        """Otherwise anyone can mint labels by spraying 404s at random URLs."""
        await client.get(f"/no-such-route-{uuid.uuid4().hex}")
        body = (await client.get("/metrics")).text
        assert 'path="<unmatched>"' in body
        assert "no-such-route" not in body
