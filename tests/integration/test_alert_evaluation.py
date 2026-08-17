"""Alert evaluation: the four gates before anything is sent."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.db.models.alerting import AlertRule
from lo_core.schemas.prompt import ProjectCreate
from lo_core.schemas.telemetry import SpanIngest
from lo_core.services import alerts as alert_service
from lo_core.services import projects as project_service
from lo_core.services import traces as trace_service

pytestmark = pytest.mark.integration


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture webhook deliveries instead of making network calls."""
    sent: list[dict[str, Any]] = []

    async def fake_deliver(rule: AlertRule, value: float, sample_size: int) -> bool:
        sent.append({"rule": rule.name, "value": value, "sample_size": sample_size})
        return True

    monkeypatch.setattr(alert_service, "deliver", fake_deliver)
    return sent


async def make_project(session: AsyncSession) -> uuid.UUID:
    project = await project_service.create_project(
        session, ProjectCreate(slug=f"alert-{uuid.uuid4().hex[:8]}", name="Alerts")
    )
    return project.id


async def seed_traces(
    session: AsyncSession, project_id: uuid.UUID, *, ok: int, errored: int
) -> None:
    """Ingest enough spans to produce `ok + errored` traces."""
    spans: list[SpanIngest] = []
    started = datetime.now(UTC) - timedelta(seconds=30)

    for index in range(ok + errored):
        spans.append(
            SpanIngest(
                trace_id=uuid.uuid4().hex,
                span_id=uuid.uuid4().hex[:16],
                name="op",
                kind="chain",
                status="error" if index >= ok else "ok",
                started_at=started,
                ended_at=started + timedelta(milliseconds=100),
                duration_ms=100,
            )
        )
    await trace_service.ingest_spans(session, project_id, spans)


async def make_rule(session: AsyncSession, project_id: uuid.UUID, **overrides: Any) -> AlertRule:
    values: dict[str, Any] = {
        "name": f"rule-{uuid.uuid4().hex[:6]}",
        "metric": "error_rate",
        "comparison": "above",
        "threshold": 0.2,
        "window_seconds": 300,
        "min_sample_size": 5,
        "cooldown_seconds": 900,
        "webhook_url": "https://example.test/hook",
    }
    values.update(overrides)
    return await alert_service.create_rule(session, project_id, **values)


class TestFiring:
    async def test_fires_when_breached(
        self, session: AsyncSession, delivered: list[dict[str, Any]]
    ) -> None:
        project_id = await make_project(session)
        await seed_traces(session, project_id, ok=5, errored=5)  # 50% error rate
        rule = await make_rule(session, project_id, threshold=0.2)

        assert await alert_service.evaluate_rule(session, rule) is True
        assert delivered[0]["value"] == pytest.approx(0.5)
        assert rule.last_fired_at is not None
        assert rule.last_value == pytest.approx(0.5)

    async def test_does_not_fire_below_threshold(
        self, session: AsyncSession, delivered: list[dict[str, Any]]
    ) -> None:
        project_id = await make_project(session)
        await seed_traces(session, project_id, ok=10, errored=0)
        rule = await make_rule(session, project_id, threshold=0.2)

        assert await alert_service.evaluate_rule(session, rule) is False
        assert delivered == []

    async def test_below_comparison(
        self, session: AsyncSession, delivered: list[dict[str, Any]]
    ) -> None:
        """`trace_count below N` catches a pipeline that stopped sending."""
        project_id = await make_project(session)
        await seed_traces(session, project_id, ok=3, errored=0)
        rule = await make_rule(
            session,
            project_id,
            metric="trace_count",
            comparison="below",
            threshold=10,
            min_sample_size=1,
        )

        assert await alert_service.evaluate_rule(session, rule) is True
        assert delivered[0]["value"] == pytest.approx(3.0)


class TestGates:
    async def test_sample_size_gate(
        self, session: AsyncSession, delivered: list[dict[str, Any]]
    ) -> None:
        """One failure out of three is a 33% error rate and means nothing."""
        project_id = await make_project(session)
        await seed_traces(session, project_id, ok=2, errored=1)  # 33%, but n=3
        rule = await make_rule(session, project_id, threshold=0.2, min_sample_size=5)

        assert await alert_service.evaluate_rule(session, rule) is False
        assert delivered == []

    async def test_cooldown_suppresses_repeat_firing(
        self, session: AsyncSession, delivered: list[dict[str, Any]]
    ) -> None:
        """The difference between an alert and a pager-spam generator.

        Evaluated every minute against a condition true for an hour, this rule
        would otherwise fire sixty times.
        """
        project_id = await make_project(session)
        await seed_traces(session, project_id, ok=0, errored=10)
        rule = await make_rule(session, project_id, threshold=0.2, cooldown_seconds=900)

        assert await alert_service.evaluate_rule(session, rule) is True
        assert await alert_service.evaluate_rule(session, rule) is False
        assert await alert_service.evaluate_rule(session, rule) is False
        assert len(delivered) == 1

    async def test_fires_again_once_the_cooldown_lapses(
        self, session: AsyncSession, delivered: list[dict[str, Any]]
    ) -> None:
        project_id = await make_project(session)
        await seed_traces(session, project_id, ok=0, errored=10)
        rule = await make_rule(session, project_id, threshold=0.2, cooldown_seconds=60)

        assert await alert_service.evaluate_rule(session, rule) is True
        rule.last_fired_at = datetime.now(UTC) - timedelta(seconds=120)
        assert await alert_service.evaluate_rule(session, rule) is True
        assert len(delivered) == 2

    async def test_no_data_does_not_fire(
        self, session: AsyncSession, delivered: list[dict[str, Any]]
    ) -> None:
        project_id = await make_project(session)
        rule = await make_rule(session, project_id, threshold=0.2)

        assert await alert_service.evaluate_rule(session, rule) is False


class TestDeliveryFailures:
    async def test_failed_delivery_still_stamps_last_fired(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise a broken endpoint retries the same alert every evaluation,
        and a recovered one gets an hour of backlog at once."""

        async def always_fail(*args: Any, **kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(alert_service, "deliver", always_fail)

        project_id = await make_project(session)
        await seed_traces(session, project_id, ok=0, errored=10)
        rule = await make_rule(session, project_id, threshold=0.2)

        assert await alert_service.evaluate_rule(session, rule) is True
        assert rule.last_fired_at is not None
        assert rule.consecutive_failures == 1

    async def test_a_persistently_dead_endpoint_disables_the_rule(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A silent alerting outage is the worst kind, so it becomes visible."""

        async def always_fail(*args: Any, **kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(alert_service, "deliver", always_fail)

        project_id = await make_project(session)
        await seed_traces(session, project_id, ok=0, errored=10)
        rule = await make_rule(session, project_id, threshold=0.2, cooldown_seconds=0)

        for _ in range(alert_service.MAX_CONSECUTIVE_FAILURES):
            await alert_service.evaluate_rule(session, rule)

        assert rule.enabled is False

    async def test_successful_delivery_resets_the_failure_count(
        self, session: AsyncSession, delivered: list[dict[str, Any]]
    ) -> None:
        project_id = await make_project(session)
        await seed_traces(session, project_id, ok=0, errored=10)
        rule = await make_rule(session, project_id, threshold=0.2, cooldown_seconds=0)
        rule.consecutive_failures = 4

        await alert_service.evaluate_rule(session, rule)
        assert rule.consecutive_failures == 0


class TestEvaluateAll:
    async def test_one_broken_rule_does_not_stop_the_others(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rules are evaluated independently — one project's bad webhook must
        not block another project's alerts."""
        calls: list[str] = []

        async def flaky(rule: AlertRule, value: float, sample_size: int) -> bool:
            calls.append(rule.name)
            if rule.name.startswith("boom"):
                raise RuntimeError("delivery exploded")
            return True

        monkeypatch.setattr(alert_service, "deliver", flaky)

        project_id = await make_project(session)
        await seed_traces(session, project_id, ok=0, errored=10)
        await make_rule(session, project_id, name="boom-rule", threshold=0.2)
        await make_rule(session, project_id, name="good-rule", threshold=0.2)

        fired = await alert_service.evaluate_all(session)

        assert "good-rule" in calls
        assert fired >= 1

    async def test_disabled_rules_are_skipped(
        self, session: AsyncSession, delivered: list[dict[str, Any]]
    ) -> None:
        project_id = await make_project(session)
        await seed_traces(session, project_id, ok=0, errored=10)
        await make_rule(session, project_id, threshold=0.2, enabled=False)

        assert await alert_service.evaluate_all(session) == 0
        assert delivered == []
