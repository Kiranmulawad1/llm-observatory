"""Alert rules.

An alert rule is a threshold on a metric over a window, plus somewhere to send
the notification when it is crossed.

The design problem here is not detection — that is one query. It is **not being
a pager-spam generator.** A rule evaluated every minute against a condition that
stays true for an hour will fire sixty times unless something stops it, and an
alerting system that cries wolf gets muted, which is strictly worse than having
no alerting at all. `cooldown_seconds` plus `last_fired_at` is what turns a
sustained breach into one notification.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from lo_core.db.base import CONTROL_SCHEMA, ControlBase
from lo_core.db.mixins import TimestampMixin, UUIDPrimaryKey

# What is being measured. Deliberately a small closed set rather than arbitrary
# SQL: a rule that can run any query is a rule that can table-scan production
# from a cron job, and "alert on anything" is a feature nobody actually asks for.
AlertMetric = Literal["error_rate", "p95_latency_ms", "p99_latency_ms", "cost_usd", "trace_count"]
ALERT_METRICS: tuple[str, ...] = (
    "error_rate",
    "p95_latency_ms",
    "p99_latency_ms",
    "cost_usd",
    "trace_count",
)

AlertComparison = Literal["above", "below"]
ALERT_COMPARISONS: tuple[str, ...] = ("above", "below")


class AlertRule(UUIDPrimaryKey, TimestampMixin, ControlBase):
    __tablename__ = "alert_rules"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_alert_rules_project_id_name"),
        CheckConstraint("metric IN " + str(ALERT_METRICS), name="metric_valid"),
        CheckConstraint("comparison IN " + str(ALERT_COMPARISONS), name="comparison_valid"),
        CheckConstraint("window_seconds > 0", name="window_positive"),
        CheckConstraint("cooldown_seconds >= 0", name="cooldown_non_negative"),
        {"schema": CONTROL_SCHEMA},
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    comparison: Mapped[str] = mapped_column(String(8), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)

    # How far back the metric is measured. A five-minute window smooths the
    # spikes that a one-minute window would page on; too long and a real outage
    # takes too long to surface.
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="300")

    # Minimum traces in the window before the rule may fire. Without this, a
    # single errored request at 3am is a 100% error rate and wakes someone up.
    min_sample_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")

    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="900")

    webhook_url: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The value that most recently tripped it — shown in the UI so a rule that
    # keeps firing is diagnosable without going to the logs.
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Counts delivery failures. A webhook pointing at a decommissioned endpoint
    # is a silent alerting outage otherwise, which is the worst kind.
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    def __repr__(self) -> str:
        return f"<AlertRule {self.name} {self.metric} {self.comparison} {self.threshold}>"
