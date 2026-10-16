"""Migration 0002 (content inventory) against the ``*_test`` database.

Covers what ``compare_metadata`` cannot: CHECK constraints, the immutability
triggers, the deferred current-version foreign key, and an exact downgrade.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from hashlib import sha256
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

import ken_core.db.models  # noqa: F401 -- registers every model
from ken_core.db.base import Base
from ken_core.db.models.agents import APPEND_ONLY_AGENT_TABLES
from ken_core.db.models.analytics import APPEND_ONLY_ANALYTICS_TABLES
from ken_core.db.models.deployment import APPEND_ONLY_DEPLOYMENT_TABLES
from ken_core.db.models.intelligence import IMMUTABLE_INTELLIGENCE_TABLES
from ken_core.db.models.inventory import IMMUTABLE_TABLES
from ken_core.db.models.learning import APPEND_ONLY_LEARNING_TABLES
from ken_core.db.models.recommendations import APPEND_ONLY_RECOMMENDATION_TABLES
from ken_core.db.models.review import APPEND_ONLY_REVIEW_TABLES
from tests.integration.conftest import TRUNCATE_ALL, SyncDb

pytestmark = pytest.mark.integration

INVENTORY_TABLES = (
    "ingestion_runs",
    "ingestion_issues",
    "pages",
    "page_versions",
    "existing_links",
    "known_urls",
)
HASH = sha256(b"x").hexdigest()
RUN = uuid.UUID("00000000-0000-4000-8000-000000000001")
PAGE = uuid.UUID("00000000-0000-4000-8000-000000000002")


# --------------------------------------------------------------------------- #
# Seed SQL (plain SQL: the point is what the DATABASE enforces)
# --------------------------------------------------------------------------- #
def _insert_run(c: Connection, **overrides: Any) -> None:
    values = {
        "id": RUN,
        "source_type": "file_export",
        "source_name": "t",
        "status": "running",
        "completed_at": None,
    } | overrides
    c.execute(
        text(
            "INSERT INTO ingestion_runs (id, source_type, source_name, status, completed_at) "
            "VALUES (:id, :source_type, :source_name, :status, :completed_at)"
        ),
        values,
    )


def _insert_page(c: Connection, *, version: int = 1, content_hash: str = HASH) -> None:
    c.execute(
        text(
            "INSERT INTO pages (id, canonical_url, current_version, content_hash, "
            "publication_state, word_count, internal_link_count, external_link_count, "
            "first_seen_at, last_seen_at, last_changed_at) VALUES (:id, 'https://s.test/', "
            ":v, :h, 'published', 0, 0, 0, now(), now(), now())"
        ),
        {"id": PAGE, "v": version, "h": content_hash},
    )


def _insert_version(c: Connection, *, version: int = 1, canonical_status: str = "self") -> None:
    c.execute(
        text(
            "INSERT INTO page_versions (id, page_id, version, content_hash, hash_version, "
            "source_type, source_id, requested_url, url, canonical_url, canonical_status, "
            "publication_state, body_html, body_text, word_count, internal_link_count, "
            "external_link_count, fetched_at) VALUES (:id, :page, :v, :h, 1, 'file_export', "
            "'s', 'https://s.test/', 'https://s.test/', 'https://s.test/', :cs, 'published', "
            "'', '', 0, 0, 0, now())"
        ),
        {"id": uuid.uuid4(), "page": PAGE, "v": version, "h": HASH, "cs": canonical_status},
    )


def _insert_link(c: Connection) -> None:
    c.execute(
        text(
            "INSERT INTO existing_links (page_id, page_version, ordinal, href, link_type, "
            "anchor_text, anchor_source, block_id, block_kind, occurrence_index, context_text, "
            "context_hash) VALUES (:page, 1, 0, '/x', 'internal', 'a', 'text', 'b0', 'p', 0, "
            "'c', :h)"
        ),
        {"page": PAGE, "h": HASH},
    )


def _seed_page(c: Connection) -> None:
    _insert_page(c)
    _insert_version(c)
    _insert_link(c)


@pytest.fixture
def empty(migrated: None, sync_db: SyncDb) -> SyncDb:
    return sync_db


def _fails(db: SyncDb, fn: Callable[[Connection], Any]) -> DBAPIError:
    """Run ``fn`` in its own transaction; it must fail (at statement or COMMIT)."""
    with pytest.raises(DBAPIError) as info:
        db.run(fn)
    return info.value


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
def test_head_creates_every_inventory_table_constraint_and_index(empty: SyncDb) -> None:
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
                "pk": inspector.get_pk_constraint(table)["name"],
            }
            for table in INVENTORY_TABLES
        }

    reflected = empty.run(_reflect)
    for name in INVENTORY_TABLES:
        table = Base.metadata.tables[name]
        expected_checks = {
            str(c.name) for c in table.constraints if c.__class__.__name__ == "CheckConstraint"
        }
        expected_uniques = {
            str(c.name) for c in table.constraints if c.__class__.__name__ == "UniqueConstraint"
        }
        expected_fks = {str(fk.name) for fk in table.foreign_key_constraints}
        assert reflected[name]["checks"] == expected_checks, name
        assert reflected[name]["uniques"] == expected_uniques, name
        assert reflected[name]["fks"] == expected_fks, name
        assert reflected[name]["indexes"] == {str(i.name) for i in table.indexes}, name
        assert reflected[name]["pk"] == table.primary_key.name, name


def test_current_version_foreign_key_is_deferrable_initially_deferred(empty: SyncDb) -> None:
    row = empty.run(
        lambda c: c.execute(
            text(
                "SELECT condeferrable, condeferred FROM pg_constraint "
                "WHERE conname = 'fk_pages_id_current_version_page_versions'"
            )
        ).one()
    )
    assert tuple(row) == (True, True)


def test_identity_columns_are_generated_always(empty: SyncDb) -> None:
    rows = empty.run(
        lambda c: c.execute(
            text(
                "SELECT table_name, identity_generation FROM information_schema.columns "
                "WHERE column_name = 'id' AND is_identity = 'YES' "
                "AND table_name IN ('ingestion_issues', 'existing_links') ORDER BY table_name"
            )
        ).all()
    )
    assert [tuple(r) for r in rows] == [
        ("existing_links", "ALWAYS"),
        ("ingestion_issues", "ALWAYS"),
    ]


# --------------------------------------------------------------------------- #
# Immutability
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE page_versions SET title = 'changed'",
        "DELETE FROM page_versions",
        "UPDATE existing_links SET anchor_text = 'changed'",
        "DELETE FROM existing_links",
    ],
)
def test_versions_and_links_reject_update_and_delete(empty: SyncDb, statement: str) -> None:
    empty.run(_seed_page)
    error = _fails(empty, lambda c: c.execute(text(statement)))
    assert "append-only" in str(error.orig)
    assert getattr(error.orig, "sqlstate", None) == "55000"
    counts = empty.run(
        lambda c: c.execute(
            text(
                "SELECT (SELECT count(*) FROM page_versions), "
                "(SELECT count(*) FROM page_versions WHERE title IS NULL), "
                "(SELECT count(*) FROM existing_links WHERE anchor_text = 'a')"
            )
        ).one()
    )
    assert tuple(counts) == (1, 1, 1)


def test_trigger_set_matches_the_orm_declaration(empty: SyncDb) -> None:
    rows: list[str] = empty.run(
        lambda c: list(
            c.execute(
                text(
                    "SELECT DISTINCT event_object_table FROM information_schema.triggers "
                    "WHERE action_statement LIKE '%ken_reject_mutation%'"
                )
            ).scalars()
        )
    )
    # Migrations 0003-0006 extend the same function to later history tables.
    assert set(rows) == (
        set(IMMUTABLE_TABLES)
        | set(IMMUTABLE_INTELLIGENCE_TABLES)
        | set(APPEND_ONLY_RECOMMENDATION_TABLES)
        | set(APPEND_ONLY_AGENT_TABLES)
        | set(APPEND_ONLY_REVIEW_TABLES)
        | set(APPEND_ONLY_DEPLOYMENT_TABLES)
        | set(APPEND_ONLY_ANALYTICS_TABLES)
        # Migration 0009 (Phase 8) extends the same function to the four append-only
        # learning tables: a learning audit trail is never rewritten.
        | set(APPEND_ONLY_LEARNING_TABLES)
    )


def test_pages_remain_updatable(empty: SyncDb) -> None:
    empty.run(_seed_page)
    empty.run(lambda c: c.execute(text("UPDATE pages SET gone_at = now()")))


# --------------------------------------------------------------------------- #
# The deferred current-version pointer
# --------------------------------------------------------------------------- #
def test_page_pointing_at_a_missing_version_fails_at_commit(empty: SyncDb) -> None:
    def _bad(c: Connection) -> None:
        _insert_page(c, version=1)  # accepted now: the check is deferred
        _insert_version(c, version=1)
        c.execute(text("UPDATE pages SET current_version = 2"))  # no version 2

    error = _fails(empty, _bad)
    assert isinstance(error, IntegrityError)
    assert empty.run(lambda c: c.execute(text("SELECT count(*) FROM pages")).scalar()) == 0


def test_page_then_version_in_one_transaction_commits(empty: SyncDb) -> None:
    empty.run(_seed_page)
    assert empty.run(lambda c: c.execute(text("SELECT count(*) FROM pages")).scalar()) == 1


# --------------------------------------------------------------------------- #
# CHECK constraints
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "succeeded", "completed_at": None},  # finished needs completed_at
        {"status": "bogus"},
        {"source_type": "ftp"},
    ],
)
def test_run_checks(empty: SyncDb, overrides: dict[str, Any]) -> None:
    error = _fails(empty, lambda c: _insert_run(c, **overrides))
    assert isinstance(error, IntegrityError)


def test_running_run_cannot_have_completed_at(empty: SyncDb) -> None:
    def _bad(c: Connection) -> None:
        _insert_run(c)
        c.execute(text("UPDATE ingestion_runs SET completed_at = now()"))

    assert isinstance(_fails(empty, _bad), IntegrityError)


def test_negative_counter_is_rejected(empty: SyncDb) -> None:
    def _bad(c: Connection) -> None:
        _insert_run(c)
        c.execute(text("UPDATE ingestion_runs SET pages_failed = -1"))

    assert isinstance(_fails(empty, _bad), IntegrityError)


def test_content_hash_must_be_sha256_hex(empty: SyncDb) -> None:
    def _bad(c: Connection) -> None:
        _insert_page(c, content_hash="ABC")
        _insert_version(c)

    error = _fails(empty, _bad)
    assert isinstance(error, IntegrityError)


def test_canonical_status_enum(empty: SyncDb) -> None:
    def _bad(c: Connection) -> None:
        _insert_page(c)
        _insert_version(c, canonical_status="maybe")

    error = _fails(empty, _bad)
    assert isinstance(error, IntegrityError)


def test_known_url_page_outcome_requires_a_page(empty: SyncDb) -> None:
    error = _fails(
        empty,
        lambda c: c.execute(
            text(
                "INSERT INTO known_urls (url, outcome, first_seen_at, last_seen_at) "
                "VALUES ('https://s.test/a', 'page', now(), now())"
            )
        ),
    )
    assert isinstance(error, IntegrityError)


def test_known_url_failure_may_keep_its_page(empty: SyncDb) -> None:
    """The CHECK is an implication: a failed observation keeps the last known page."""
    empty.run(_seed_page)
    empty.run(
        lambda c: c.execute(
            text(
                "INSERT INTO known_urls (url, page_id, outcome, first_seen_at, last_seen_at) "
                "VALUES ('https://s.test/a', :p, 'not_found', now(), now()), "
                "('https://s.test/b', NULL, 'fetch_error', now(), now())"
            ),
            {"p": PAGE},
        )
    )


def test_external_id_is_unique_when_present(empty: SyncDb) -> None:
    def _two(c: Connection, external_id: str | None) -> None:
        for n in (1, 2):
            page = uuid.uuid4()
            c.execute(
                text(
                    "INSERT INTO pages (id, canonical_url, external_id, current_version, "
                    "content_hash, publication_state, word_count, internal_link_count, "
                    "external_link_count, first_seen_at, last_seen_at, last_changed_at) "
                    "VALUES (:id, :u, :e, 1, :h, 'published', 0, 0, 0, now(), now(), now())"
                ),
                {"id": page, "u": f"https://s.test/{n}", "e": external_id, "h": HASH},
            )
            c.execute(
                text(
                    "INSERT INTO page_versions (id, page_id, version, content_hash, "
                    "hash_version, source_type, source_id, requested_url, url, canonical_url, "
                    "canonical_status, publication_state, body_html, body_text, word_count, "
                    "internal_link_count, external_link_count, fetched_at) VALUES (:id, :p, 1, "
                    ":h, 1, 'cms', 's', 'u', 'u', 'u', 'self', 'published', '', '', 0, 0, 0, "
                    "now())"
                ),
                {"id": uuid.uuid4(), "p": page, "h": HASH},
            )

    empty.run(lambda c: _two(c, None))  # many pages have no external id
    empty.run(lambda c: c.execute(text(TRUNCATE_ALL)))
    assert isinstance(_fails(empty, lambda c: _two(c, "cms-1")), IntegrityError)


# --------------------------------------------------------------------------- #
# Downgrade
# --------------------------------------------------------------------------- #
def _public_tables(c: Connection) -> set[str]:
    return set(inspect(c).get_table_names())


def _ken_functions(c: Connection) -> set[str]:
    return set(
        c.execute(
            text(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public'"
            )
        ).scalars()
    )


def test_downgrade_to_0001_restores_exactly_the_events_schema(
    alembic_config: Config, sync_db: SyncDb
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "0001_initial_events")
    try:
        assert sync_db.run(_public_tables) == {"events", "alembic_version"}
        assert "ken_reject_mutation" not in sync_db.run(_ken_functions)
        assert sync_db.run(
            lambda c: c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        ) == ("0001_initial_events")
    finally:
        command.upgrade(alembic_config, "head")
    assert set(INVENTORY_TABLES) <= sync_db.run(_public_tables)
    assert "ken_reject_mutation" in sync_db.run(_ken_functions)


def test_no_extension_beyond_plpgsql_and_pgvector(empty: SyncDb) -> None:
    extensions: set[str] = empty.run(
        lambda c: set(c.execute(text("SELECT extname FROM pg_extension")).scalars())
    )
    # 0002 needs none; pgvector arrives with migration 0003 (ADR-0010).
    assert extensions == {"plpgsql", "vector"}
