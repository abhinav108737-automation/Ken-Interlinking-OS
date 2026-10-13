"""Declarative base shared by every ORM model.

The naming convention makes every constraint and index name deterministic.
Without it PostgreSQL invents names (``events_event_id_key``) that differ from
what SQLAlchemy would emit, so Alembic autogenerate reports phantom diffs and a
hand-written downgrade cannot reliably name the constraint it must drop.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Root of the ORM model hierarchy. ``Base.metadata`` is Alembic's target."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
