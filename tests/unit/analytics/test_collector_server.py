"""Running the collector: binding guards and listener verification (ADR-0015 §1 / §12.9.5).

Loopback IP literal, port 0, NEVER 8765, and a URL that is only handed out once an HTTP GET
of the health path on that exact address answered with this instance's nonce.
"""

from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest

from ken_services.analytics.collector import server as collector_server
from ken_services.analytics.collector.app import HEALTH_PATH
from ken_services.analytics.collector.server import (
    CollectorError,
    CollectorServer,
    serve_collector,
)
from ken_services.deployment.sim_site import FORBIDDEN_PORTS
from tests.unit.analytics._collector_support import (
    build_app,
    config,
    live_plan,
    payload,
)


def test_the_user_dashboard_port_is_forbidden() -> None:
    assert frozenset({0, 8765}) == FORBIDDEN_PORTS


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.168.1.10", "::", "localhost", "example.test"],  # noqa: S104
)
def test_a_non_loopback_bind_needs_allow_remote(host: str) -> None:
    app, _ = build_app(collector_config=config(host=host))
    with pytest.raises(CollectorError):
        CollectorServer(app)


def test_a_non_loopback_bind_is_allowed_once_approved() -> None:
    app, _ = build_app(collector_config=config(host="192.168.1.10", allow_remote=True))
    assert CollectorServer(app).port is None  # constructed, not bound


class _FakeSocket:
    """Pretends the OS assigned a forbidden port."""

    instances: list[_FakeSocket] = []  # noqa: RUF012 - test bookkeeping

    def __init__(self, family: int, kind: int) -> None:
        self.closed = False
        _FakeSocket.instances.append(self)

    def bind(self, address: tuple[str, int]) -> None:
        assert address[1] == 0  # never a fixed port

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", 8765)

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("port", [8765, 0])
def test_bind_refuses_forbidden_ports(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    _FakeSocket.instances.clear()
    monkeypatch.setattr(socket, "socket", _FakeSocket)
    monkeypatch.setattr(_FakeSocket, "getsockname", lambda self: ("127.0.0.1", port))
    with pytest.raises(CollectorError, match=str(port)):
        collector_server._bind(collector_server._address_of("127.0.0.1", allow_remote=False))
    assert _FakeSocket.instances and _FakeSocket.instances[0].closed


def test_bind_refuses_another_address_than_the_requested_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSocket.instances.clear()
    monkeypatch.setattr(socket, "socket", _FakeSocket)
    monkeypatch.setattr(_FakeSocket, "getsockname", lambda self: ("127.0.0.5", 4100))
    with pytest.raises(CollectorError):
        collector_server._bind(collector_server._address_of("127.0.0.1", allow_remote=False))
    assert _FakeSocket.instances[0].closed


async def test_the_url_is_only_returned_after_the_nonce_verified_the_listener() -> None:
    app, sink = build_app([live_plan(1)])
    async with serve_collector(app) as server:
        port = int(server.base_url.rsplit(":", 1)[1])
        assert server.base_url == f"http://127.0.0.1:{port}"
        assert port not in FORBIDDEN_PORTS
        async with httpx.AsyncClient(base_url=server.base_url, trust_env=False) as client:
            health = await client.get(HEALTH_PATH)
            assert health.status_code == 200
            assert health.text == app.nonce

            beacon = await client.get("/k.js")
            assert beacon.status_code == 200
            assert beacon.headers["content-type"] == "application/javascript; charset=utf-8"
            assert beacon.headers["x-content-type-options"] == "nosniff"

            posted = await client.post(
                "/e",
                content=payload({"t": "imp", "l": str(live_plan(1).plan_id)}),
                headers={"content-type": "text/plain", "origin": "http://127.0.0.1:4100"},
            )
            assert posted.status_code == 204
            assert posted.content == b""

            assert (await client.get("/nope")).status_code == 404
            assert (await client.get("/e")).status_code == 405
    assert len(sink.stored) == 1
    assert server.port is None


async def test_a_wrong_nonce_refuses_the_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = build_app()
    server = CollectorServer(app, startup_timeout_s=5.0)

    class _Response:
        status_code = 200
        text = "not-the-nonce"

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def get(self, url: str) -> _Response:
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _Client())
    with pytest.raises(CollectorError, match="not this collector"):
        await server.start()
    assert server.port is None


async def test_starting_twice_is_refused() -> None:
    app, _ = build_app()
    async with serve_collector(app) as server:
        with pytest.raises(CollectorError, match="already running"):
            await server.start()


def test_the_base_url_is_unavailable_before_the_start() -> None:
    app, _ = build_app()
    with pytest.raises(CollectorError):
        _ = CollectorServer(app).base_url
