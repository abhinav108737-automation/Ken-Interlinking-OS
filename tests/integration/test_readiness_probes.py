"""Database-backed readiness probes against the real test database."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.common import HealthStatus
from ken_core.health.probes import probe_migrations, probe_pgvector, probe_postgres

pytestmark = pytest.mark.integration


async def test_postgres_probe(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    result = await probe_postgres(sessionmaker)
    assert result.status is HealthStatus.OK
    assert result.detail["database"] == "ken_interlink_test"
    assert result.detail["server_version"]
    assert "ken:ken" not in str(result.detail)


async def test_pgvector_probe_reports_available_but_not_installed(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    result = await probe_pgvector(sessionmaker)
    # The project image provisions pgvector; Phase 0 never enables it.
    assert result.status is HealthStatus.OK
    assert result.detail == {"available": True, "installed_version": None, "enabled_from_phase": 2}


@pytest.mark.usefixtures("migrated")
async def test_migrations_probe_at_head(
    sessionmaker: async_sessionmaker[AsyncSession], alembic_ini_path: str
) -> None:
    result = await probe_migrations(sessionmaker, alembic_ini_path=alembic_ini_path)
    assert result.status is HealthStatus.OK
    assert result.detail == {
        "code_head": "0002_content_inventory",
        "db_revision": "0002_content_inventory",
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
    assert detail == {"code_head": "0002_content_inventory", "db_revision": None}
