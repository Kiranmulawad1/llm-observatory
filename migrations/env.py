"""Alembic runtime configuration.

Three things here are load-bearing and worth reading before editing:

1. **The URL comes from `lo_core.config`, never from `alembic.ini`.** Migrations
   connect to exactly the database the application connects to, and no password
   is ever committed. The async driver is stripped for the offline/SQL path
   (see `_sync_url`) because that mode never opens a connection at all.

2. **`include_schemas=True` plus an explicit allow-list.** The models live in the
   `control` and `telemetry` schemas rather than `public`, so autogenerate has to
   look beyond the default schema. But TimescaleDB installs half a dozen internal
   schemas (`_timescaledb_catalog`, `timescaledb_information`, …) into the same
   database, and an unfiltered autogenerate would cheerfully emit `DROP TABLE`
   for every one of them. `include_name` is the guard.

3. **The version table lives in `control`.** Leaving it in `public` would mean the
   one table recording migration state sits outside both managed schemas, which
   breaks a `telemetry`-only restore and makes schema-scoped grants awkward.
"""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from logging.config import fileConfig
from typing import Literal

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.schema import CreateSchema

from lo_core.config import get_settings
from lo_core.db.base import CONTROL_SCHEMA, TELEMETRY_SCHEMA, metadata

# Importing the models package registers every mapped class against the shared
# MetaData. Without this import autogenerate sees an empty schema and helpfully
# proposes dropping the entire database.
import lo_core.db.models  # noqa: F401  isort:skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata

MANAGED_SCHEMAS = frozenset({CONTROL_SCHEMA, TELEMETRY_SCHEMA})

# Options shared by the online and offline paths. Keeping them in one dict is
# what stops `alembic upgrade --sql` from drifting away from a live upgrade and
# generating subtly different DDL.
COMMON_OPTIONS: dict[str, object] = {
    "target_metadata": target_metadata,
    "include_schemas": True,
    "version_table": "alembic_version",
    "version_table_schema": CONTROL_SCHEMA,
    # Detect server-side type and default changes explicitly. Without these, a
    # varchar(50) -> varchar(100) widening is silently omitted from autogenerate.
    "compare_type": True,
    "compare_server_default": True,
}


def _sync_url() -> str:
    """The application DSN, downgraded to the sync driver name.

    Only used by offline mode, which renders SQL from the dialect without ever
    connecting, and therefore must not name an async driver.
    """
    return str(get_settings().database_url).replace("postgresql+asyncpg://", "postgresql://")


# Alembic's own aliases for the include_name callback signature.
NameType = Literal[
    "schema",
    "table",
    "column",
    "index",
    "unique_constraint",
    "foreign_key_constraint",
    "check_constraint",
]
ParentNames = MutableMapping[
    Literal["schema_name", "schema_qualified_table_name", "table_name"], str | None
]


def include_name(name: str | None, type_: NameType, parent_names: ParentNames) -> bool:
    """Restrict autogenerate to the schemas this project owns.

    Returning False for a schema makes Alembic treat it as out of scope entirely,
    rather than as "present in the database but missing from the models" — which
    is what produces spurious DROP statements.
    """
    if type_ == "schema":
        return name in MANAGED_SCHEMAS
    if type_ == "table":
        return parent_names.get("schema_name") in MANAGED_SCHEMAS
    if type_ == "index":
        # TimescaleDB creates its own descending index on a hypertable's time
        # column (`<table>_started_at_idx`) when the table is converted. It is
        # not in our SQLAlchemy metadata, so autogenerate sees it as an index
        # the database has and the models do not — and proposes dropping it on
        # every single migration. Dropping it would gut hypertable query
        # performance, so it is excluded from comparison entirely.
        if name is not None and name.endswith("_started_at_idx"):
            return parent_names.get("schema_name") != TELEMETRY_SCHEMA
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a database connection (`alembic upgrade --sql`).

    This is what feeds a reviewed-SQL deployment process, where a human reads the
    statements before they are applied to production.
    """
    context.configure(
        url=_sync_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
        **COMMON_OPTIONS,  # type: ignore[arg-type]
    )
    with context.begin_transaction():
        context.run_migrations()


def ensure_schemas(connection: Connection) -> None:
    """Create the managed schemas if they are missing.

    This cannot live in the first migration, because Alembic creates its version
    table — which we place in `control` — *before* it runs any migration. On a
    virgin database that ordering would fail on the version table itself.

    It also cannot rely on infra/docker/postgres-init/01-init.sql. That script is
    a docker-entrypoint convenience that runs only for a fresh local volume; a
    managed Cloud SQL instance and an ephemeral CI container never execute it.
    Namespaces are a prerequisite for migrating at all, so the migration runner
    owns them, and `alembic upgrade head` works against any empty database.
    """
    for schema in sorted(MANAGED_SCHEMAS):
        connection.execute(CreateSchema(schema, if_not_exists=True))


def do_run_migrations(connection: Connection) -> None:
    ensure_schemas(connection)
    context.configure(
        connection=connection,
        include_name=include_name,
        **COMMON_OPTIONS,  # type: ignore[arg-type]
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Apply migrations against a live database.

    An async engine is used purely so this module shares the asyncpg driver the
    rest of the project depends on; Alembic's own work still happens on a sync
    connection handed over by `run_sync`.
    """
    section: dict[str, str] = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = str(get_settings().database_url)

    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
            # The explicit commit is required, and its absence fails silently.
            #
            # `ensure_schemas` issues DDL, which implicitly opens a transaction
            # on this connection. Alembic's `begin_transaction()` then sees a
            # connection that is already mid-transaction and returns a no-op
            # context manager rather than taking ownership — so nothing commits,
            # `connect()` rolls back on exit, and Alembic still cheerfully logs
            # "Running upgrade ... done" against a database where no table was
            # actually created.
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
