"""Request-id middleware: echo, strict validation, access log."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ken_api.middleware import request_id as request_id_module
from ken_api.middleware.request_id import resolve_request_id
from ken_core.logging import request_id_var
from tests.conftest import make_app


class RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.records.append((event, kw))


@pytest.fixture
def access_log(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    recorder = RecordingLogger()
    monkeypatch.setattr(request_id_module, "logger", recorder)
    return recorder


def _is_uuid4(value: str) -> bool:
    try:
        return uuid.UUID(value).version == 4
    except ValueError:
        return False


def test_valid_inbound_request_id_is_echoed() -> None:
    response = TestClient(make_app()).get("/health", headers={"X-Request-ID": "abc-123_x.y"})
    assert response.headers["x-request-id"] == "abc-123_x.y"


def test_missing_request_id_is_generated() -> None:
    response = TestClient(make_app()).get("/health")
    assert _is_uuid4(response.headers["x-request-id"])


@pytest.mark.parametrize(
    "malicious",
    ["a" * 65, "id with spaces", '{"admin":true}', "<script>", "id;rm -rf /", "x\ty", ""],
)
def test_malicious_inbound_request_id_is_replaced(malicious: str) -> None:
    response = TestClient(make_app()).get("/health", headers={"X-Request-ID": malicious})
    echoed = response.headers["x-request-id"]
    assert echoed != malicious
    assert _is_uuid4(echoed)


@pytest.mark.parametrize("value", ["abc\n", "abc\nfake=log", "abc\r\n", "\x1b[31mred", "é"])
def test_control_characters_rejected(value: str) -> None:
    # Values HTTP clients refuse to send are still validated at the function level.
    assert _is_uuid4(resolve_request_id(value))


def test_one_access_log_line_per_request(access_log: RecordingLogger) -> None:
    client = TestClient(make_app())
    response = client.get("/health", headers={"X-Request-ID": "trace-1"})

    assert len(access_log.records) == 1
    event, fields = access_log.records[0]
    assert event == "http_request"
    assert fields["method"] == "GET"
    assert fields["path"] == "/health"
    assert fields["status"] == 200
    assert fields["request_id"] == "trace-1"
    assert fields["duration_ms"] >= 0
    assert response.status_code == 200


def test_access_log_escapes_control_characters_in_path(access_log: RecordingLogger) -> None:
    TestClient(make_app()).get("/nope%0Afake_event=1")
    _, fields = access_log.records[0]
    assert "\n" not in fields["path"]
    assert fields["status"] == 404


def test_request_id_context_is_reset_after_request() -> None:
    TestClient(make_app()).get("/health", headers={"X-Request-ID": "scoped"})
    assert request_id_var.get() is None
