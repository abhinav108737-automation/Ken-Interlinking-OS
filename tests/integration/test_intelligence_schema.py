"""Migration 0003 (content intelligence) against the ``*_test`` database.

Covers what ``compare_metadata`` cannot: the extension, CHECK constraints, GIN
operator classes, the partial unique index, the append-only triggers, the
``vector_dims`` CHECK, and an exact downgrade to the 0002 state.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, Connection, UniqueConstraint, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError, IntegrityError

import ken_core.db.models  # noqa: F401 -- registers every model
from ken_core.db.base import Base
from ken_core.db.models.intelligence import IMMUTABLE_INTELLIGENCE_TABLES
from ken_core.db.models.inventory import IMMUTABLE_TABLES
from tests.integration.conftest import SyncDb

pytestmark = pytest.mark.integration

INTELLIGENCE_TABLES = (
    "job_runs",
    "page_analyses",
    "page_embeddings",
    "page_relationships",
    "page_protections",
)
INVENTORY_TABLES = {
    "events",
    "ingestion_runs",
    "ingestion_issues",
    "pages",
    "page_versions",
    "existing_links",
    "known_urls",
    "alembic_version",
}
HASH = sha256(b"x").hexdigest()
PAGE = uuid.UUID("00000000-0000-4000-8000-000000000002")
OTHER = uuid.UUID("00000000-0000-4000-8000-000000000003")
RUN = uuid.UUID("00000000-0000-4000-8000-000000000004")


@pytest.fixture
def db(migrated: None, sync_db: SyncDb) -> SyncDb:
    return sync_db


def _fails(db: SyncDb, fn: Callable[[Connection], Any]) -> DBAPIError:
    with pytest.raises(DBAPIError) as info:
        db.run(fn)
    return info.value


def _page(c: Connection, page_id: uuid.UUID = PAGE, path: str = "a") -> None:
    c.execute(
        text(
            "INSERT INTO pages (id, canonical_url, current_version, content_hash, "
            "publication_state, word_count, internal_link_count, external_link_count, "
            "first_seen_at, last_seen_at, last_changed_at) "
            "VALUES (:id, :u, 1, :h, 'published', 0, 0, 0, now(), now(), now())"
        ),
        {"id": page_id, "u": f"https://s.test/{path}", "h": HASH},
    )
    c.execute(
        text(
            "INSERT INTO page_versions (id, page_id, version, content_hash, hash_version, "
            "source_type, source_id, requested_url, url, canonical_url, canonical_status, "
            "publication_state, body_html, body_text, word_count, internal_link_count, "
            "external_link_count, fetched_at) VALUES (:id, :p, 1, :h, 1, 'cms', 's', 'u', 'u', "
            "'u', 'self', 'published', '', '', 0, 0, 0, now())"
        ),
        {"id": uuid.uuid4(), "p": page_id, "h": HASH},
    )


def _analysis(c: Connection, **overrides: Any) -> None:
    values = {"id": uuid.uuid4(), "p": PAGE, "av": "tax1-alg1", "intent": "unknown"} | overrides
    c.execute(
        text(
            "INSERT INTO page_analyses (id, page_id, page_version, analysis_version, extractor, "
            "page_type, intent, commercial_importance, topics) "
            "VALUES (:id, :p, 1, :av, 'deterministic', 'report', :intent, 1, "
            '\'[{"node_id": "ev"}]\'::jsonb)'
        ),
        values,
    )


def _embedding(c: Connection, *, vector: str = "[1,0,0]", dimensions: int = 3) -> None:
    c.execute(
        text(
            "INSERT INTO page_embeddings (id, page_id, page_version, model, provider, dimensions, "
            "recipe_version, input_hash, embedding) "
            "VALUES (:id, :p, 1, 'm', 'mock', :d, 1, :h, CAST(:v AS vector))"
        ),
        {"id": uuid.uuid4(), "p": PAGE, "d": dimensions, "h": HASH, "v": vector},
    )


def _seed(c: Connection) -> None:
    _page(c)
    _analysis(c)
    _embedding(c)


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
def test_pgvector_is_enabled_at_head(db: SyncDb) -> None:
    extensions: set[str] = db.run(
        lambda c: set(c.execute(text("SELECT extname FROM pg_extension")).scalars())
    )
    assert "vector" in extensions


def test_tables_constraints_and_indexes_match_the_orm(db: SyncDb) -> None:
    def _reflect(c: Connection) -> dict[str, Any]:
        inspector = inspect(c)
        return {
            table: {
                "checks": {str(x["name"]) for x in inspector.get_check_constraints(table)},
                "uniques": {str(x["name"]) for x in inspector.get_unique_constraints(table)},
                "fks": {str(x["name"]) for x in inspector.get_foreign_keys(table)},
                "indexes": {
                    str(x["name"])
                    for x in inspector.get_indexes(table)
                    if not x.get("duplicates_constraint")
                },
            }
            for table in INTELLIGENCE_TABLES
        }

    reflected = db.run(_reflect)
    # The DIALECT's rendering of each name: a convention name over 63 characters
    # is truncated deterministically (uq_page_relationships_..._rel_38f9).
    name: Any = postgresql.dialect().identifier_preparer.format_constraint  # type: ignore[no-untyped-call]
    for table in INTELLIGENCE_TABLES:
        orm = Base.metadata.tables[table]
        checks = {name(c) for c in orm.constraints if isinstance(c, CheckConstraint)}
        uniques = {name(c) for c in orm.constraints if isinstance(c, UniqueConstraint)}
        assert reflected[table]["checks"] == checks, table
        assert reflected[table]["uniques"] == uniques, table
        assert reflected[table]["fks"] == {name(fk) for fk in orm.foreign_key_constraints}
        assert reflected[table]["indexes"] == {name(i) for i in orm.indexes}, table


def test_gin_indexes_use_jsonb_path_ops_and_protection_index_is_partial(db: SyncDb) -> None:
    definitions: dict[str, str] = db.run(
        lambda c: dict(
            c.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE tablename IN "
                    "('page_analyses', 'page_protections')"
                )
            ).all()
        )
    )
    for name in (
        "ix_page_analyses_topics",
        "ix_page_analyses_geographies",
        "ix_page_analyses_entities",
    ):
        assert "USING gin" in definitions[name]
        assert "jsonb_path_ops" in definitions[name]
    active = definitions["uq_page_protections_active"]
    assert active.startswith("CREATE UNIQUE INDEX")
    assert "COALESCE(rule_id, ''::text)" in active
    assert active.endswith("WHERE active")


def test_embedding_column_is_dimensionless(db: SyncDb) -> None:
    formatted: str = db.run(
        lambda c: c.execute(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                "WHERE a.attrelid = 'page_embeddings'::regclass AND a.attname = 'embedding'"
            )
        ).scalar_one()
    )
    assert formatted == "vector"


def test_trigger_set_covers_all_history_tables(db: SyncDb) -> None:
    tables: set[str] = db.run(
        lambda c: set(
            c.execute(
                text(
                    "SELECT DISTINCT event_object_table FROM information_schema.triggers "
                    "WHERE action_statement LIKE '%ken_reject_mutation%'"
                )
            ).scalars()
        )
    )
    assert tables == set(IMMUTABLE_TABLES) | set(IMMUTABLE_INTELLIGENCE_TABLES)


# --------------------------------------------------------------------------- #
# What the database enforces
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE page_analyses SET summary = 'changed'",
        "DELETE FROM page_analyses",
        "UPDATE page_embeddings SET model = 'other'",
        "DELETE FROM page_embeddings",
    ],
)
def test_analyses_and_embeddings_are_append_only(db: SyncDb, statement: str) -> None:
    db.run(_seed)
    error = _fails(db, lambda c: c.execute(text(statement)))
    assert getattr(error.orig, "sqlstate", None) == "55000"
    assert "append-only" in str(error.orig)
    counts = db.run(
        lambda c: tuple(
            c.execute(
                text(
                    "SELECT (SELECT count(*) FROM page_analyses WHERE summary = ''), "
                    "(SELECT count(*) FROM page_embeddings WHERE model = 'm')"
                )
            ).one()
        )
    )
    assert counts == (1, 1)


def test_vector_dims_must_equal_dimensions(db: SyncDb) -> None:
    db.run(_page)
    error = _fails(db, lambda c: _embedding(c, vector="[1,0,0]", dimensions=4))
    assert isinstance(error, IntegrityError)
    assert "ck_page_embeddings_dimensions_match" in str(error.orig)
    db.run(lambda c: _embedding(c, vector="[1,0,0,0]", dimensions=4))


def test_embedding_input_hash_and_dimensions_checks(db: SyncDb) -> None:
    db.run(_page)
    error = _fails(
        db,
        lambda c: c.execute(
            text(
                "INSERT INTO page_embeddings (id, page_id, page_version, model, provider, "
                "dimensions, recipe_version, input_hash, embedding) VALUES "
                "(:id, :p, 1, 'm', 'x', 3, 1, 'not-a-hash', '[1,0,0]')"
            ),
            {"id": uuid.uuid4(), "p": PAGE},
        ),
    )
    assert "ck_page_embeddings_input_hash_sha256" in str(error.orig)


def test_one_analysis_per_version_and_analysis_version(db: SyncDb) -> None:
    db.run(_page)
    db.run(_analysis)
    db.run(lambda c: _analysis(c, av="tax2-alg1"))
    assert isinstance(_fails(db, _analysis), IntegrityError)
    assert isinstance(_fails(db, lambda c: _analysis(c, av="x", intent="vague")), IntegrityError)


def test_at_most_one_active_protection_per_page_source_rule(db: SyncDb) -> None:
    db.run(_page)

    def protect(rule: str | None, source: str = "policy") -> Callable[[Connection], Any]:
        return lambda c: c.execute(
            text(
                "INSERT INTO page_protections (page_id, source, rule_id, reason, created_by) "
                "VALUES (:p, :s, :r, 'why', 'policy')"
            ),
            {"p": PAGE, "s": source, "r": rule},
        )

    db.run(protect("legal"))
    assert isinstance(_fails(db, protect("legal")), IntegrityError)
    db.run(protect("legal", source="manual"))  # another source is another protection
    db.run(protect(None, source="manual"))
    assert isinstance(_fails(db, protect(None, source="manual")), IntegrityError)
    # Deactivated rows are history: a new active one may follow.
    db.run(
        lambda c: c.execute(
            text(
                "UPDATE page_protections SET active = false, deactivated_at = now(), "
                "deactivated_by = 'policy' WHERE source = 'policy'"
            )
        )
    )
    db.run(protect("legal"))
    # active and deactivated_at must agree.
    error = _fails(
        db,
        lambda c: c.execute(
            text("UPDATE page_protections SET active = false WHERE active AND source = 'policy'")
        ),
    )
    assert "ck_page_protections_active_iff_not_deactivated" in str(error.orig)
    assert isinstance(_fails(db, protect("x", source="robot")), IntegrityError)


def test_relationship_checks(db: SyncDb) -> None:
    db.run(_page)
    db.run(lambda c: _page(c, OTHER, "b"))

    def edge(source: uuid.UUID, target: uuid.UUID, kind: str, weight: float) -> Any:
        return lambda c: c.execute(
            text(
                "INSERT INTO page_relationships (source_page_id, source_page_version, "
                "target_page_id, target_page_version, relationship_type, weight, "
                "analysis_version, computed_at) VALUES (:s, 1, :t, 1, :k, :w, 'tax1-alg1', now())"
            ),
            {"s": source, "t": target, "k": kind, "w": weight},
        )

    db.run(edge(PAGE, OTHER, "same_market", 1.0))
    db.run(edge(OTHER, PAGE, "same_market", 1.0))  # directed: the reverse is another edge
    assert isinstance(_fails(db, edge(PAGE, OTHER, "same_market", 0.5)), IntegrityError)
    assert isinstance(_fails(db, edge(PAGE, PAGE, "submarket", 0.5)), IntegrityError)
    assert isinstance(_fails(db, edge(PAGE, OTHER, "submarket", 1.5)), IntegrityError)
    assert isinstance(_fails(db, edge(PAGE, OTHER, "friends", 0.5)), IntegrityError)
    # Derived data is mutable by design.
    db.run(lambda c: c.execute(text("UPDATE page_relationships SET weight = 0.5")))
    db.run(lambda c: c.execute(text("DELETE FROM page_relationships")))


def test_job_run_checks(db: SyncDb) -> None:
    def run(**values: Any) -> Any:
        row = {"id": uuid.uuid4(), "kind": "analysis", "status": "running", "done": None} | values
        return lambda c: c.execute(
            text(
                "INSERT INTO job_runs (id, kind, status, triggered_by, completed_at) "
                "VALUES (:id, :kind, :status, 't', :done)"
            ),
            row,
        )

    db.run(run())
    assert isinstance(_fails(db, run(kind="crawl")), IntegrityError)
    assert isinstance(_fails(db, run(status="paused")), IntegrityError)
    assert isinstance(_fails(db, run(status="succeeded")), IntegrityError)  # needs completed_at
    assert isinstance(_fails(db, run(done=datetime(2026, 1, 1, tzinfo=UTC))), IntegrityError)


def test_analysis_run_reference_is_restricted(db: SyncDb) -> None:
    db.run(_page)
    db.run(
        lambda c: c.execute(
            text(
                "INSERT INTO job_runs (id, kind, status, triggered_by) "
                "VALUES (:id, 'analysis', 'running', 't')"
            ),
            {"id": RUN},
        )
    )
    db.run(
        lambda c: c.execute(
            text(
                "INSERT INTO page_analyses (id, page_id, page_version, analysis_version, "
                "extractor, page_type, intent, commercial_importance, run_id) "
                "VALUES (:id, :p, 1, 'v', 'deterministic', 'r', 'unknown', 0, :r)"
            ),
            {"id": uuid.uuid4(), "p": PAGE, "r": RUN},
        )
    )
    error = _fails(db, lambda c: c.execute(text("DELETE FROM job_runs")))
    assert isinstance(error, IntegrityError)


# --------------------------------------------------------------------------- #
# Downgrade
# --------------------------------------------------------------------------- #
def _state(c: Connection) -> dict[str, Any]:
    return {
        "tables": set(inspect(c).get_table_names()),
        "extensions": set(c.execute(text("SELECT extname FROM pg_extension")).scalars()),
        "triggers": set(
            c.execute(
                text(
                    "SELECT DISTINCT event_object_table FROM information_schema.triggers "
                    "WHERE action_statement LIKE '%ken_reject_mutation%'"
                )
            ).scalars()
        ),
        "functions": set(
            c.execute(
                text(
                    "SELECT p.proname FROM pg_proc p JOIN pg_namespace n "
                    "ON n.oid = p.pronamespace WHERE n.nspname = 'public'"
                )
            ).scalars()
        ),
        "revision": c.execute(text("SELECT version_num FROM alembic_version")).scalar(),
    }


def test_downgrade_to_0002_restores_exactly_the_inventory_schema(
    alembic_config: Config, sync_db: SyncDb
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "0002_content_inventory")
    try:
        state = sync_db.run(_state)
        assert state["tables"] == INVENTORY_TABLES
        assert state["extensions"] == {"plpgsql"}
        assert state["triggers"] == set(IMMUTABLE_TABLES)
        assert state["functions"] == {"ken_reject_mutation"}
        assert state["revision"] == "0002_content_inventory"
    finally:
        command.upgrade(alembic_config, "head")
    state = sync_db.run(_state)
    assert set(INTELLIGENCE_TABLES) <= state["tables"]
    assert "vector" in state["extensions"]
    assert state["revision"] == "0003_content_intelligence"
