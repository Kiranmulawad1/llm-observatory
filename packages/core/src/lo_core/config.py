"""Process configuration.

Settings are read from the environment exactly once, at import of `get_settings()`,
and validated by pydantic. A misconfigured process fails at startup with a readable
error rather than at the first request with a `NoneType` traceback.

Secrets are typed `SecretStr` so they cannot be accidentally logged: repr/str render
as `**********`, and reading the real value requires an explicit
`.get_secret_value()` call that shows up in code review.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "dev", "staging", "prod"]

# Sentinel value. `assert_production_safe` refuses to start any non-local
# environment that is still using it. Named rather than inlined so the check and
# the default can never drift apart.
INSECURE_DEV_PEPPER = "dev-only-insecure-pepper"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # LO_DATABASE_URL etc. Namespacing avoids collisions with whatever else
        # is in the environment of a shared Kubernetes node.
        env_prefix="LO_",
    )

    environment: Environment = "local"
    log_level: str = "INFO"
    service_name: str = "lo-api"

    # --- Data layer -------------------------------------------------------
    database_url: PostgresDsn = Field(
        description="Async SQLAlchemy DSN. Must use the asyncpg driver.",
    )
    database_pool_size: int = 10
    database_max_overflow: int = 5
    # Echo raw SQL. Never enable outside local: it logs query parameters.
    database_echo: bool = False

    redis_url: RedisDsn = Field(description="Broker for the arq job queue and rate limiter.")

    # --- Security ---------------------------------------------------------
    # Server-side pepper mixed into API key hashes. Rotating this invalidates
    # every issued key, which is the intended break-glass behaviour.
    api_key_pepper: SecretStr = Field(
        default=SecretStr(INSECURE_DEV_PEPPER),
        description="Set from a secrets manager in every non-local environment.",
    )

    # --- Provider credentials (optional until Phase 3) --------------------
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: PostgresDsn) -> PostgresDsn:
        if v.scheme != "postgresql+asyncpg":
            raise ValueError(
                f"database_url must use the 'postgresql+asyncpg' scheme, got {v.scheme!r}. "
                "The sync psycopg driver will deadlock the event loop."
            )
        return v

    @property
    def is_local(self) -> bool:
        return self.environment == "local"

    def assert_production_safe(self) -> None:
        """Fail fast on dev defaults that must never reach a deployed environment.

        Called from the API/worker lifespan so a bad rollout crash-loops visibly
        instead of running with an insecure default nobody notices.
        """
        if self.is_local:
            return
        problems: list[str] = []
        if self.api_key_pepper.get_secret_value() == INSECURE_DEV_PEPPER:
            problems.append("LO_API_KEY_PEPPER is still the built-in development default")
        if self.database_echo:
            problems.append("LO_DATABASE_ECHO=true leaks query parameters into logs")
        if problems:
            raise RuntimeError(
                f"Refusing to start in environment={self.environment}: " + "; ".join(problems)
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Override in tests via `get_settings.cache_clear()`."""
    return Settings()
