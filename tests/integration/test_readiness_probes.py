"""Database-backed readiness probes against the real test database."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.common import HealthStatus
from ken_core.health.probes import probe_migrations, probe_postgres, probe_vector_store
from ken_core.providers.vectors.memory import MemoryVectorStore
from tests.integration.conftest import HEAD_REVISION

pytestmark = pytest.mark.integration


async def test_postgres_probe(
    sessionmaker: async_sessionmaker[AsyncSession], test_database_url: str
) -> None:
    result = await probe_postgres(sessionmaker)
    assert result.status is HealthStatus.OK
    # Whatever *_test database KEN_TEST_DATABASE_URL names, not a hard-coded one.
    assert result.detail["database"] == urlsplit(test_database_url).path.lstrip("/")
    assert result.detail["server_version"]
    assert "ken:ken" not in str(result.detail)


@pytest.mark.usefixtures("migrated")
async def test_vector_extension_is_not_installed_at_head(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """ADR-0017: no migration creates the extension, so it must be absent at head."""
    from sqlalchemy import text

    async with sessionmaker() as session:
        installed = (
            await session.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
        ).scalar_one_or_none()
    assert installed is None


async def test_vector_store_probe_reports_the_collection() -> None:
    """The probe answers over the frozen VectorStore protocol, whichever store it is."""
    store = MemoryVectorStore(collection="ken_pages")
    result = await probe_vector_store(store)
    # Reachable but not indexed yet is OK, and SAYS it is not indexed: `require_qdrant` is
    # true by default, so a DEGRADED here would 503 every fresh install.
    assert result.status is HealthStatus.OK
    assert result.detail["store"] == "memory"
    assert result.detail["collection"] == "ken_pages"
    assert result.detail["collection_present"] is False

    await store.ensure_collection(vector_size=4)
    ready = await probe_vector_store(store)
    assert ready.status is HealthStatus.OK
    assert ready.detail["collection_present"] is True


@pytest.mark.usefixtures("migrated")
async def test_migrations_probe_at_head(
    sessionmaker: async_sessionmaker[AsyncSession], alembic_ini_path: str
) -> None:
    result = await probe_migrations(sessionmaker, alembic_ini_path=alembic_ini_path)
    assert result.status is HealthStatus.OK
    assert result.detail == {
        "code_head": "0010_qdrant_and_autonomy",
        "db_revision": "0010_qdrant_and_autonomy",
    }


def test_migrations_probe_unmigrated_database(
    alembic_config: Config, alembic_ini_path: str, test_database_url: str
) -> None:
    import asyncio

    from ken_core.db.engine import create_engine
    from ken_core.db.session import create_sessionmaker
    from tests.conftest import make_settings

    async def probe() -> tuple[HealthStatus, dict[str, object]]:
        engine = create_engine(make_settings(database_url=test_database_url))
        try:
            result = await probe_migrations(
                create_sessionmaker(engine), alembic_ini_path=alembic_ini_path
            )
        finally:
            await engine.dispose()
        return result.status, dict(result.detail)

    command.downgrade(alembic_config, "base")
    try:
        status, detail = asyncio.run(probe())
    finally:
        command.upgrade(alembic_config, "head")
    assert status is HealthStatus.UNAVAILABLE
    assert detail == {"code_head": HEAD_REVISION, "db_revision": None}
