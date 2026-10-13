"""Integration fixtures. Every test here runs against the ``*_test`` database only.

The skip-when-unreachable and ``_test``-name guard live in tests/conftest.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import TypeVar

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from ken_core.db.engine import create_engine as create_async_app_engine
from ken_core.db.session import create_sessionmaker
from tests.conftest import make_settings

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@pytest.fixture(scope="session")
def alembic_ini_path() -> str:
    return str(ALEMBIC_INI)


@pytest.fixture
def alembic_config(test_database_url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    # configparser interpolation: a literal % in the URL must be doubled.
    config.set_main_option("sqlalchemy.url", test_database_url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    return config


T = TypeVar("T")


class SyncDb:
    """Run synchronous inspection code against the test DB over asyncpg.

    asyncpg is async-only, so there is no synchronous SQLAlchemy engine. Each
    ``run`` opens a short-lived async engine inside ``asyncio.run`` and hands
    ``fn`` a sync-facing Connection via ``run_sync`` -- which is what
    ``inspect()``, ``MigrationContext`` and ``compare_metadata`` need.

    Tests that use this stay SYNCHRONOUS on purpose: they also call Alembic
    commands, and the async migrations/env.py runs its own ``asyncio.run``,
    which would fail inside an already-running event loop.
    """

    def __init__(self, url: str) -> None:
        self._url = url

    def run(self, fn: Callable[[Connection], T]) -> T:
        async def _go() -> T:
            engine = create_async_engine(self._url, poolclass=NullPool)
            try:
                async with engine.begin() as connection:
                    return await connection.run_sync(fn)
            finally:
                await engine.dispose()

        return asyncio.run(_go())


@pytest.fixture
def sync_db(test_database_url: str) -> SyncDb:
    return SyncDb(test_database_url)


@pytest.fixture
def migrated(alembic_config: Config, sync_db: SyncDb) -> Iterator[None]:
    """Test database at head, with an empty events table."""
    command.upgrade(alembic_config, "head")
    sync_db.run(lambda connection: connection.execute(text("TRUNCATE events RESTART IDENTITY")))
    yield


@pytest.fixture
async def async_engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_app_engine(make_settings(database_url=test_database_url))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def sessionmaker(async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(async_engine)
