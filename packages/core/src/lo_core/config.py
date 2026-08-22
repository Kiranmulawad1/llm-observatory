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

    # The platform operator credential.
    #
    # Distinct from a project API key, and deliberately so. A project key is
    # issued *to* a tenant and is scoped to their data; this one belongs to
    # whoever runs the platform, creates projects, and mints those keys. It is
    # what the dashboard's server side holds — which is why the browser never
    # sees it (ADR 0008).
    #
    # Generated into .env by `make bootstrap` so local development is not a
    # choice between "no auth" and "friction". Required in every deployed
    # environment: without it, project creation would have no gate at all.
    admin_token: SecretStr | None = Field(
        default=None,
        description="Platform operator token. Creates projects and issues project keys.",
    )

    # Where the worker exposes /metrics.
    #
    # The API serves its own on the port it already has; the worker has no HTTP
    # server, so it starts one just for this. 9464 is the OpenTelemetry
    # Prometheus exporter's conventional port, which makes the intent obvious
    # to anyone reading a Service definition.
    metrics_port: int = 9464

    # --- Provider credentials (optional until Phase 3) --------------------
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    # The setting that turns one adapter into support for a dozen vendors.
    #
    # Groq, Together, OpenRouter, Fireworks, vLLM and Ollama all serve the
    # OpenAI Chat Completions API, so which one you are talking to is a URL
    # rather than a code path. Unset means OpenAI itself.
    #
    #   https://api.groq.com/openai/v1      https://openrouter.ai/api/v1
    #   http://localhost:11434/v1  (Ollama) http://localhost:8000/v1  (vLLM)
    openai_base_url: str | None = Field(
        default=None,
        description="Base URL for any OpenAI-compatible endpoint. Unset means OpenAI.",
    )

    @field_validator("anthropic_api_key", "openai_api_key", "admin_token", mode="before")
    @classmethod
    def _blank_secret_is_unset(cls, v: object) -> object:
        """An empty string means "not configured", not "configured as empty".

        `.env.example` ships these keys present but blank, so a fresh checkout
        loads `SecretStr("")` rather than `None`. Anything checking `is None`
        then believes a credential exists and passes "" to a vendor SDK, which
        fails with the SDK's own error instead of the actionable one written
        here — and it does so for *every* new user, since blank is the default
        state of the file.

        Normalising at the boundary fixes it once, for every consumer, including
        ones added later.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

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
        if self.admin_token is None:
            problems.append(
                "LO_ADMIN_TOKEN is unset — project creation and key issuance would be ungated"
            )
        elif len(self.admin_token.get_secret_value()) < 32:
            problems.append("LO_ADMIN_TOKEN is shorter than 32 characters")
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
