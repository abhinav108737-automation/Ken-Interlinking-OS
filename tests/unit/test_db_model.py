"""ORM model and migration 0001, statically (no database needed)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from ken_contracts.events import EventEnvelope
from ken_core.db.base import Base
from ken_core.db.engine import create_engine
from ken_core.db.models import Event
from tests.conftest import make_settings

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0001_initial_events.py"


def test_events_columns_mirror_the_envelope() -> None:
    table = Event.__table__
    storage_only = {"id", "created_at"}
    assert set(table.columns.keys()) - storage_only == set(EventEnvelope.model_fields)


def test_events_table_is_append_only() -> None:
    assert "updated_at" not in Event.__table__.columns


def test_events_nullability_and_defaults() -> None:
    cols = Event.__table__.columns
    nullable = {c.name for c in cols if c.nullable}
    assert nullable == {"subject_type", "subject_id", "correlation_id"}
    for name in ("event_version", "occurred_at", "payload", "created_at"):
        assert cols[name].server_default is not None, name


def test_constraint_and_index_names_are_deterministic() -> None:
    table = Base.metadata.tables["events"]
    names = {c.name for c in table.constraints}
    assert {"pk_events", "uq_events_event_id", "ck_events_event_version_positive"} <= names
    assert any(isinstance(c, UniqueConstraint) for c in table.constraints)
    assert any(isinstance(c, CheckConstraint) for c in table.constraints)
    assert {i.name for i in table.indexes} == {
        "ix_events_event_name_occurred_at",
        "ix_events_correlation_id",
        "ix_events_subject_type_subject_id",
    }


def _ddl_type(column: Any) -> str:
    return str(column.type.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


def test_the_only_vector_column_is_the_dimensionless_page_embedding() -> None:
    """ADR-0010: one vector column, with NO fixed dimension (it is the provider's)."""
    vector_columns = [
        (table.name, column.name, _ddl_type(column))
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if _ddl_type(column).lower().startswith("vector")  # pgvector, not full-text TSVECTOR
    ]
    assert vector_columns == [("page_embeddings", "embedding", "vector")]
    table = Base.metadata.tables["page_embeddings"]
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
    assert "vector(" not in ddl.lower()


def test_migration_0001_never_enables_extensions_or_vectors() -> None:
    """ADR-0003 guardrail: 0001 must run on a PostgreSQL without pgvector."""
    source = MIGRATION.read_text()
    code = re.sub(r'"""[\s\S]*?"""', "", source)  # ignore the explanatory docstring
    code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())
    assert "extension" not in code.lower()
    assert "vector" not in code.lower()


def test_engine_uses_asyncpg_and_pool_settings() -> None:
    settings = make_settings(database_pool_size=3, database_pool_timeout_s=4.5)
    engine = create_engine(settings)
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "asyncpg"
        assert engine.dialect.is_async
        assert engine.pool.size() == 3  # type: ignore[attr-defined]
        assert engine.pool._pre_ping is True
        assert engine.pool._timeout == 4.5  # type: ignore[attr-defined]
    finally:
        engine.sync_engine.dispose()


def test_engine_bounds_the_asyncpg_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncpg's default is 60 s; an unreachable DB must fail fast (QA cleanup M3)."""
    import ken_core.db.engine as engine_module

    captured: dict[str, object] = {}

    def fake_create_async_engine(url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(engine_module, "create_async_engine", fake_create_async_engine)
    engine_module.create_engine(make_settings(database_connect_timeout_s=2.5))
    assert captured["connect_args"] == {"timeout": 2.5}
