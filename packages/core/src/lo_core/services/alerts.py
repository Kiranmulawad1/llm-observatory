"""Alert rule evaluation and webhook delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lo_core.config import get_settings
from lo_core.db.models.alerting import AlertRule
from lo_core.errors import ConflictError, NotFoundError
from lo_core.logging import get_logger
from lo_core.services import metrics as metrics_service

log = get_logger(__name__)

WEBHOOK_TIMEOUT = 10.0
# A webhook endpoint that has been dead this many consecutive evaluations is
# disabled. Retrying a decommissioned URL forever is pure noise, and the
# disabled flag is visible in the UI — which is the point.
MAX_CONSECUTIVE_FAILURES = 10


async def create_rule(session: AsyncSession, project_id: uuid.UUID, **values: Any) -> AlertRule:
    rule = AlertRule(project_id=project_id, **values)
    session.add(rule)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"alert rule {values.get('name')!r} already exists") from exc
    return rule


async def list_rules(session: AsyncSession, project_id: uuid.UUID) -> list[AlertRule]:
    result = await session.execute(
        select(AlertRule).where(AlertRule.project_id == project_id).order_by(AlertRule.name)
    )
    return list(result.scalars().all())


async def get_rule(session: AsyncSession, project_id: uuid.UUID, rule_id: uuid.UUID) -> AlertRule:
    result = await session.execute(
        select(AlertRule).where(AlertRule.id == rule_id, AlertRule.project_id == project_id)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise NotFoundError(f"alert rule {rule_id} not found")
    return rule


async def delete_rule(session: AsyncSession, rule: AlertRule) -> None:
    await session.delete(rule)
    await session.flush()


def is_breached(value: float, comparison: str, threshold: float) -> bool:
    return value > threshold if comparison == "above" else value < threshold


def sign_payload(payload: bytes) -> str:
    """HMAC-SHA256 over the exact bytes sent.

    Lets the receiver prove the notification came from this platform and was not
    modified. Without it, anyone who learns the webhook URL can forge alerts —
    and alert endpoints frequently do things like page a human or open a ticket.

    Signed over the serialised bytes rather than a re-serialised dict: any
    difference in key order or whitespace produces a different signature, so the
    receiver must verify against the raw body it received.
    """
    secret = get_settings().api_key_pepper.get_secret_value()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


async def deliver(rule: AlertRule, value: float, sample_size: int) -> bool:
    """POST the alert. Returns whether delivery succeeded."""
    body = {
        "type": "alert.triggered",
        "rule": {
            "id": str(rule.id),
            "name": rule.name,
            "metric": rule.metric,
            "comparison": rule.comparison,
            "threshold": rule.threshold,
            "window_seconds": rule.window_seconds,
        },
        "value": value,
        "sample_size": sample_size,
        "project_id": str(rule.project_id),
        "triggered_at": datetime.now(UTC).isoformat(),
    }
    payload = json.dumps(body, separators=(",", ":")).encode()

    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
            response = await client.post(
                rule.webhook_url,
                content=payload,
                headers={
                    "content-type": "application/json",
                    "x-lo-signature": sign_payload(payload),
                    "x-lo-event": "alert.triggered",
                },
            )
        return response.status_code < 300
    except Exception as exc:
        log.warning("alert.delivery_failed", rule=rule.name, error=str(exc))
        return False


async def evaluate_rule(session: AsyncSession, rule: AlertRule) -> bool:
    """Evaluate one rule and fire if warranted. Returns whether it fired.

    Four gates before anything is sent, in order of cheapness:

    1. **Cooldown.** A condition true for an hour must notify once, not sixty
       times. This is checked first because it is free and skips the query.
    2. **Sample size.** One failed request out of three is a 100% error rate and
       is not worth waking anyone for.
    3. **Threshold.** The actual comparison.
    4. **Delivery.** Failures are counted; a persistently dead endpoint is
       disabled rather than retried forever.
    """
    now = datetime.now(UTC)

    if rule.last_fired_at is not None:
        if now - rule.last_fired_at < timedelta(seconds=rule.cooldown_seconds):
            return False

    value, sample_size = await metrics_service.metric_value(
        session, rule.project_id, rule.metric, rule.window_seconds
    )

    if value is None or sample_size < rule.min_sample_size:
        return False
    if not is_breached(value, rule.comparison, rule.threshold):
        return False

    delivered = await deliver(rule, value, sample_size)

    # `last_fired_at` is stamped even when delivery fails. Otherwise a broken
    # endpoint would retry the same alert every single evaluation, and a
    # recovered endpoint would be hit with an hour of backlog at once.
    rule.last_fired_at = now
    rule.last_value = value

    if delivered:
        rule.consecutive_failures = 0
    else:
        rule.consecutive_failures += 1
        if rule.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            rule.enabled = False
            log.error("alert.rule_disabled", rule=rule.name, failures=rule.consecutive_failures)

    await session.flush()
    log.info("alert.triggered", rule=rule.name, value=value, delivered=delivered)
    return True


async def evaluate_all(session: AsyncSession) -> int:
    """Evaluate every enabled rule. Returns how many fired.

    Rules are evaluated independently: one project's broken webhook must not
    stop another project's alerts from being checked.
    """
    result = await session.execute(select(AlertRule).where(AlertRule.enabled.is_(True)))
    rules = list(result.scalars().all())

    fired = 0
    for rule in rules:
        try:
            if await evaluate_rule(session, rule):
                fired += 1
        except Exception as exc:
            log.error("alert.evaluation_failed", rule=rule.name, error=str(exc))

    return fired
