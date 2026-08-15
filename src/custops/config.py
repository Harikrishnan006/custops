"""Application configuration.

Every configurable value in the system is declared in this module and loaded
from the environment through Pydantic Settings. Nothing else in the codebase
reads ``os.environ``, so the entire configuration surface is one file wide —
which is what makes secret handling auditable (BUILD_SPEC §17, Rule 16).

Naming convention:

* Infrastructure settings use the conventional prefixes the surrounding
  ecosystem already expects (``POSTGRES_*``, ``REDIS_*``, ``LOG_*``) so the same
  variables work for Docker Compose, a native install, and CI without
  translation.
* Application settings are namespaced ``CUSTOPS_*`` to avoid colliding with
  unrelated variables in a shared environment.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

from custops import __version__

Environment = Literal["local", "test", "staging", "production"]
LogFormat = Literal["json", "console"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_ENV_FILE = ".env"
_MASK = "***"


class PostgresSettings(BaseSettings):
    """Connection settings for the system of record.

    PostgreSQL is the source of truth for the whole platform and also hosts the
    pgvector extension used for knowledge retrieval (decision D4: one database
    service, not two).
    """

    model_config = SettingsConfigDict(
        env_prefix="POSTGRES_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    user: str = "custops"
    password: SecretStr = SecretStr("custops")
    db: str = "custops"

    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=5, ge=0)
    echo_sql: bool = False

    def dsn(self, *, driver: str = "asyncpg", reveal_password: bool = True) -> str:
        """Build a SQLAlchemy URL.

        Credentials are percent-encoded so that passwords containing reserved
        characters (``@``, ``/``, ``:``) produce a valid URL rather than a
        silently misparsed one.
        """
        password = quote(self.password.get_secret_value(), safe="") if reveal_password else _MASK
        user = quote(self.user, safe="")
        return f"postgresql+{driver}://{user}:{password}@{self.host}:{self.port}/{self.db}"

    def libpq_dsn(self, *, reveal_password: bool = True) -> str:
        """Build a driver-less URL for libpq clients.

        SQLAlchemy URLs carry a ``+driver`` suffix; libpq does not understand
        one. The LangGraph Postgres checkpointer speaks psycopg 3 directly, so
        it needs this form. Kept as an explicit method rather than letting
        callers strip the suffix from ``dsn()``, because that string surgery
        would silently produce an invalid URL the moment the driver name
        changes.
        """
        password = quote(self.password.get_secret_value(), safe="") if reveal_password else _MASK
        user = quote(self.user, safe="")
        return f"postgresql://{user}:{password}@{self.host}:{self.port}/{self.db}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def safe_dsn(self) -> str:
        """A DSN with the password masked — the only form safe to log."""
        return self.dsn(reveal_password=False)


class RedisSettings(BaseSettings):
    """Connection settings for Redis.

    Redis' architectural role is deliberately still open — see
    docs/decisions/ADR-003-redis-role.md. Phase 1 uses it only as a liveness
    dependency of ``/health``; it must earn a concrete job or be removed.
    """

    model_config = SettingsConfigDict(
        env_prefix="REDIS_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "localhost"
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0)
    password: SecretStr | None = None
    socket_timeout_seconds: float = Field(default=2.0, gt=0)
    socket_connect_timeout_seconds: float = Field(default=2.0, gt=0)

    def dsn(self, *, reveal_password: bool = True) -> str:
        credentials = ""
        if self.password is not None and self.password.get_secret_value():
            secret = quote(self.password.get_secret_value(), safe="") if reveal_password else _MASK
            credentials = f":{secret}@"
        return f"redis://{credentials}{self.host}:{self.port}/{self.db}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def safe_dsn(self) -> str:
        return self.dsn(reveal_password=False)


class ProviderSettings(BaseSettings):
    """Model provider selection and credentials (decision D11).

    Provider choice is configuration; no business logic names a vendor.

    ``embedding_dimensions`` is load-bearing beyond configuration: the stored
    vector column is fixed at that width by migration, and a query embedded at a
    different width cannot be compared to it. Changing this value is a schema
    migration and a re-index of the whole corpus, not a restart.
    """

    model_config = SettingsConfigDict(
        env_prefix="PROVIDER_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 'deterministic' is a test stand-in and is rejected outside local/test —
    # see providers.registry.
    embedding_provider: str = "deterministic"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, ge=1, le=4096)

    chat_provider: str = "anthropic"
    chat_model: str = "claude-opus-5"

    openai_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    google_api_key: SecretStr = SecretStr("")


class PortalSettings(BaseSettings):
    """The legacy provisioning portal (§11, D8).

    Its own credentials, deliberately separate from anything else: a legacy
    system that shares the application's identity store is not a separate system,
    and the integration problem this portal exists to demonstrate would vanish.

    ``base_url`` is where Playwright points. ``headless`` is False only for
    watching a run locally; automation always runs headless.
    """

    model_config = SettingsConfigDict(
        env_prefix="PORTAL_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = Field(default=8100, ge=1, le=65535)
    base_url: str = "http://127.0.0.1:8100"

    username: str = "provisioning.operator"
    password: SecretStr = SecretStr("change-me-locally")

    headless: bool = True
    # A form submission that hangs must fail the step rather than the workflow.
    timeout_ms: int = Field(default=15000, ge=1000, le=120000)


class A2ASettings(BaseSettings):
    """The Billing Specialist, reached over A2A (§9, D6).

    Its own host and port because it is its own process. The orchestrator holds
    a *URL*, not an import — which is the whole point of the decision: the
    specialist could be rewritten in another language or moved to another host
    without the orchestrator changing.

    ``enabled`` defaults to False. The specialist is a second opinion, and a
    platform whose main workflow silently depends on an optional process being
    up is not one that degrades gracefully — it is one that has an undeclared
    hard dependency. Turning it on is a deliberate act.

    ``timeout_seconds`` is short on purpose. A slow second opinion is worth less
    than a prompt local answer; the workflow must not stall behind it.
    """

    model_config = SettingsConfigDict(
        env_prefix="A2A_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = False

    host: str = "127.0.0.1"
    port: int = Field(default=8200, ge=1, le=65535)
    billing_specialist_url: str = "http://127.0.0.1:8200"

    timeout_seconds: float = Field(default=5.0, gt=0, le=60)


class LoggingSettings(BaseSettings):
    """Structured logging configuration (BUILD_SPEC §16)."""

    model_config = SettingsConfigDict(
        env_prefix="LOG_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    level: LogLevel = "INFO"
    format: LogFormat = "json"


class Settings(BaseSettings):
    """Root settings object. Compose sub-settings; never read env directly."""

    model_config = SettingsConfigDict(
        env_prefix="CUSTOPS_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    service_name: str = "custops-api"
    environment: Environment = "local"
    debug: bool = False

    # Per-dependency ceiling for /health probes. A health endpoint that can hang
    # is worse than no health endpoint: orchestrators read a timeout as "alive".
    health_probe_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    portal: PortalSettings = Field(default_factory=PortalSettings)
    a2a: A2ASettings = Field(default_factory=A2ASettings)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def version(self) -> str:
        """Package version, read from installed metadata to avoid drift."""
        return __version__


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached because settings are immutable for the life of the process and are
    read on every request path.
    """
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache. Intended for tests that manipulate the env."""
    get_settings.cache_clear()
