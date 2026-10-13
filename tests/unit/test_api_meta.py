"""GET /meta: build facts and the non-secret settings allowlist."""

from __future__ import annotations

import platform

import pytest
from fastapi.testclient import TestClient

from ken_contracts.health import MetaResponse
from tests.conftest import make_app, make_settings

SECRET = "sup3rs3cret"


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql+asyncpg://ken@db.internal:5432/ken_interlink?password={SECRET}&sslmode=require",
        f"postgresql+asyncpg://ken:{SECRET}@db.internal:5432/ken_interlink",
    ],
    ids=["query-password", "userinfo-password"],
)
def test_meta_never_contains_credentials(url: str) -> None:
    response = TestClient(make_app(make_settings(database_url=url))).get("/meta")
    assert response.status_code == 200
    assert SECRET not in response.text
    assert "password" not in response.text.lower()
    settings = response.json()["settings"]
    assert settings["database_host"] == "db.internal"
    assert settings["database_name"] == "ken_interlink"


def test_meta_shape() -> None:
    settings = make_settings()
    response = TestClient(make_app(settings)).get("/meta")
    body = MetaResponse.model_validate(response.json())
    assert body.phase == 0
    assert body.auto_approve_enabled is False
    assert body.python_version == platform.python_version()
    # Exactly the allowlist: nothing else from Settings leaks in.
    assert body.settings == settings.public_settings()
