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

from typing import Any

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


# SQLAlchemy accepts `__table_args__` as either a dict of table keyword
# arguments or a tuple of constraints ending in that dict. The abstract bases
# below supply the dict form, but concrete tables that declare constraints must
# override with the tuple form — so the attribute is annotated loosely here.
# Without this, mypy infers `dict[str, str]` from the bases and rejects every
# subclass that adds a UniqueConstraint.
TableArgs = Any


class ControlBase(Base):
    """Mixin marker for tables in the `control` schema."""

    __abstract__ = True
    __table_args__: TableArgs = {"schema": CONTROL_SCHEMA}


class TelemetryBase(Base):
    """Mixin marker for tables in the `telemetry` schema."""

    __abstract__ = True
    __table_args__: TableArgs = {"schema": TELEMETRY_SCHEMA}
