"""One real worker tick against ken_interlink_test (port 5433), mock providers.

Auto-skips when the test database is unreachable (tests/conftest.py). The
conftest refuses any database whose name does not end in ``_test``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_agents.worker import run
from ken_contracts.events import EventName
from ken_core.config import Settings
from ken_core.db.engine import create_engine
from ken_core.db.repositories.events import list_events
from ken_core.db.session import create_sessionmaker

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _upgrade_to_head(database_url: str) -> None:
    """Idempotent migrate-UP of the test database only. Never downgrades."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


@pytest.fixture
def settings(test_database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        database_url=test_database_url,
        model_provider="mock",
        cms_provider="mock",
        alembic_ini_path=str(ALEMBIC_INI),
    )


@pytest.fixture
async def sessionmaker(settings: Settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # alembic's env.py calls asyncio.run(), so it must run off this loop.
    await asyncio.to_thread(_upgrade_to_head, settings.database_url)
    engine = create_engine(settings)
    yield create_sessionmaker(engine)
    await engine.dispose()


async def test_one_tick_writes_exactly_one_heartbeat(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as session:
        before = {
            e.event_id
            for e in await list_events(session, event_name=EventName.WORKER_HEARTBEAT, limit=500)
        }

    code = await run(settings, once=True, handle_signals=False)
    assert code == 0

    async with sessionmaker() as session:
        after = await list_events(session, event_name=EventName.WORKER_HEARTBEAT, limit=500)
    new = [e for e in after if e.event_id not in before]
    assert len(new) == 1
    [event] = new
    assert event.emitted_by == "ken-worker"
    assert (event.subject_type, event.subject_id) == ("service", "ken-worker")
    assert event.correlation_id is not None
    assert event.payload["tick"] == 1
    assert event.payload["checks"]["postgres"] == "ok"
    assert event.payload["checks"]["migrations"] == "ok"
    assert event.payload["checks"]["model_provider"] == "ok"  # mock provider
    assert event.payload["status"] in {"ready", "degraded"}  # pgvector is optional
