"""Async engine construction.

asyncpg is the only driver (docs/adr/ADR-0004.md); ``Settings`` has already
rejected any other URL scheme, so SQLAlchemy selects its asyncpg dialect from
``postgresql+asyncpg://`` without further configuration. asyncpg is async-only:
there is deliberately no synchronous engine anywhere in the application.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ken_core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the process-wide async engine.

    Creating an engine performs no I/O: the first connection is opened lazily.
    That property is what lets the API boot (and ``/health`` answer) while the
    database is down.

    ``pool_pre_ping`` discards connections that died while idle in the pool
    (database restart, failover), instead of surfacing the first query on each
    as an error.

    Raises ``ConfigurationError`` (secret-free) when the URL targets the host's native
    PostgreSQL port, in every environment.
    """
    settings.assert_safe_runtime_target()
    return create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        pool_timeout=settings.database_pool_timeout_s,
        pool_pre_ping=True,
        # asyncpg defaults to a 60 s connect timeout; bound it so an unreachable
        # database fails fast instead of stalling a request or a worker tick.
        connect_args={"timeout": settings.database_connect_timeout_s},
    )
