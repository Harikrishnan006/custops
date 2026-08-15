"""Settings loading (Phase 1 definition of done, item 5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from custops import __version__
from custops.config import (
    LoggingSettings,
    PostgresSettings,
    RedisSettings,
    Settings,
    get_settings,
    reset_settings_cache,
)


@pytest.mark.usefixtures("clean_env")
def test_defaults_apply_when_environment_is_empty() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "local"
    assert settings.debug is False
    assert settings.service_name == "custops-api"
    assert settings.postgres.host == "localhost"
    assert settings.postgres.port == 5432
    assert settings.redis.port == 6379
    assert settings.logging.level == "INFO"
    assert settings.logging.format == "json"


@pytest.mark.usefixtures("clean_env")
def test_version_is_read_from_package_metadata() -> None:
    # Sourced from installed metadata rather than a literal, so the version
    # cannot drift from pyproject.toml.
    assert Settings(_env_file=None).version == __version__


@pytest.mark.usefixtures("clean_env")
def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOPS_ENVIRONMENT", "staging")
    monkeypatch.setenv("CUSTOPS_DEBUG", "true")
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PORT", "6000")
    monkeypatch.setenv("REDIS_HOST", "cache.internal")
    monkeypatch.setenv("LOG_FORMAT", "console")

    settings = Settings(_env_file=None)

    assert settings.environment == "staging"
    assert settings.debug is True
    assert settings.postgres.host == "db.internal"
    assert settings.postgres.port == 6000
    assert settings.redis.host == "cache.internal"
    assert settings.logging.format == "console"


@pytest.mark.usefixtures("clean_env")
@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("CUSTOPS_ENVIRONMENT", "banana"),
        ("CUSTOPS_HEALTH_PROBE_TIMEOUT_SECONDS", "0"),
        ("POSTGRES_PORT", "70000"),
        ("LOG_LEVEL", "TRACE"),
        ("LOG_FORMAT", "xml"),
    ],
)
def test_invalid_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    # Configuration errors must surface at startup, not as confusing runtime
    # behaviour three layers deeper.
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.usefixtures("clean_env")
def test_postgres_dsn_percent_encodes_credentials() -> None:
    settings = PostgresSettings(
        _env_file=None,
        host="db.internal",
        port=5432,
        user="cust ops",
        password="p@ss:w/rd",
        db="custops",
    )

    dsn = settings.dsn()

    assert dsn == "postgresql+asyncpg://cust%20ops:p%40ss%3Aw%2Frd@db.internal:5432/custops"


@pytest.mark.usefixtures("clean_env")
def test_safe_dsn_never_reveals_the_password() -> None:
    settings = PostgresSettings(_env_file=None, password="super-secret")

    assert "super-secret" not in settings.safe_dsn
    assert "***" in settings.safe_dsn
    # repr of a SecretStr must not leak either — this is the value that ends up
    # in tracebacks and log records.
    assert "super-secret" not in repr(settings)


@pytest.mark.usefixtures("clean_env")
def test_redis_dsn_with_and_without_password() -> None:
    without = RedisSettings(_env_file=None)
    assert without.dsn() == "redis://localhost:6379/0"

    with_password = RedisSettings(_env_file=None, password="s3cret", db=3)
    assert with_password.dsn() == "redis://:s3cret@localhost:6379/3"
    assert "s3cret" not in with_password.safe_dsn


@pytest.mark.usefixtures("clean_env")
def test_logging_settings_accept_only_known_formats() -> None:
    assert LoggingSettings(_env_file=None, format="console").format == "console"

    with pytest.raises(ValidationError):
        LoggingSettings(_env_file=None, format="logfmt")


@pytest.mark.usefixtures("clean_env")
def test_get_settings_is_cached_and_resettable() -> None:
    reset_settings_cache()
    first = get_settings()
    assert get_settings() is first

    reset_settings_cache()
    assert get_settings() is not first
