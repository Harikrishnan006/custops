"""Structured logging.

One log pipeline for the whole process. Application logs (structlog) and
third-party logs (uvicorn, sqlalchemy, alembic) are rendered by the *same*
formatter, so every line in the stream has the same shape and carries the same
correlation fields. A trace that only covers our own code is not a trace.

Every record carries:

* ``timestamp`` (ISO-8601, UTC), ``level``, ``event``, ``logger``
* ``execution_id`` and ``request_id`` — always present, ``null`` when unbound
  (BUILD_SPEC §16 and Phase 1 definition of done item 6)
* ``service``, ``version``, ``environment``

Never log chain-of-thought (Rule 18). Log structured decisions, evidence
references, tool input/output where safe, and concise rationale summaries.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from custops.config import Settings
from custops.observability.context import (
    EXECUTION_ID_KEY,
    REQUEST_ID_KEY,
    get_execution_id,
    get_request_id,
)

# Third-party loggers that must flow through our formatter rather than printing
# their own format. uvicorn installs handlers on these at startup; we take them
# over so the output stream stays homogeneous.
_MANAGED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine", "alembic")


def _inject_correlation_ids(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Attach the ambient correlation ids to every record.

    ``setdefault`` rather than assignment: an explicit value passed at the call
    site (``log.info("x", execution_id=...)``) wins over the ambient one.
    """
    event_dict.setdefault(EXECUTION_ID_KEY, get_execution_id())
    event_dict.setdefault(REQUEST_ID_KEY, get_request_id())
    return event_dict


def _service_metadata_processor(service: str, version: str, environment: str) -> Processor:
    """Build a processor stamping static service identity onto every record."""

    def processor(
        _logger: WrappedLogger,
        _method_name: str,
        event_dict: EventDict,
    ) -> EventDict:
        event_dict.setdefault("service", service)
        event_dict.setdefault("version", version)
        event_dict.setdefault("environment", environment)
        return event_dict

    return processor


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the stdlib root logger. Idempotent.

    Safe to call more than once (tests reconfigure between cases), which is why
    ``cache_logger_on_first_use`` is off: cached loggers would keep the previous
    configuration and silently ignore the new one.
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _inject_correlation_ids,
        _service_metadata_processor(
            service=settings.service_name,
            version=settings.version,
            environment=settings.environment,
        ),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            # Hand off to the stdlib formatter instead of rendering here, so our
            # records and foreign records converge on one renderer.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    render_processors: list[Processor] = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
    ]
    if settings.logging.format == "json":
        # ConsoleRenderer formats exceptions itself; JSONRenderer needs exc_info
        # flattened into a string first.
        render_processors.append(structlog.processors.format_exc_info)
        render_processors.append(structlog.processors.JSONRenderer())
    else:
        render_processors.append(structlog.dev.ConsoleRenderer(colors=False))

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=render_processors,
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.logging.level)

    for name in _MANAGED_LOGGERS:
        managed = logging.getLogger(name)
        managed.handlers.clear()
        managed.propagate = True

    # uvicorn emits its access log from the protocol layer, outside the ASGI
    # application and therefore outside the request context, so those lines
    # always carry a null request_id. RequestContextMiddleware emits a correlated
    # equivalent (with duration); disabling uvicorn's avoids two lines per
    # request that disagree about what they know.
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Use ``__name__`` at the call site."""
    return structlog.stdlib.get_logger(name)
