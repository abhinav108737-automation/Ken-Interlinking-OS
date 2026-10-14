"""Structured logging.

FROZEN CONTRACT (the processor chain and the reserved field names).

TRD.md section 2 requires structured application logs and says OpenTelemetry is
"supported", not required. Phase 0 therefore ships no OTel SDK but DOES reserve
the correlation field names ``request_id``, ``trace_id`` and ``span_id`` in the
processor chain, so Phase 7 can start populating them without touching every
call site or reformatting historical logs.

Two rules that exist to prevent real incidents:

1. stdlib ``logging`` is bridged into structlog, so uvicorn's and SQLAlchemy's
   output is structured too. Without this, half the logs in production are
   JSON and half are not.
2. ``log_secrets`` has no setting and no flag. Credentials are never logged.
   ``Settings.public_settings()`` exists precisely so that configuration can be
   surfaced without surfacing secrets.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

# Correlation context, populated by the request-id middleware and the worker.
# Reserved now so Phase 7 can map these onto OpenTelemetry without a migration
# of log consumers.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
span_id_var: ContextVar[str | None] = ContextVar("span_id", default=None)

_CONFIGURED = False


def _add_correlation_ids(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Attach whichever correlation ids are currently in scope."""
    for key, var in (
        ("request_id", request_id_var),
        ("trace_id", trace_id_var),
        ("span_id", span_id_var),
    ):
        value = var.get()
        if value is not None:
            event_dict[key] = value
    return event_dict


def configure_logging(*, level: str = "INFO", fmt: str = "console") -> None:
    """Configure structlog and route stdlib logging through it.

    Idempotent: calling it twice (API lifespan plus a test fixture, say) is
    harmless, which matters because both the API and the worker configure
    logging at startup.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_correlation_ids,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: structlog.types.Processor
    final_processors: list[structlog.types.Processor] = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
    ]
    if fmt == "json":
        renderer = structlog.processors.JSONRenderer()
        # JSON needs the traceback serialised into a field; ConsoleRenderer
        # formats exc_info itself and warns if this processor runs first.
        final_processors.append(structlog.processors.format_exc_info)
    else:
        # Pin plain tracebacks (QA L7): a "rich" formatter -- picked automatically if that
        # package ever arrives transitively -- renders frame LOCALS, which can include a
        # plaintext password or token.
        renderer = structlog.dev.ConsoleRenderer(
            colors=sys.stderr.isatty(),
            exception_formatter=structlog.dev.plain_traceback,
        )
    final_processors.append(renderer)

    # Rendering happens in exactly ONE place: the stdlib handler's
    # ProcessorFormatter below. The structlog chain therefore ends with
    # wrap_for_formatter, which hands the event dict over unrendered. Ending it
    # with a renderer instead renders every structlog event twice -- in JSON
    # mode the whole first rendering became the "event" string, so no field was
    # queryable. Regression-tested in tests/unit/test_logging.py.
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # The single renderer, shared by structlog events and bridged stdlib
    # records (uvicorn, sqlalchemy, alembic). foreign_pre_chain gives stdlib
    # records the same level/timestamp/correlation fields structlog events get.
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=final_processors,
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own handlers; route its server/error logs through the
    # root handler so they are structured like everything else.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # uvicorn's access log is always silenced. The API's request-id middleware
    # already writes one richer access line per request (request_id,
    # duration_ms, status), so uvicorn's would be a duplicate. This is done here
    # rather than via `--no-access-log` because this function runs in the app
    # lifespan, AFTER uvicorn has configured logging -- setting propagate=True
    # here previously re-enabled the access log even when the flag was passed.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False

    _CONFIGURED = True


def reset_logging() -> None:
    """Allow reconfiguration. Intended for tests only."""
    global _CONFIGURED
    _CONFIGURED = False


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. The standard way to obtain a logger anywhere."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
