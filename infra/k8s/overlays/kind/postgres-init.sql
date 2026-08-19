-- Runs exactly once, when the postgres data volume is first created.
-- Scope: extensions and schemas only. Table DDL belongs to Alembic so that
-- local, CI and production converge on the same migration history.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Control plane: prompts, datasets, eval runs, projects/API keys, review queue.
-- Relational, transactional, low volume, needs foreign-key integrity.
CREATE SCHEMA IF NOT EXISTS control;

-- Telemetry: traces and spans. Append-only, high-cardinality time-series.
-- Isolated from day one so it can move to its own instance (or ClickHouse)
-- without a schema rewrite.
CREATE SCHEMA IF NOT EXISTS telemetry;
