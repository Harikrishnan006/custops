"""Structured logging (Phase 1 definition of done, item 6).

The claim under test is specific: every emitted record is JSON and carries an
``execution_id`` field, present-but-null when no workflow is running. That field
existing now is what makes Phase 5's workflow correlation a one-line change
instead of a cross-cutting edit.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from custops.config import LoggingSettings, Settings
from custops.observability.context import bind_context
from custops.observability.logging import configure_logging, get_logger


def _emitted_records(captured: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in captured.strip().splitlines() if line.strip()]


def test_log_records_are_json_with_correlation_fields(
    test_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(test_settings)
    get_logger("test").info("workflow_probe", customer_ref="ACME-1")

    records = _emitted_records(capsys.readouterr().out)

    assert len(records) == 1
    record = records[0]
    assert record["event"] == "workflow_probe"
    assert record["level"] == "info"
    assert record["customer_ref"] == "ACME-1"
    assert record["service"] == test_settings.service_name
    assert record["version"] == test_settings.version
    assert record["environment"] == "test"
    assert "timestamp" in record
    # Present and null: the field exists in the schema before anything sets it.
    assert "execution_id" in record
    assert record["execution_id"] is None
    assert "request_id" in record
    assert record["request_id"] is None


def test_bound_context_appears_in_records(
    test_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(test_settings)

    with bind_context(execution_id="exec-123", request_id="req-456"):
        get_logger("test").info("inside_context")
    get_logger("test").info("outside_context")

    inside, outside = _emitted_records(capsys.readouterr().out)

    assert inside["execution_id"] == "exec-123"
    assert inside["request_id"] == "req-456"
    # Reset on exit, so identifiers cannot leak into unrelated work.
    assert outside["execution_id"] is None
    assert outside["request_id"] is None


def test_third_party_logs_share_the_same_shape(
    test_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """uvicorn/sqlalchemy records must be structured too, or a trace has holes."""
    configure_logging(test_settings)

    with bind_context(execution_id="exec-789"):
        logging.getLogger("uvicorn.error").warning("connection reset by peer")

    records = _emitted_records(capsys.readouterr().out)

    assert len(records) == 1
    assert records[0]["event"] == "connection reset by peer"
    assert records[0]["level"] == "warning"
    assert records[0]["execution_id"] == "exec-789"
    assert records[0]["service"] == test_settings.service_name


def test_explicit_call_site_value_wins_over_ambient_context(
    test_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(test_settings)

    with bind_context(execution_id="ambient"):
        get_logger("test").info("explicit_override", execution_id="explicit")

    assert _emitted_records(capsys.readouterr().out)[0]["execution_id"] == "explicit"


def test_console_format_is_human_readable(
    test_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    console_settings = test_settings.model_copy(
        update={"logging": LoggingSettings(_env_file=None, level="INFO", format="console")}
    )
    configure_logging(console_settings)

    get_logger("test").info("human_readable")

    output = capsys.readouterr().out
    assert "human_readable" in output
    with pytest.raises(json.JSONDecodeError):
        json.loads(output.strip())


def test_level_filtering_is_applied(
    test_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    warning_only = test_settings.model_copy(
        update={"logging": LoggingSettings(_env_file=None, level="WARNING", format="json")}
    )
    configure_logging(warning_only)

    logger = get_logger("test")
    logger.info("suppressed")
    logger.warning("emitted")

    records = _emitted_records(capsys.readouterr().out)
    assert [record["event"] for record in records] == ["emitted"]
