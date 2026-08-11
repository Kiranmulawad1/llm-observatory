"""Configuration guardrails.

These tests exist because both failure modes are silent in production: a sync
driver quietly serialises the whole event loop, and a leftover dev pepper quietly
weakens every API key. Both should be startup crashes, and this pins that.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lo_core.config import INSECURE_DEV_PEPPER, Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://lo:pw@localhost:5432/db",
        "redis_url": "redis://localhost:6379/0",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_rejects_sync_postgres_driver() -> None:
    with pytest.raises(ValidationError, match="asyncpg"):
        _settings(database_url="postgresql://lo:pw@localhost:5432/db")


def test_accepts_asyncpg_driver() -> None:
    assert _settings().database_url.scheme == "postgresql+asyncpg"


def test_secrets_are_not_rendered_in_repr() -> None:
    s = _settings(api_key_pepper="super-secret-value")
    assert "super-secret-value" not in repr(s)
    assert s.api_key_pepper.get_secret_value() == "super-secret-value"


def test_local_environment_tolerates_dev_defaults() -> None:
    _settings(environment="local").assert_production_safe()  # must not raise


def test_deployed_environment_rejects_default_pepper() -> None:
    # Passed explicitly: the test harness exports LO_API_KEY_PEPPER, and
    # pydantic-settings ranks the environment above the field default, so
    # omitting it here would silently test a *safe* value and always pass.
    s = _settings(environment="prod", api_key_pepper=INSECURE_DEV_PEPPER)
    with pytest.raises(RuntimeError, match="development default"):
        s.assert_production_safe()


def test_deployed_environment_rejects_sql_echo() -> None:
    s = _settings(environment="staging", api_key_pepper="real-secret", database_echo=True)
    with pytest.raises(RuntimeError, match="query parameters"):
        s.assert_production_safe()
