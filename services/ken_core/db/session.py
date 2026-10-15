"""Session factories.

* :func:`create_sessionmaker` -- the read-write factory (services, the auth provider, jobs).
* :func:`create_readonly_sessionmaker` -- for GET readers ONLY (ADR-0014 s8): every
  transaction it begins starts with ``SET TRANSACTION READ ONLY``, so a bug in a GET handler
  cannot write (PostgreSQL refuses with SQLSTATE 25006). The auth provider keeps the
  read-write factory, because it writes ``last_seen_at``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, SessionTransaction


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory bound to ``engine``.

    ``expire_on_commit=False``: under asyncio an expired attribute cannot be
    lazily refreshed (that would be implicit I/O), so reading an object after
    commit would raise instead of returning the committed value.
    """
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class ReadOnlySession(Session):
    """A sync ``Session`` whose every database transaction is ``READ ONLY``."""


@event.listens_for(ReadOnlySession, "after_begin")
def _read_only(session: Session, transaction: SessionTransaction, connection: Connection) -> Any:
    # The first statement of the transaction: PostgreSQL requires SET TRANSACTION to precede
    # any query. after_begin runs once per connection the session transaction uses.
    connection.exec_driver_sql("SET TRANSACTION READ ONLY")


def create_readonly_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """The factory for GET readers: READ ONLY transactions over the same engine (pool)."""
    return async_sessionmaker(
        engine, class_=AsyncSession, sync_session_class=ReadOnlySession, expire_on_commit=False
    )
