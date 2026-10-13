"""Session factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory bound to ``engine``.

    ``expire_on_commit=False``: under asyncio an expired attribute cannot be
    lazily refreshed (that would be implicit I/O), so reading an object after
    commit would raise instead of returning the committed value.
    """
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
