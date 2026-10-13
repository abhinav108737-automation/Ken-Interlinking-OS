"""Alembic environment: async migrations over asyncpg.

URL precedence (first non-empty wins):

1. ``-x dburl=...`` on the command line (integration tests, one-off targets);
2. ``sqlalchemy.url`` set programmatically on the Config object;
3. ``ken_core.config.get_settings().database_url`` (``KEN_DATABASE_URL``).

``alembic.ini`` deliberately leaves ``sqlalchemy.url`` empty so credentials
never live in a committed file. The same ``postgresql+asyncpg://`` URL drives
both the application and migrations (docs/adr/ADR-0004.md).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import ken_core.db.models  # noqa: F401  -- registers every model on Base.metadata
from ken_core.config import get_settings
from ken_core.db.base import Base

config = context.config

# Programmatic callers (tests, the API) already own logging; reconfiguring it
# from the ini file would clobber their handlers.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    url = x_args.get("dburl") or config.get_main_option("sqlalchemy.url")
    resolved = url or get_settings().database_url
    # Guard the URL actually used -- including a -x dburl= override -- before
    # any DDL runs: locally, port 5432 is the developer's own PostgreSQL.
    get_settings().model_copy(update={"database_url": resolved}).assert_safe_migration_target()
    return resolved


def _configure_kwargs() -> dict[str, object]:
    return {
        "target_metadata": target_metadata,
        "compare_type": True,
        "compare_server_default": True,
    }


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) without connecting."""
    context.configure(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_configure_kwargs(),
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **_configure_kwargs())
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    # NullPool: a migration run is one short-lived connection, not a service.
    engine = create_async_engine(
        _database_url(),
        poolclass=pool.NullPool,
        # asyncpg's default connect timeout is 60 s; fail fast on an unreachable host.
        connect_args={"timeout": get_settings().database_connect_timeout_s},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_sync_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
