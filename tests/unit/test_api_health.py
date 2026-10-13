"""GET /health: liveness with zero I/O (docs/CONTRACTS.md rule 7)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ken_contracts.health import HealthResponse, ReadinessResponse
from ken_core import logging as ken_logging
from tests.conftest import FakeCMSProvider, FakeModelProvider, make_app, make_settings

# Port 1 on loopback: nothing listens there, so any connection attempt fails fast.
BROKEN_DB_URL = "postgresql+asyncpg://nobody:nopass@127.0.0.1:1/nowhere"


class ExplodingSessionmaker:
    """Any attempt to open a session is a test failure for /health."""

    calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        type(self).calls += 1
        raise RuntimeError("database is down")


@pytest.fixture(autouse=True)
def _no_global_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    # The lifespan configures process-wide logging; keep tests hermetic.
    monkeypatch.setattr(ken_logging, "configure_logging", lambda **_: None)


def test_health_returns_200_with_broken_database() -> None:
    app = make_app(make_settings(database_url=BROKEN_DB_URL))
    app.state.sessionmaker = ExplodingSessionmaker()
    app.state.model_provider = FakeModelProvider()
    app.state.cms_provider = FakeCMSProvider()

    with TestClient(app) as client:  # runs the real lifespan
        response = client.get("/health")

    assert response.status_code == 200
    body = HealthResponse.model_validate(response.json())
    assert body.status == "ok"
    assert body.uptime_s >= 0
    assert ExplodingSessionmaker.calls == 0, "/health must not touch the database"


def test_health_with_real_engine_pointing_at_dead_database() -> None:
    """The lifespan builds a real engine against a dead port; /health is unaffected."""
    app = make_app(make_settings(database_url=BROKEN_DB_URL))
    app.state.model_provider = FakeModelProvider()
    app.state.cms_provider = FakeCMSProvider()

    with TestClient(app) as client:
        health = client.get("/health")
        ready = client.get("/ready")

    assert health.status_code == 200
    # ...while readiness reports the truth, as a 503 rather than a 500.
    assert ready.status_code == 503
    checks = {c.name: c for c in ReadinessResponse.model_validate(ready.json()).checks}
    assert checks["postgres"].status == "error"
    assert "nopass" not in ready.text


def test_health_identity() -> None:
    settings = make_settings(service_name="ken-api", environment="ci")
    response = TestClient(make_app(settings)).get("/health")
    identity = response.json()["identity"]
    assert identity["service"] == "ken-api"
    assert identity["environment"] == "ci"
    assert identity["version"] == "0.1.0"
