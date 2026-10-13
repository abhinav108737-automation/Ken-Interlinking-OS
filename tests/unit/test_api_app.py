"""create_app and the lifespan: dependency creation and ownership."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ken_api import main as api_main
from ken_core import logging as ken_logging
from tests.conftest import FakeCMSProvider, FakeModelProvider, make_app, make_settings


@pytest.fixture
def logging_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(ken_logging, "configure_logging", lambda **kw: calls.append(kw))
    return calls


def test_lifespan_builds_and_closes_owned_providers(
    monkeypatch: pytest.MonkeyPatch, logging_calls: list[dict[str, Any]]
) -> None:
    model, cms = FakeModelProvider(), FakeCMSProvider()
    monkeypatch.setattr(api_main, "_build_model_provider", lambda settings: model)
    monkeypatch.setattr(api_main, "_build_cms_provider", lambda settings: cms)
    app = make_app(make_settings(log_level="WARNING", log_format="json"))

    with TestClient(app):
        assert app.state.model_provider is model
        assert app.state.cms_provider is cms
        assert app.state.engine is not None
        assert app.state.sessionmaker is not None
        assert model.closed == 0

    assert model.closed == 1
    assert cms.closed == 1
    assert logging_calls == [{"level": "WARNING", "fmt": "json"}]


def test_lifespan_does_not_close_injected_providers(
    logging_calls: list[dict[str, Any]],
) -> None:
    model, cms = FakeModelProvider(), FakeCMSProvider()
    app = make_app()
    app.state.model_provider = model
    app.state.cms_provider = cms

    with TestClient(app):
        pass

    assert model.closed == 0
    assert cms.closed == 0


def test_create_app_performs_no_database_io() -> None:
    # A dead database URL must not prevent the app from being constructed.
    app = make_app(make_settings(database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z"))
    assert getattr(app.state, "sessionmaker", None) is None


@pytest.mark.parametrize(("environment", "exposed"), [("local", True), ("production", False)])
def test_docs_only_in_development(environment: str, exposed: bool) -> None:
    client = TestClient(make_app(make_settings(environment=environment)))
    assert (client.get("/openapi.json").status_code == 200) is exposed
    assert (client.get("/docs").status_code == 200) is exposed
