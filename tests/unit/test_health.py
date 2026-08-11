"""Probe semantics.

The important assertion is the negative one: liveness must not touch Postgres or
Redis. If it did, a dependency outage would fail every pod's liveness probe and
the kubelet would restart the whole fleet, converting a recoverable blip into a
self-inflicted outage.
"""

from __future__ import annotations

import httpx


async def test_healthz_is_alive(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_healthz_needs_no_dependencies(api_client: httpx.AsyncClient) -> None:
    # No DB/Redis fixture is in play here — the call succeeding IS the assertion.
    assert (await api_client.get("/healthz")).status_code == 200


async def test_request_id_is_echoed(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/healthz", headers={"x-request-id": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"


async def test_request_id_is_generated_when_absent(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/healthz")
    assert response.headers.get("x-request-id")
