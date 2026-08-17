"""Dashboard metrics and alert rules, end to end."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from lo_core.ratelimit import RateLimitResult

    async def allow(*args: Any, **kwargs: Any) -> RateLimitResult:
        return RateLimitResult(allowed=True, limit=6000, remaining=6000, retry_after=0)

    monkeypatch.setattr("lo_api.routers.traces.check_rate_limit", allow)


def hex_id(length: int) -> str:
    return uuid.uuid4().hex[:length].ljust(length, "0")


async def project_with_key(client: httpx.AsyncClient) -> tuple[str, str]:
    slug = f"proj-{uuid.uuid4().hex[:10]}"
    await client.post("/projects", json={"slug": slug, "name": "Metrics test"})
    created = await client.post(f"/projects/{slug}/api-keys", json={"name": "sdk"})
    return slug, created.json()["key"]


async def send_spans(client: httpx.AsyncClient, key: str, spans: list[dict[str, Any]]) -> None:
    response = await client.post(
        "/v1/traces", json={"spans": spans}, headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 202, response.text


def span(
    trace_id: str,
    span_id: str,
    *,
    parent: str | None = None,
    minutes_ago: int = 1,
    duration_ms: int = 100,
    status: str = "ok",
    kind: str = "llm",
    model: str | None = "claude-opus-5",
    cost: str | None = "0.001",
    tokens: tuple[int, int] = (100, 50),
) -> dict[str, Any]:
    started = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent,
        "name": "op",
        "kind": kind,
        "status": status,
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(milliseconds=duration_ms)).isoformat(),
        "duration_ms": duration_ms,
        "model": model,
        "prompt_tokens": tokens[0],
        "completion_tokens": tokens[1],
        "cost_usd": cost,
    }


class TestSummary:
    async def test_counts_and_cost_are_aggregated(self, client: httpx.AsyncClient) -> None:
        project, key = await project_with_key(client)
        await send_spans(
            client,
            key,
            [
                span(hex_id(32), hex_id(16), duration_ms=100),
                span(hex_id(32), hex_id(16), duration_ms=200),
                span(hex_id(32), hex_id(16), duration_ms=300),
            ],
        )

        body = (await client.get(f"/projects/{project}/metrics?window=1h")).json()
        s = body["summary"]
        assert s["span_count"] == 3
        assert s["trace_count"] == 3
        assert s["error_count"] == 0
        assert s["error_rate"] == 0.0
        assert s["cost_usd"] == pytest.approx(0.003)
        assert s["prompt_tokens"] == 300

    async def test_percentiles_are_exact(self, client: httpx.AsyncClient) -> None:
        """`percentile_cont` interpolates over the real values — an approximate
        p99 that is wrong in the tail is wrong exactly where you were looking."""
        project, key = await project_with_key(client)
        await send_spans(
            client,
            key,
            [span(hex_id(32), hex_id(16), duration_ms=d) for d in (100, 200, 300, 400, 500)],
        )

        s = (await client.get(f"/projects/{project}/metrics?window=1h")).json()["summary"]
        assert s["p50_latency_ms"] == pytest.approx(300)
        assert s["p95_latency_ms"] == pytest.approx(480)

    async def test_error_rate(self, client: httpx.AsyncClient) -> None:
        project, key = await project_with_key(client)
        await send_spans(
            client,
            key,
            [
                span(hex_id(32), hex_id(16)),
                span(hex_id(32), hex_id(16)),
                span(hex_id(32), hex_id(16), status="error"),
                span(hex_id(32), hex_id(16), status="error"),
            ],
        )

        s = (await client.get(f"/projects/{project}/metrics?window=1h")).json()["summary"]
        assert s["error_rate"] == pytest.approx(0.5)
        assert s["error_count"] == 2

    async def test_spans_outside_the_window_are_excluded(self, client: httpx.AsyncClient) -> None:
        """The time bound is what lets a hypertable skip whole chunks."""
        project, key = await project_with_key(client)
        await send_spans(
            client,
            key,
            [
                span(hex_id(32), hex_id(16), minutes_ago=2),
                span(hex_id(32), hex_id(16), minutes_ago=120),
            ],
        )

        recent = (await client.get(f"/projects/{project}/metrics?window=15m")).json()
        wide = (await client.get(f"/projects/{project}/metrics?window=6h")).json()

        assert recent["summary"]["span_count"] == 1
        assert wide["summary"]["span_count"] == 2

    async def test_metrics_are_scoped_to_the_project(self, client: httpx.AsyncClient) -> None:
        mine, my_key = await project_with_key(client)
        theirs, _ = await project_with_key(client)

        await send_spans(client, my_key, [span(hex_id(32), hex_id(16))])

        assert (await client.get(f"/projects/{mine}/metrics")).json()["summary"]["span_count"] == 1
        assert (await client.get(f"/projects/{theirs}/metrics")).json()["summary"][
            "span_count"
        ] == 0


class TestSeries:
    async def test_series_is_bucketed(self, client: httpx.AsyncClient) -> None:
        project, key = await project_with_key(client)
        await send_spans(
            client,
            key,
            [span(hex_id(32), hex_id(16), minutes_ago=m) for m in (1, 2, 20, 21)],
        )

        body = (await client.get(f"/projects/{project}/metrics?window=1h")).json()
        assert body["bucket"] == "1m"
        # Four spans across four distinct minutes.
        assert len(body["series"]) == 4
        assert sum(p["span_count"] for p in body["series"]) == 4

    async def test_window_selects_the_bucket_width(self, client: httpx.AsyncClient) -> None:
        """Pairing them stops a caller asking for 43,200 points in one chart."""
        project, key = await project_with_key(client)
        await send_spans(client, key, [span(hex_id(32), hex_id(16))])

        assert (await client.get(f"/projects/{project}/metrics?window=15m")).json()[
            "bucket"
        ] == "1m"
        assert (await client.get(f"/projects/{project}/metrics?window=7d")).json()["bucket"] == "1h"
        assert (await client.get(f"/projects/{project}/metrics?window=30d")).json()[
            "bucket"
        ] == "1d"

    async def test_unknown_window_is_rejected(self, client: httpx.AsyncClient) -> None:
        project, _ = await project_with_key(client)
        response = await client.get(f"/projects/{project}/metrics?window=nope")
        assert response.status_code == 422

    async def test_empty_window_returns_an_empty_series(self, client: httpx.AsyncClient) -> None:
        project, _ = await project_with_key(client)
        body = (await client.get(f"/projects/{project}/metrics?window=1h")).json()
        assert body["series"] == []
        assert body["summary"]["span_count"] == 0


class TestFilters:
    async def test_filter_by_model(self, client: httpx.AsyncClient) -> None:
        project, key = await project_with_key(client)
        await send_spans(
            client,
            key,
            [
                span(hex_id(32), hex_id(16), model="claude-opus-5"),
                span(hex_id(32), hex_id(16), model="claude-haiku-4-5"),
                span(hex_id(32), hex_id(16), model="claude-haiku-4-5"),
            ],
        )

        body = (
            await client.get(f"/projects/{project}/metrics?window=1h&model=claude-haiku-4-5")
        ).json()
        assert body["summary"]["span_count"] == 2

    async def test_filter_by_kind(self, client: httpx.AsyncClient) -> None:
        project, key = await project_with_key(client)
        await send_spans(
            client,
            key,
            [
                span(hex_id(32), hex_id(16), kind="llm"),
                span(hex_id(32), hex_id(16), kind="retrieval", model=None),
            ],
        )

        body = (await client.get(f"/projects/{project}/metrics?window=1h&kind=retrieval")).json()
        assert body["summary"]["span_count"] == 1


class TestBreakdown:
    async def test_group_by_model(self, client: httpx.AsyncClient) -> None:
        project, key = await project_with_key(client)
        await send_spans(
            client,
            key,
            [
                span(hex_id(32), hex_id(16), model="claude-opus-5", cost="0.005"),
                span(hex_id(32), hex_id(16), model="claude-haiku-4-5", cost="0.001"),
                span(hex_id(32), hex_id(16), model="claude-haiku-4-5", cost="0.001"),
            ],
        )

        rows = (
            await client.get(f"/projects/{project}/metrics/breakdown?window=1h&dimension=model")
        ).json()
        by_model = {r["label"]: r for r in rows}

        assert by_model["claude-haiku-4-5"]["span_count"] == 2
        assert by_model["claude-opus-5"]["cost_usd"] == pytest.approx(0.005)

    async def test_spans_without_a_model_are_excluded(self, client: httpx.AsyncClient) -> None:
        """A null model on a retrieval span is not a category anyone wants a row for."""
        project, key = await project_with_key(client)
        await send_spans(
            client,
            key,
            [
                span(hex_id(32), hex_id(16), model="claude-opus-5"),
                span(hex_id(32), hex_id(16), kind="retrieval", model=None),
            ],
        )

        rows = (
            await client.get(f"/projects/{project}/metrics/breakdown?window=1h&dimension=model")
        ).json()
        assert [r["label"] for r in rows] == ["claude-opus-5"]

    async def test_group_by_kind(self, client: httpx.AsyncClient) -> None:
        project, key = await project_with_key(client)
        await send_spans(
            client,
            key,
            [
                span(hex_id(32), hex_id(16), kind="llm"),
                span(hex_id(32), hex_id(16), kind="retrieval", model=None),
                span(hex_id(32), hex_id(16), kind="retrieval", model=None),
            ],
        )

        rows = (
            await client.get(f"/projects/{project}/metrics/breakdown?window=1h&dimension=kind")
        ).json()
        by_kind = {r["label"]: r["span_count"] for r in rows}
        assert by_kind == {"llm": 1, "retrieval": 2}


class TestAlertRules:
    async def test_create_and_list(self, client: httpx.AsyncClient) -> None:
        project, _ = await project_with_key(client)

        created = await client.post(
            f"/projects/{project}/alerts",
            json={
                "name": "high error rate",
                "metric": "error_rate",
                "comparison": "above",
                "threshold": 0.05,
                "webhook_url": "https://example.test/hook",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["enabled"] is True
        # Defaults that matter: a cooldown so a sustained breach fires once.
        assert created.json()["cooldown_seconds"] == 900
        assert created.json()["min_sample_size"] == 5

        listed = (await client.get(f"/projects/{project}/alerts")).json()
        assert len(listed) == 1

    async def test_unknown_metric_is_rejected(self, client: httpx.AsyncClient) -> None:
        project, _ = await project_with_key(client)
        response = await client.post(
            f"/projects/{project}/alerts",
            json={
                "name": "bad",
                "metric": "vibes",
                "threshold": 1,
                "webhook_url": "https://example.test/hook",
            },
        )
        assert response.status_code == 422

    async def test_duplicate_name_conflicts(self, client: httpx.AsyncClient) -> None:
        project, _ = await project_with_key(client)
        payload = {
            "name": "dupe",
            "metric": "error_rate",
            "threshold": 0.1,
            "webhook_url": "https://example.test/hook",
        }
        assert (await client.post(f"/projects/{project}/alerts", json=payload)).status_code == 201
        assert (await client.post(f"/projects/{project}/alerts", json=payload)).status_code == 409

    async def test_delete(self, client: httpx.AsyncClient) -> None:
        project, _ = await project_with_key(client)
        created = await client.post(
            f"/projects/{project}/alerts",
            json={
                "name": "temp",
                "metric": "cost_usd",
                "threshold": 10,
                "webhook_url": "https://example.test/hook",
            },
        )
        rule_id = created.json()["id"]

        assert (await client.delete(f"/projects/{project}/alerts/{rule_id}")).status_code == 204
        assert (await client.get(f"/projects/{project}/alerts")).json() == []
