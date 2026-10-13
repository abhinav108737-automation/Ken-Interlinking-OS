"""Repository-wide pytest configuration.

1. Integration tests (``@pytest.mark.integration``) AUTO-SKIP when the test
   database is unreachable, so ``pytest`` stays green on a machine with no
   PostgreSQL running. They never fall back to another database.
2. Protocol fakes for ``ModelProvider`` and ``CMSProvider``, so API and probe
   tests never depend on the concrete providers.

Test database: ``KEN_TEST_DATABASE_URL``, default the project container on
port 5433 (never 5432: docs/adr/ADR-0003.md). Integration tests migrate it up
AND DOWN, so its database name must end in ``_test``; anything else is refused
rather than risk dropping tables in a real database.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import pytest
from pydantic import BaseModel

from ken_contracts.cms import (
    CMSPage,
    DeploymentResult,
    LinkChangeRequest,
    RollbackResult,
    ValidationResult,
)
from ken_contracts.model import (
    Completion,
    EmbeddingResult,
    GenerationOptions,
    Message,
    ModelRole,
    ModelRoleStatus,
    ProviderCapabilities,
    ProviderHealth,
    StructuredResult,
)

DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://ken:ken@localhost:5433/ken_interlink_test"
TEST_DATABASE_URL = os.environ.get("KEN_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)

_db_unavailable_reason: str | None = None
_db_checked = False


def _test_database_unavailable_reason() -> str | None:
    """None if the test database is usable, else why not. Checked once per session."""
    global _db_checked, _db_unavailable_reason
    if _db_checked:
        return _db_unavailable_reason
    _db_checked = True

    database = urlsplit(TEST_DATABASE_URL).path.lstrip("/")
    if not database.endswith("_test"):
        _db_unavailable_reason = (
            f"KEN_TEST_DATABASE_URL must name a database ending in '_test' (got {database!r}); "
            "integration tests run destructive migrations"
        )
        return _db_unavailable_reason

    # asyncpg is async-only, so probe with a short-lived async engine. Collection
    # runs outside any event loop, so asyncio.run is safe here.
    import asyncio

    from sqlalchemy import text  # local: unit-only runs never need it
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    async def _probe() -> None:
        engine = create_async_engine(
            TEST_DATABASE_URL, poolclass=NullPool, connect_args={"timeout": 2}
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(_probe())
    except Exception as exc:
        # Class name only: the driver message can include host and user.
        _db_unavailable_reason = f"test database unreachable ({type(exc).__name__})"
    return _db_unavailable_reason


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    integration = [item for item in items if item.get_closest_marker("integration")]
    if not integration:
        return
    reason = _test_database_unavailable_reason()
    if reason is None:
        return
    if os.environ.get("KEN_REQUIRE_INTEGRATION_DB", "").lower() in {"1", "true", "yes"}:
        # The quality gate (make verify, CI) sets this. Without it, a dead
        # database would skip every integration test and the gate would still
        # go green -- a false pass.
        raise pytest.UsageError(
            f"KEN_REQUIRE_INTEGRATION_DB is set but integration tests cannot run: {reason}"
        )
    skip = pytest.mark.skip(reason=reason)
    for item in integration:
        item.add_marker(skip)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return TEST_DATABASE_URL


# --------------------------------------------------------------------------- #
# Protocol fakes
# --------------------------------------------------------------------------- #
class FakeModelProvider:
    """Satisfies ``ModelProvider``. Only health/capabilities are meaningful."""

    name = "fake-model"

    def __init__(
        self,
        *,
        reachable: bool = True,
        role_status: dict[ModelRole, str] | None = None,
    ) -> None:
        self.reachable = reachable
        self.role_status = role_status or dict.fromkeys(ModelRole, "ok")
        self.closed = 0

    async def complete(
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        options: GenerationOptions | None = None,
    ) -> Completion:
        raise NotImplementedError

    async def complete_structured[S: BaseModel](
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        schema: type[S],
        options: GenerationOptions | None = None,
        max_attempts: int = 2,
    ) -> StructuredResult[S]:
        raise NotImplementedError

    async def embed(
        self, *, texts: Sequence[str], role: ModelRole = ModelRole.EMBEDDING
    ) -> EmbeddingResult:
        raise NotImplementedError

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            supports_tools=False,
            supports_json_schema=True,
            supports_embeddings=False,
            roles=[
                ModelRoleStatus(role=role, model=f"model-{role.value}", status=status)
                for role, status in self.role_status.items()
            ],
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            reachable=self.reachable,
            error=None if self.reachable else "connection refused to http://secret-host:1",
        )

    async def aclose(self) -> None:
        self.closed += 1


class FakeCMSProvider:
    """Satisfies ``CMSProvider``. Only health is meaningful."""

    name = "fake-cms"

    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable
        self.closed = 0

    async def get_page(self, *, page_id: str) -> CMSPage:
        raise NotImplementedError

    async def validate_link_changes(self, *, request: LinkChangeRequest) -> ValidationResult:
        raise NotImplementedError

    async def apply_link_changes(self, *, request: LinkChangeRequest) -> DeploymentResult:
        raise NotImplementedError

    async def rollback(
        self, *, deployment_id: str, operation_id: Any, actor: str
    ) -> RollbackResult:
        raise NotImplementedError

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, reachable=self.reachable)

    async def aclose(self) -> None:
        self.closed += 1


@pytest.fixture
def fake_model_provider() -> FakeModelProvider:
    return FakeModelProvider()


@pytest.fixture
def fake_cms_provider() -> FakeCMSProvider:
    return FakeCMSProvider()


# --------------------------------------------------------------------------- #
# Settings and API app helpers
# --------------------------------------------------------------------------- #
def make_settings(**overrides: Any) -> Any:
    """Settings isolated from any developer ``.env`` and from ``KEN_*`` env vars' file."""
    from ken_core.config import Settings

    return Settings(_env_file=None, **overrides)


def make_app(settings: Any = None, *, probes: Sequence[Any] | None = None) -> Any:
    """A ``create_app`` instance whose readiness probes are replaced by ``probes``.

    No lifespan runs unless the caller enters ``TestClient`` as a context
    manager, so nothing here touches a database or a provider.
    """
    from ken_api.deps import get_probes
    from ken_api.main import create_app

    app = create_app(settings if settings is not None else make_settings())
    if probes is not None:
        fixed = list(probes)
        app.dependency_overrides[get_probes] = lambda: fixed
    return app
