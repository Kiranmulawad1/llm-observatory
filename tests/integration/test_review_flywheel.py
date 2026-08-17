"""The data flywheel, end to end.

    production trace -> sampled -> flagged -> labelled -> dataset example

The final test walks the whole loop, which is the thing this phase exists to
make real rather than describe.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.db.models.project import Project
from lo_core.services import review as service

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
    slug = f"rev-{uuid.uuid4().hex[:10]}"
    await client.post("/projects", json={"slug": slug, "name": "Review test"})
    created = await client.post(f"/projects/{slug}/api-keys", json={"name": "sdk"})
    return slug, created.json()["key"]


async def send_trace(
    client: httpx.AsyncClient,
    key: str,
    *,
    question: str,
    answer: str,
    context: list[str] | None = None,
    status: str = "ok",
) -> str:
    """Ingest a two-span trace: a root chain plus an llm generation."""
    trace_id, root_id, llm_id = hex_id(32), hex_id(16), hex_id(16)
    started = datetime.now(UTC) - timedelta(seconds=30)

    spans: list[dict[str, Any]] = [
        {
            "trace_id": trace_id,
            "span_id": root_id,
            "name": "answer_question",
            "kind": "chain",
            "status": status,
            "started_at": started.isoformat(),
            "ended_at": (started + timedelta(milliseconds=200)).isoformat(),
            "duration_ms": 200,
            "input": {"question": question},
        }
    ]
    if context is not None:
        spans.append(
            {
                "trace_id": trace_id,
                "span_id": hex_id(16),
                "parent_span_id": root_id,
                "name": "retrieval",
                "kind": "retrieval",
                "started_at": started.isoformat(),
                "ended_at": (started + timedelta(milliseconds=50)).isoformat(),
                "duration_ms": 50,
                "output": {"documents": context},
            }
        )
    spans.append(
        {
            "trace_id": trace_id,
            "span_id": llm_id,
            "parent_span_id": root_id,
            "name": "generation",
            "kind": "llm",
            "status": status,
            "started_at": started.isoformat(),
            "ended_at": (started + timedelta(milliseconds=150)).isoformat(),
            "duration_ms": 150,
            "model": "claude-opus-5",
            "output": {"text": answer},
        }
    )

    response = await client.post(
        "/v1/traces", json={"spans": spans}, headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 202, response.text
    return trace_id


async def enable_guardrails(client: httpx.AsyncClient, project: str, **overrides: Any) -> None:
    payload = {
        "enabled": True,
        # Sample everything, so tests are deterministic rather than depending on
        # which side of the hash a generated trace id lands.
        "sample_rate": 1.0,
        "control_sample_rate": 0.0,
    }
    payload.update(overrides)
    response = await client.put(f"/projects/{project}/guardrails", json=payload)
    assert response.status_code == 200, response.text


async def run_sampler(session: AsyncSession, project_slug: str) -> int:
    """Invoke the sampler the way the cron does.

    Takes the `session` fixture, which is bound to the same connection as the
    `client` fixture — so it sees the traces the API just ingested, inside the
    same rolled-back transaction.
    """
    project = (
        await session.execute(select(Project).where(Project.slug == project_slug))
    ).scalar_one()
    config = await service.get_config(session, project.id)
    assert config is not None
    return await service.sample_project(session, config)


class TestSampling:
    async def test_a_flagged_trace_enters_the_queue(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        project, key = await project_with_key(client)
        await enable_guardrails(client, project)
        await send_trace(
            client,
            key,
            question="What is the price?",
            answer="It costs 4999 dollars.",
            context=["Our products range from 20 to 80 dollars."],
        )

        assert await run_sampler(session, project) == 1

        items = (await client.get(f"/projects/{project}/review")).json()
        assert len(items) == 1
        assert items[0]["sampled_as"] == "flagged"
        assert [f["check"] for f in items[0]["findings"]] == ["grounding"]
        assert "4999" in items[0]["findings"][0]["detail"]["unsupported_numbers"]

    async def test_a_clean_trace_is_not_queued(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        project, key = await project_with_key(client)
        await enable_guardrails(client, project, control_sample_rate=0.0)
        await send_trace(
            client,
            key,
            question="When does it ship?",
            answer="It ships Tuesday.",
            context=["Orders ship on Tuesday."],
        )

        assert await run_sampler(session, project) == 0
        assert (await client.get(f"/projects/{project}/review")).json() == []

    async def test_control_sampling_queues_clean_traces(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """The instrument that measures what the checks miss."""
        project, key = await project_with_key(client)
        await enable_guardrails(client, project, control_sample_rate=1.0)
        await send_trace(
            client,
            key,
            question="When does it ship?",
            answer="It ships Tuesday.",
            context=["Orders ship on Tuesday."],
        )

        assert await run_sampler(session, project) == 1
        items = (await client.get(f"/projects/{project}/review")).json()
        assert items[0]["sampled_as"] == "control"
        assert items[0]["findings"] == []

    async def test_pii_in_output_is_flagged(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        project, key = await project_with_key(client)
        await enable_guardrails(client, project)
        await send_trace(
            client, key, question="Who handles this?", answer="Email alice@example.com."
        )

        await run_sampler(session, project)
        items = (await client.get(f"/projects/{project}/review")).json()
        assert [f["check"] for f in items[0]["findings"]] == ["pii"]

    async def test_sampling_is_idempotent(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """A re-run over an overlapping window must not hand a human the same
        trace twice."""
        project, key = await project_with_key(client)
        await enable_guardrails(client, project)
        await send_trace(
            client, key, question="q", answer="It costs 4999.", context=["nothing relevant"]
        )

        first = await run_sampler(session, project)
        assert first == 1

        # Rewind the watermark so the same trace is back in the window.
        project_row = (
            await session.execute(select(Project).where(Project.slug == project))
        ).scalar_one()
        config = await service.get_config(session, project_row.id)
        assert config is not None
        config.last_scanned_at = datetime.now(UTC) - timedelta(hours=1)

        assert await service.sample_project(session, config) == 0
        assert len((await client.get(f"/projects/{project}/review")).json()) == 1

    async def test_disabled_guardrails_sample_nothing(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        project, key = await project_with_key(client)
        await enable_guardrails(client, project, enabled=False)
        await send_trace(client, key, question="q", answer="It costs 4999.", context=["x"])

        assert await run_sampler(session, project) == 0

    async def test_the_snapshot_survives_independently_of_the_trace(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """Telemetry is under retention; a labelled example is worth more the
        older it gets. The item copies what it needs."""
        project, key = await project_with_key(client)
        await enable_guardrails(client, project)
        await send_trace(
            client,
            key,
            question="What is the price?",
            answer="It costs 4999 dollars.",
            context=["Products cost 20 to 80 dollars."],
        )
        await run_sampler(session, project)

        item = (await client.get(f"/projects/{project}/review")).json()[0]
        assert item["inputs"] == {"question": "What is the price?"}
        assert item["output"] == "It costs 4999 dollars."
        assert item["context"] == ["Products cost 20 to 80 dollars."]
        assert item["model"] == "claude-opus-5"


class TestLabeling:
    async def test_label_records_the_verdict(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        project, key = await project_with_key(client)
        await enable_guardrails(client, project)
        await send_trace(client, key, question="q", answer="It costs 4999.", context=["x"])
        await run_sampler(session, project)

        item_id = (await client.get(f"/projects/{project}/review")).json()[0]["id"]
        response = await client.post(
            f"/projects/{project}/review/{item_id}/label",
            json={
                "verdict": "bad",
                "reason": "hallucinated_price",
                "corrected_output": "I don't have pricing for that item.",
                "labeled_by": "kiran",
            },
        )

        assert response.status_code == 200
        assert response.json()["verdict"] == "bad"
        assert response.json()["status"] == "labeled"
        assert response.json()["labeled_at"] is not None

    async def test_labeled_items_leave_the_pending_queue(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        project, key = await project_with_key(client)
        await enable_guardrails(client, project)
        await send_trace(client, key, question="q", answer="It costs 4999.", context=["x"])
        await run_sampler(session, project)

        item_id = (await client.get(f"/projects/{project}/review")).json()[0]["id"]
        await client.post(
            f"/projects/{project}/review/{item_id}/label",
            json={"verdict": "good"},
        )

        assert (await client.get(f"/projects/{project}/review?status=pending")).json() == []
        assert len((await client.get(f"/projects/{project}/review?status=labeled")).json()) == 1

    async def test_skip_dismisses_without_a_verdict(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        project, key = await project_with_key(client)
        await enable_guardrails(client, project)
        await send_trace(client, key, question="q", answer="It costs 4999.", context=["x"])
        await run_sampler(session, project)

        item_id = (await client.get(f"/projects/{project}/review")).json()[0]["id"]
        response = await client.post(f"/projects/{project}/review/{item_id}/skip")

        assert response.json()["status"] == "skipped"
        assert response.json()["verdict"] is None

    async def test_invalid_verdict_rejected(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        project, key = await project_with_key(client)
        await enable_guardrails(client, project)
        await send_trace(client, key, question="q", answer="It costs 4999.", context=["x"])
        await run_sampler(session, project)

        item_id = (await client.get(f"/projects/{project}/review")).json()[0]["id"]
        response = await client.post(
            f"/projects/{project}/review/{item_id}/label", json={"verdict": "maybe"}
        )
        assert response.status_code == 422

    async def test_queue_is_ordered_worst_first(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """A leaked key is reviewed before an ungrounded number."""
        project, key = await project_with_key(client)
        await enable_guardrails(client, project)
        await send_trace(client, key, question="a", answer="It costs 4999.", context=["x"])
        await send_trace(
            client, key, question="b", answer="key sk-abcdefghijklmnop1234567890", context=["x"]
        )
        await run_sampler(session, project)

        items = (await client.get(f"/projects/{project}/review")).json()
        assert items[0]["findings"][0]["check"] == "pii"
        assert items[0]["severity"] > items[1]["severity"]


class TestStats:
    async def test_control_sample_measures_the_miss_rate(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """The only reason the control sample exists: a clean trace a human
        judges bad is a check that missed something."""
        project, key = await project_with_key(client)
        await enable_guardrails(client, project, control_sample_rate=1.0)
        await send_trace(
            client,
            key,
            question="q",
            answer="It ships Tuesday.",
            context=["Orders ship Tuesday."],
        )
        await run_sampler(session, project)

        item_id = (await client.get(f"/projects/{project}/review")).json()[0]["id"]
        await client.post(
            f"/projects/{project}/review/{item_id}/label",
            json={"verdict": "bad", "corrected_output": "It ships Wednesday."},
        )

        stats = (await client.get(f"/projects/{project}/review/stats")).json()
        assert stats["control_reviewed"] == 1
        assert stats["control_missed"] == 1
        assert stats["estimated_miss_rate"] == pytest.approx(1.0)


class TestFlywheel:
    async def test_a_bad_label_without_a_correction_cannot_be_promoted(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """An example with no expected answer is unscoreable by exactly the
        evaluators you would run against it."""
        project, key = await project_with_key(client)
        await enable_guardrails(client, project)
        await client.post(f"/projects/{project}/datasets", json={"slug": "qa", "name": "QA"})
        await send_trace(client, key, question="q", answer="It costs 4999.", context=["x"])
        await run_sampler(session, project)

        item_id = (await client.get(f"/projects/{project}/review")).json()[0]["id"]
        await client.post(f"/projects/{project}/review/{item_id}/label", json={"verdict": "bad"})

        response = await client.post(
            f"/projects/{project}/review/promote",
            json={"item_ids": [item_id], "dataset": "qa"},
        )
        assert response.status_code == 422
        assert "corrected output" in response.json()["detail"]

    async def test_unlabeled_items_cannot_be_promoted(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        project, key = await project_with_key(client)
        await enable_guardrails(client, project)
        await client.post(f"/projects/{project}/datasets", json={"slug": "qa", "name": "QA"})
        await send_trace(client, key, question="q", answer="It costs 4999.", context=["x"])
        await run_sampler(session, project)

        item_id = (await client.get(f"/projects/{project}/review")).json()[0]["id"]
        response = await client.post(
            f"/projects/{project}/review/promote",
            json={"item_ids": [item_id], "dataset": "qa"},
        )
        assert response.status_code == 422

    async def test_promoting_twice_is_rejected(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        project, key = await project_with_key(client)
        await enable_guardrails(client, project)
        await client.post(f"/projects/{project}/datasets", json={"slug": "qa", "name": "QA"})
        await send_trace(client, key, question="q", answer="It costs 4999.", context=["x"])
        await run_sampler(session, project)

        item_id = (await client.get(f"/projects/{project}/review")).json()[0]["id"]
        await client.post(
            f"/projects/{project}/review/{item_id}/label",
            json={"verdict": "bad", "corrected_output": "I don't know the price."},
        )
        body = {"item_ids": [item_id], "dataset": "qa"}

        assert (
            await client.post(f"/projects/{project}/review/promote", json=body)
        ).status_code == 201
        assert (
            await client.post(f"/projects/{project}/review/promote", json=body)
        ).status_code == 409

    async def test_the_full_loop(self, client: httpx.AsyncClient, session: AsyncSession) -> None:
        """production trace -> flagged -> labelled -> dataset example.

        This is the flywheel the whole platform exists to turn, so it is tested
        as one continuous path rather than in pieces.
        """
        project, key = await project_with_key(client)
        await enable_guardrails(client, project)
        await client.post(f"/projects/{project}/datasets", json={"slug": "qa", "name": "QA"})

        # 1. A production request hallucinates a price.
        await send_trace(
            client,
            key,
            question="How much is the widget?",
            answer="The widget costs 4999 dollars.",
            context=["Widgets are currently out of stock."],
        )

        # 2. Sampling flags it — the number appears nowhere in the context.
        assert await run_sampler(session, project) == 1
        item = (await client.get(f"/projects/{project}/review")).json()[0]
        assert item["findings"][0]["check"] == "grounding"

        # 3. A human labels it and supplies the answer it should have given.
        await client.post(
            f"/projects/{project}/review/{item['id']}/label",
            json={
                "verdict": "bad",
                "reason": "hallucinated_price",
                "corrected_output": "Widgets are currently out of stock.",
                "labeled_by": "kiran",
            },
        )

        # 4. Promotion appends it to the eval dataset as a new version.
        promoted = await client.post(
            f"/projects/{project}/review/promote",
            json={"item_ids": [item["id"]], "dataset": "qa", "created_by": "kiran"},
        )
        assert promoted.status_code == 201, promoted.text
        version = promoted.json()
        assert version["version"] == 1
        assert version["item_count"] == 1

        # 5. It is now an ordinary eval example — the next prompt change is
        #    tested against the failure that produced it.
        items = (await client.get(f"/projects/{project}/datasets/qa/versions/1/items")).json()
        assert items[0]["inputs"] == {"question": "How much is the widget?"}
        assert items[0]["expected_output"] == "Widgets are currently out of stock."

        # 6. Provenance survives, so "where did this example come from?" is
        #    answerable months later.
        listed = (await client.get(f"/projects/{project}/review?status=labeled")).json()
        assert listed[0]["promoted_at"] is not None

    async def test_promotion_carries_forward_existing_examples(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """A dataset version is a complete snapshot, not a delta."""
        project, key = await project_with_key(client)
        await enable_guardrails(client, project)
        await client.post(f"/projects/{project}/datasets", json={"slug": "qa", "name": "QA"})
        await client.post(
            f"/projects/{project}/datasets/qa/versions",
            json={"items": [{"inputs": {"question": "existing"}, "expected_output": "yes"}]},
        )

        await send_trace(client, key, question="new", answer="It costs 4999.", context=["x"])
        await run_sampler(session, project)
        item_id = (await client.get(f"/projects/{project}/review")).json()[0]["id"]
        await client.post(
            f"/projects/{project}/review/{item_id}/label",
            json={"verdict": "bad", "corrected_output": "I don't know."},
        )

        promoted = await client.post(
            f"/projects/{project}/review/promote",
            json={"item_ids": [item_id], "dataset": "qa"},
        )
        assert promoted.json()["version"] == 2
        assert promoted.json()["item_count"] == 2
