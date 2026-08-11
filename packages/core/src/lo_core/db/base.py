"""Declarative base and schema layout.

Two logical schemas, one physical database (for now):

  control    — projects, api keys, prompts, datasets, eval runs, review queue.
               Relational, transactional, low write volume, needs FK integrity.

  telemetry  — traces and spans. Append-only, high-cardinality time-series,
               orders of magnitude more rows. Becomes a TimescaleDB hypertable.

Keeping them apart from day one means moving `telemetry` to its own instance
(or to ClickHouse) later is a connection-string change plus a repository swap,
not a schema rewrite. It also lets the two get different backup, retention and
resource policies, which is the real reason they cannot share a schema forever.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

CONTROL_SCHEMA = "control"
TELEMETRY_SCHEMA = "telemetry"

# Deterministic constraint names. Without this, Postgres auto-generates names
# and Alembic cannot emit a reversible DROP CONSTRAINT — you end up hand-editing
# migrations forever. Set it before the first migration or it is painful later.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = metadata


class ControlBase(Base):
    """Mixin marker for tables in the `control` schema."""

    __abstract__ = True
    __table_args__ = {"schema": CONTROL_SCHEMA}


class TelemetryBase(Base):
    """Mixin marker for tables in the `telemetry` schema."""

    __abstract__ = True
    __table_args__ = {"schema": TELEMETRY_SCHEMA}
