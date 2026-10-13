"""ken_core.logging: idempotent configuration and request-id correlation."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from ken_core import logging as ken_logging


@pytest.fixture
def isolated_logging() -> Iterator[None]:
    """Snapshot and restore global logging state around a test."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_structlog = structlog.get_config()
    ken_logging.reset_logging()
    try:
        yield
    finally:
        ken_logging.reset_logging()
        structlog.configure(**saved_structlog)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _json_lines(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.splitlines() if line.strip().startswith("{")]


@pytest.mark.usefixtures("isolated_logging")
def test_configure_is_idempotent() -> None:
    ken_logging.configure_logging(level="INFO", fmt="json")
    first = list(logging.getLogger().handlers)
    ken_logging.configure_logging(level="DEBUG", fmt="console")
    assert logging.getLogger().handlers == first
    assert len(first) == 1


def _line_for(lines: list[dict[str, object]], event: str) -> dict[str, object]:
    """The rendered line for ``event``, tolerant of how ``event`` is encoded."""
    return next(line for line in lines if event in str(line["event"]))


@pytest.mark.usefixtures("isolated_logging")
def test_request_id_appears_in_output(capsys: pytest.CaptureFixture[str]) -> None:
    ken_logging.configure_logging(level="INFO", fmt="json")
    token = ken_logging.request_id_var.set("req-123")
    try:
        ken_logging.get_logger("test.logging").info("hello", answer=42)
    finally:
        ken_logging.request_id_var.reset(token)
    ken_logging.get_logger("test.logging").info("after")

    lines = _json_lines(capsys.readouterr().err)
    hello = _line_for(lines, "hello")
    after = _line_for(lines, "after")
    assert hello["request_id"] == "req-123"
    assert hello["level"] == "info"
    assert "request_id" not in after


@pytest.mark.usefixtures("isolated_logging")
def test_structlog_events_are_rendered_once(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression: events were once JSON-encoded twice, so no field was queryable."""
    ken_logging.configure_logging(level="INFO", fmt="json")
    ken_logging.get_logger("test.logging").info("hello", answer=42)
    line = _line_for(_json_lines(capsys.readouterr().err), "hello")
    assert line["event"] == "hello"
    assert line["answer"] == 42


@pytest.mark.usefixtures("isolated_logging")
def test_stdlib_logging_is_bridged(capsys: pytest.CaptureFixture[str]) -> None:
    ken_logging.configure_logging(level="INFO", fmt="json")
    token = ken_logging.request_id_var.set("req-std")
    try:
        logging.getLogger("uvicorn.error").info("from stdlib")
    finally:
        ken_logging.request_id_var.reset(token)
    lines = _json_lines(capsys.readouterr().err)
    line = _line_for(lines, "from stdlib")
    assert line["request_id"] == "req-std"


@pytest.mark.usefixtures("isolated_logging")
def test_uvicorn_access_log_is_silenced(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression: the middleware writes the access line; uvicorn's would duplicate it.

    configure_logging runs after uvicorn configures logging, so it must not
    re-enable uvicorn.access (it once did, overriding --no-access-log).
    """
    ken_logging.configure_logging(level="INFO", fmt="json")
    logging.getLogger("uvicorn.access").info("GET /health 200")
    logging.getLogger("uvicorn.error").info("server started")
    events = [line["event"] for line in _json_lines(capsys.readouterr().err)]
    assert "GET /health 200" not in events
    assert "server started" in events
