"""Migration 0004 (recommendations) against the ``*_test`` database.

Structure matches the ORM; ``page_versions.search_vector`` is generated (checked
semantically) with a GIN index; the GENERIC plan content guard; append-only history and
candidates; the partial unique indexes; the pinned target version; an exact downgrade.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
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
from ken_core.db.models.recommendations import (
    APPEND_ONLY_RECOMMENDATION_TABLES,
    PLAN_MUTABLE_COLUMNS,
)
from tests.integration.conftest import HEAD_REVISION, TRUNCATE_ALL, SyncDb

pytestmark = pytest.mark.integration

RECOMMENDATION_TABLES = ("link_plans", "link_plan_transitions", "link_candidates", "opportunities")
HASH = sha256(b"x").hexdigest()
CTX = sha256(b"block").hexdigest()
SOURCE = uuid.UUID("00000000-0000-4000-8000-000000000101")
TARGET = uuid.UUID("00000000-0000-4000-8000-000000000102")
THIRD = uuid.UUID("00000000-0000-4000-8000-000000000103")
RUN = uuid.UUID("00000000-0000-4000-8000-000000000104")


@pytest.fixture
def db(migrated: None, sync_db: SyncDb) -> SyncDb:
    return sync_db


def _fails(db: SyncDb, fn: Callable[[Connection], Any]) -> DBAPIError:
    with pytest.raises(DBAPIError) as info:
        db.run(fn)
    return info.value


def _page(
    c: Connection,
    page_id: uuid.UUID,
    path: str,
    *,
    title: str = "Title",
    body: str = "",
    description: str | None = None,
) -> None:
    c.execute(
        text(
            "INSERT INTO pages (id, canonical_url, current_version, content_hash, "
            "publication_state, word_count, internal_link_count, external_link_count, "
            "first_seen_at, last_seen_at, last_changed_at) "
            "VALUES (:id, :u, 1, :h, 'published', 0, 0, 0, now(), now(), now())"
        ),
        {"id": page_id, "u": f"https://s.test/{path}", "h": HASH},
    )
    _version(c, page_id, 1, title=title, body=body, description=description)


def _version(
    c: Connection,
    page_id: uuid.UUID,
    version: int,
    *,
    title: str | None = "Title",
    body: str = "",
    description: str | None = None,
) -> None:
    c.execute(
        text(
            "INSERT INTO page_versions (id, page_id, version, content_hash, hash_version, "
            "source_type, source_id, requested_url, url, canonical_url, canonical_status, "
            "publication_state, title, meta_description, body_html, body_text, word_count, "
            "internal_link_count, external_link_count, fetched_at) VALUES (:id, :p, :v, :h, 1, "
            "'cms', 's', 'u', 'u', 'u', 'self', 'published', :t, :d, '', :b, 0, 0, 0, now())"
        ),
        {
            "id": uuid.uuid4(),
            "p": page_id,
            "v": version,
            "h": HASH,
            "t": title,
            "d": description,
            "b": body,
        },
    )


def _plan(c: Connection, **overrides: Any) -> uuid.UUID:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "source": SOURCE,
        "sv": 1,
        "target": TARGET,
        "tv": 1,
        "status": "proposed",
        "ctx": CTX,
        "start": 0,
        "end": 5,
        "supersedes": None,
        "anchor": "anchor",
    } | overrides
    c.execute(
        text(
            "INSERT INTO link_plans (id, source_page_id, source_page_version, source_url, "
            "target_page_id, target_page_version, target_url, anchor_text, anchor_source, "
            "block_id, block_kind, occurrence_index, start_offset, end_offset, context_hash, "
            "total_score, confidence, rationale, engine_version, policy_version, "
            "ranking_policy_version, analysis_version, supersedes, status) VALUES (:id, "
            ":source, :sv, 'https://s.test/s', :target, :tv, 'https://s.test/t', :anchor, "
            "'target_topic', 'b0', 'p', 0, :start, :end, :ctx, 0.5, 0.5, 'r', 'e', 1, 1, "
            "'tax1-alg1', :supersedes, :status)"
        ),
        values,
    )
    return uuid.UUID(str(values["id"]))


def _seed(c: Connection) -> None:
    _page(c, SOURCE, "s")
    _page(c, TARGET, "t")
    _page(c, THIRD, "u")


def _run(c: Connection) -> None:
    c.execute(
        text(
            "INSERT INTO job_runs (id, kind, status, triggered_by) "
            "VALUES (:id, 'recommendation', 'running', 't')"
        ),
        {"id": RUN},
    )


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
def test_tables_constraints_and_indexes_match_the_orm(db: SyncDb) -> None:
    tables = (*RECOMMENDATION_TABLES, "page_versions")

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
            for table in tables
        }

    reflected = db.run(_reflect)
    name: Any = postgresql.dialect().identifier_preparer.format_constraint  # type: ignore[no-untyped-call]
    for table in tables:
        orm = Base.metadata.tables[table]
        assert reflected[table]["checks"] == {
            name(c) for c in orm.constraints if isinstance(c, CheckConstraint)
        }, table
        assert reflected[table]["uniques"] == {
            name(c) for c in orm.constraints if isinstance(c, UniqueConstraint)
        }, table
        assert reflected[table]["fks"] == {name(fk) for fk in orm.foreign_key_constraints}, table
        assert reflected[table]["indexes"] == {name(i) for i in orm.indexes}, table
        assert reflected[table]["pk"] == orm.primary_key.name, table


def test_partial_unique_indexes_are_partial_on_open_statuses(db: SyncDb) -> None:
    definitions: dict[str, str] = db.run(
        lambda c: dict(
            c.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'link_plans'")
            ).all()
        )
    )
    for index in ("uq_link_plans_open_pair", "uq_link_plans_open_span"):
        definition = definitions[index]
        assert definition.startswith("CREATE UNIQUE INDEX"), index
        for status in ("proposed", "approved", "validated"):
            assert f"'{status}'" in definition
        for status in ("rejected", "expired", "deployed", "superseded"):
            assert f"'{status}'" not in definition
    assert definitions["uq_link_plans_supersedes"].endswith("WHERE (supersedes IS NOT NULL)")
    assert "total_score DESC" in definitions["ix_link_plans_status_total_score"]


def test_search_vector_is_a_stored_generated_column_with_a_gin_index(db: SyncDb) -> None:
    def _facts(c: Connection) -> tuple[Any, ...]:
        column = c.execute(
            text(
                "SELECT is_generated, data_type FROM information_schema.columns "
                "WHERE table_name = 'page_versions' AND column_name = 'search_vector'"
            )
        ).one()
        stored: Any = c.execute(
            text(
                "SELECT attgenerated::text FROM pg_attribute WHERE attrelid = "
                "'page_versions'::regclass AND attname = 'search_vector'"
            )
        ).scalar_one()
        index: Any = c.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_page_versions_search_vector'"
            )
        ).scalar_one()
        return (column[0], column[1], stored, index)

    is_generated, data_type, stored, index = db.run(_facts)
    assert (is_generated, data_type, stored) == ("ALWAYS", "tsvector", "s")
    assert "USING gin (search_vector)" in index


def test_search_vector_value_matches_the_orm_expression(db: SyncDb) -> None:
    """Semantic check of the generated expression (alembic only compares it as text)."""

    def _go(c: Connection) -> tuple[Any, ...]:
        _page(
            c,
            SOURCE,
            "s",
            title="Electric Vehicle Market",
            body="Batteries power vehicles. " * 3,
            description="India outlook",
        )
        _page(c, TARGET, "t", title="x" * 3000, body="y")  # title capped at 2000 chars
        return tuple(
            c.execute(
                text(
                    "SELECT pv.search_vector = ("
                    "setweight(to_tsvector('english', left(coalesce(pv.title, ''), 2000)), 'A') "
                    "|| setweight(to_tsvector('english', "
                    "left(coalesce(pv.meta_description, ''), 5000)), 'B') "
                    "|| setweight(to_tsvector('english', left(pv.body_text, 100000)), 'C')), "
                    "pv.search_vector @@ plainto_tsquery('english', 'electric vehicles'), "
                    "pv.search_vector @@ plainto_tsquery('english', 'india') "
                    "FROM page_versions pv ORDER BY pv.page_id"
                )
            ).all()
        )

    first, second = db.run(_go)
    assert tuple(first) == (True, True, True)
    assert second[0] is True


def test_search_vector_cannot_be_written(db: SyncDb) -> None:
    db.run(lambda c: _page(c, SOURCE, "s"))
    error = _fails(
        db,
        lambda c: c.execute(
            text(
                "INSERT INTO page_versions (id, page_id, version, content_hash, hash_version, "
                "source_type, source_id, requested_url, url, canonical_url, canonical_status, "
                "publication_state, body_html, body_text, word_count, internal_link_count, "
                "external_link_count, fetched_at, search_vector) VALUES (:id, :p, 2, :h, 1, "
                "'cms', 's', 'u', 'u', 'u', 'self', 'published', '', '', 0, 0, 0, now(), "
                "to_tsvector('x'))"
            ),
            {"id": uuid.uuid4(), "p": SOURCE, "h": HASH},
        ),
    )
    assert "non-default value" in str(error.orig).lower()


# --------------------------------------------------------------------------- #
# Plan content guard
# --------------------------------------------------------------------------- #
def _status(db: SyncDb, plan: uuid.UUID) -> Any:
    return db.run(
        lambda c: c.execute(
            text("SELECT status FROM link_plans WHERE id = :id"), {"id": plan}
        ).scalar_one()
    )


def _transition_row(c: Connection, plan: uuid.UUID, from_status: str, to_status: str) -> None:
    c.execute(
        text(
            "INSERT INTO link_plan_transitions (plan_id, from_status, to_status, actor) "
            "VALUES (:p, :f, :t, 'system:test')"
        ),
        {"p": plan, "f": from_status, "t": to_status},
    )


def test_a_raw_status_update_is_refused_at_commit(db: SyncDb) -> None:
    """Migration 0007 (ADR-0014 s8, Phase 3 QA M2): status moves only with a transition row
    written by the same transaction. The deferred trigger fires at COMMIT."""
    db.run(_seed)
    plan = db.run(_plan)
    assert PLAN_MUTABLE_COLUMNS == ("status", "updated_at")
    error = _fails(
        db,
        lambda c: c.execute(
            text(
                "UPDATE link_plans SET status = 'critiqued', updated_at = now() + interval '1 s' "
                "WHERE id = :id"
            ),
            {"id": plan},
        ),
    )
    assert getattr(error.orig, "sqlstate", None) == "55000"
    assert "no transition record" in str(error.orig)
    assert _status(db, plan) == "proposed"


def test_a_status_update_with_its_transition_row_commits(db: SyncDb) -> None:
    db.run(_seed)
    plan = db.run(_plan)

    def _go(c: Connection) -> None:
        c.execute(
            text("UPDATE link_plans SET status = 'critiqued', updated_at = now() WHERE id = :id"),
            {"id": plan},
        )
        _transition_row(c, plan, "proposed", "critiqued")  # after the UPDATE: still deferred

    db.run(_go)
    assert _status(db, plan) == "critiqued"


def test_a_transition_row_of_another_transaction_does_not_count(db: SyncDb) -> None:
    db.run(_seed)
    plan = db.run(_plan)
    db.run(lambda c: _transition_row(c, plan, "proposed", "critiqued"))  # committed earlier
    error = _fails(
        db,
        lambda c: c.execute(
            text("UPDATE link_plans SET status = 'critiqued' WHERE id = :id"), {"id": plan}
        ),
    )
    assert "no transition record" in str(error.orig)
    assert _status(db, plan) == "proposed"


def test_a_transition_row_must_match_from_and_to(db: SyncDb) -> None:
    db.run(_seed)
    plan = db.run(_plan)

    def _go(c: Connection) -> None:
        c.execute(text("UPDATE link_plans SET status = 'critiqued' WHERE id = :id"), {"id": plan})
        _transition_row(c, plan, "proposed", "policy_checked")

    error = _fails(db, _go)
    assert "no transition record" in str(error.orig)


def test_an_illegal_status_move_is_refused_even_with_a_row(db: SyncDb) -> None:
    db.run(_seed)
    plan = db.run(_plan)

    def _go(c: Connection) -> None:
        c.execute(text("UPDATE link_plans SET status = 'deployed' WHERE id = :id"), {"id": plan})
        _transition_row(c, plan, "proposed", "deployed")

    error = _fails(db, _go)
    assert "not an allowed transition" in str(error.orig)
    assert _status(db, plan) == "proposed"


def test_set_constraints_immediate_checks_at_statement_time(db: SyncDb) -> None:
    """Tests that roll back (and never commit) use SET CONSTRAINTS ALL IMMEDIATE."""
    db.run(_seed)
    plan = db.run(_plan)

    def _go(c: Connection) -> None:
        c.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        c.execute(text("UPDATE link_plans SET status = 'critiqued' WHERE id = :id"), {"id": plan})
        raise AssertionError("the UPDATE itself must fail")  # pragma: no cover

    error = _fails(db, _go)
    assert "no transition record" in str(error.orig)


def test_a_savepoint_transition_counts_for_the_top_level_transaction(db: SyncDb) -> None:
    """txid is pg_current_xact_id() (the TOP-LEVEL id), not xmin: a transition written inside
    a savepoint still matches the status change at commit."""
    db.run(_seed)
    plan = db.run(_plan)

    def _go(c: Connection) -> None:
        with c.begin_nested():
            c.execute(
                text("UPDATE link_plans SET status = 'critiqued' WHERE id = :id"), {"id": plan}
            )
            _transition_row(c, plan, "proposed", "critiqued")

    db.run(_go)
    assert _status(db, plan) == "critiqued"


def test_two_moves_in_one_transaction_each_need_their_row(db: SyncDb) -> None:
    db.run(_seed)
    plan = db.run(_plan)

    def _both(c: Connection) -> None:
        for a, b in (("proposed", "critiqued"), ("critiqued", "awaiting_approval")):
            c.execute(
                text("UPDATE link_plans SET status = :b WHERE id = :id"), {"b": b, "id": plan}
            )
            _transition_row(c, plan, a, b)

    db.run(_both)
    assert _status(db, plan) == "awaiting_approval"

    def _one_missing(c: Connection) -> None:
        c.execute(text("UPDATE link_plans SET status = 'approved' WHERE id = :id"), {"id": plan})
        _transition_row(c, plan, "awaiting_approval", "approved")
        c.execute(text("UPDATE link_plans SET status = 'validated' WHERE id = :id"), {"id": plan})

    error = _fails(db, _one_missing)
    assert "approved -> validated" in str(error.orig)
    assert _status(db, plan) == "awaiting_approval"


def test_the_trigger_transition_table_matches_allowed_transitions() -> None:
    """Parity: the LATEST literal table in a migration IS ``ALLOWED_TRANSITIONS``.

    The table is a frozen copy per revision: 0007 wrote the first one and 0010 replaced the
    guard with the ADR-0018 table. What must hold is that the table the guard runs TODAY
    equals the contract, and that 0010's "restore 0007" copy still equals 0007's -- otherwise
    a downgrade would install a transition set that never existed.
    """
    import importlib.util
    from pathlib import Path

    from ken_contracts.recommendations import ALLOWED_TRANSITIONS

    versions = Path(__file__).resolve().parents[2] / "migrations" / "versions"

    def load(name: str) -> object:
        spec = importlib.util.spec_from_file_location(f"migration_{name}", versions / name)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    m0007: Any = load("0007_controlled_deployment.py")
    m0010: Any = load("0010_qdrant_and_autonomy.py")

    expected = {(a.value, b.value) for a, targets in ALLOWED_TRANSITIONS.items() for b in targets}
    assert set(m0010.PLAN_TRANSITIONS_0010) == expected
    assert len(m0010.PLAN_TRANSITIONS_0010) == len(expected)
    # 0010's downgrade must restore EXACTLY what 0007 installed.
    assert set(m0010.PLAN_TRANSITIONS_0007) == set(m0007.PLAN_TRANSITIONS)
    assert len(m0010.PLAN_TRANSITIONS_0007) == len(m0007.PLAN_TRANSITIONS)
    # And the new table is a strict superset: ADR-0018 only ADDED moves.
    assert set(m0007.PLAN_TRANSITIONS) < expected


CONTENT_COLUMNS = {
    "id": "gen_random_uuid()",
    "run_id": "NULL",  # same value -> exercises IS DISTINCT FROM with NULL below
    "source_url": "'https://s.test/other'",
    "target_page_version": "target_page_version",  # no-op is allowed; changed below
    "target_title": "'changed'",
    "relationship_type": "'same_market'",
    "channels": "'[\"semantic\"]'::jsonb",
    "anchor_text": "'changed anchor'",
    "anchor_source": "'target_title'",
    "block_id": "'b9'",
    "block_kind": "'li'",
    "occurrence_index": "1",
    "start_offset": "1",
    "end_offset": "6",
    "context_hash": f"'{sha256(b'other').hexdigest()}'",
    "preview": '\'{"before": "x"}\'::jsonb',
    "score": "'{\"total\": 1}'::jsonb",
    "total_score": "0.9",
    "confidence": "0.9",
    "calibration_version": "'cal-1'",
    "reason_codes": "'[\"same_market\"]'::jsonb",
    "rationale": "'other'",
    "engine_version": "'other'",
    "policy_version": "2",
    "ranking_policy_version": "2",
    "embedding_model": "'other'",
    "reasoning_model": "'other'",
    "analysis_version": "'tax2-alg1'",
    "created_at": "now() - interval '1 day'",
}


@pytest.mark.parametrize(
    "column", sorted(c for c in CONTENT_COLUMNS if c not in {"run_id", "target_page_version"})
)
def test_every_content_column_is_immutable(db: SyncDb, column: str) -> None:
    db.run(_seed)
    plan = db.run(_plan)
    error = _fails(
        db,
        lambda c: c.execute(
            text(
                f"UPDATE link_plans SET {column} = {CONTENT_COLUMNS[column]}, "  # noqa: S608
                "status = 'critiqued' WHERE id = :id"
            ),
            {"id": plan},
        ),
    )
    assert getattr(error.orig, "sqlstate", None) == "55000"
    assert "immutable" in str(error.orig)
    status: Any = db.run(lambda c: c.execute(text("SELECT status FROM link_plans")).scalar_one())
    assert status == "proposed"


def test_foreign_key_and_status_content_columns_are_immutable(db: SyncDb) -> None:
    def _go(c: Connection) -> None:
        _seed(c)
        _version(c, TARGET, 2)
        _run(c)

    db.run(_go)
    plan = db.run(_plan)
    for assignment in (
        "target_page_version = 2",
        "source_page_id = :third",
        "target_page_id = :third",
        "run_id = :run",
        "supersedes = :id",
    ):

        def _update(c: Connection, a: str = assignment) -> None:
            c.execute(
                text(f"UPDATE link_plans SET {a} WHERE id = :id"),  # noqa: S608
                {"id": plan, "third": THIRD, "run": RUN},
            )

        error = _fails(db, _update)
        assert getattr(error.orig, "sqlstate", None) == "55000", assignment


def test_a_no_op_update_is_allowed(db: SyncDb) -> None:
    db.run(_seed)
    plan = db.run(_plan)
    db.run(
        lambda c: c.execute(
            text("UPDATE link_plans SET anchor_text = anchor_text WHERE id = :id"), {"id": plan}
        )
    )


def test_plans_cannot_be_deleted(db: SyncDb) -> None:
    db.run(_seed)
    db.run(_plan)
    error = _fails(db, lambda c: c.execute(text("DELETE FROM link_plans")))
    assert getattr(error.orig, "sqlstate", None) == "55000"
    assert db.run(lambda c: c.execute(text("SELECT count(*) FROM link_plans")).scalar_one()) == 1


def test_the_guard_is_generic_over_new_columns(db: SyncDb) -> None:
    """A column added later is immutable automatically (to_jsonb(NEW) - status - updated_at)."""

    def _go(c: Connection) -> None:
        _seed(c)
        plan = _plan(c)
        c.execute(text("ALTER TABLE link_plans ADD COLUMN later_column text DEFAULT 'a'"))
        c.execute(text("UPDATE link_plans SET later_column = 'b' WHERE id = :id"), {"id": plan})

    try:
        error = _fails(db, _go)  # the whole transaction (incl. the ALTER) rolls back
        assert getattr(error.orig, "sqlstate", None) == "55000"
    finally:
        columns = db.run(lambda c: {col["name"] for col in inspect(c).get_columns("link_plans")})
        assert "later_column" not in columns


# --------------------------------------------------------------------------- #
# Append-only tables
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE link_plan_transitions SET note = 'changed'",
        "DELETE FROM link_plan_transitions",
        "UPDATE link_candidates SET total_score = 0.1",
        "DELETE FROM link_candidates",
    ],
)
def test_transitions_and_candidates_are_append_only(db: SyncDb, statement: str) -> None:
    def _go(c: Connection) -> None:
        _seed(c)
        _run(c)
        plan = _plan(c)
        c.execute(
            text(
                "INSERT INTO link_plan_transitions (plan_id, from_status, to_status, actor) "
                "VALUES (:p, NULL, 'proposed', 'system:x')"
            ),
            {"p": plan},
        )
        c.execute(
            text(
                "INSERT INTO link_candidates (run_id, source_page_id, source_page_version, "
                "target_page_id, target_page_version, total_score, rejection, plan_id) "
                "VALUES (:r, :s, 1, :t, 1, 0.5, NULL, :p)"
            ),
            {"r": RUN, "s": SOURCE, "t": TARGET, "p": plan},
        )

    db.run(_go)
    assert set(APPEND_ONLY_RECOMMENDATION_TABLES) == {"link_plan_transitions", "link_candidates"}
    error = _fails(db, lambda c: c.execute(text(statement)))
    assert getattr(error.orig, "sqlstate", None) == "55000"
    assert "append-only" in str(error.orig)


def test_truncate_is_not_blocked_by_the_row_triggers(db: SyncDb) -> None:
    def _go(c: Connection) -> None:
        _seed(c)
        plan = _plan(c)
        c.execute(
            text(
                "INSERT INTO link_plan_transitions (plan_id, to_status, actor) "
                "VALUES (:p, 'proposed', 'system:x')"
            ),
            {"p": plan},
        )

    db.run(_go)
    db.run(lambda c: c.execute(text(TRUNCATE_ALL)))
    assert db.run(lambda c: c.execute(text("SELECT count(*) FROM link_plans")).scalar_one()) == 0


def test_candidate_kept_iff_planned_and_valid_rejection(db: SyncDb) -> None:
    def _base(c: Connection) -> None:
        _seed(c)
        _run(c)

    db.run(_base)
    insert = (
        "INSERT INTO link_candidates (run_id, source_page_id, source_page_version, "
        "target_page_id, target_page_version, rejection, plan_id) "
        "VALUES (:r, :s, 1, :t, 1, :rej, NULL)"
    )
    error = _fails(
        db, lambda c: c.execute(text(insert), {"r": RUN, "s": SOURCE, "t": TARGET, "rej": None})
    )
    assert "ck_link_candidates_kept_iff_planned" in str(error.orig)
    error = _fails(
        db, lambda c: c.execute(text(insert), {"r": RUN, "s": SOURCE, "t": TARGET, "rej": "nope"})
    )
    assert "ck_link_candidates_rejection_valid" in str(error.orig)
    db.run(
        lambda c: c.execute(
            text(insert), {"r": RUN, "s": SOURCE, "t": TARGET, "rej": "already_linked"}
        )
    )
    error = _fails(
        db,
        lambda c: c.execute(
            text(insert), {"r": RUN, "s": SOURCE, "t": TARGET, "rej": "already_linked"}
        ),
    )
    assert isinstance(error, IntegrityError)  # one row per (run, source, target)


# --------------------------------------------------------------------------- #
# Uniqueness and references
# --------------------------------------------------------------------------- #
def test_one_open_plan_per_pair(db: SyncDb) -> None:
    db.run(_seed)
    db.run(_plan)
    error = _fails(db, lambda c: _plan(c, start=10, end=12))
    assert isinstance(error, IntegrityError)
    assert "uq_link_plans_open_pair" in str(error.orig)
    # Closed plans do not occupy the pair.
    db.run(lambda c: _plan(c, status="rejected", start=20, end=22))
    db.run(lambda c: _plan(c, status="expired", start=30, end=32))


def test_one_open_plan_per_anchor_span(db: SyncDb) -> None:
    db.run(_seed)
    db.run(_plan)
    error = _fails(db, lambda c: _plan(c, target=THIRD))  # same context hash + start
    assert isinstance(error, IntegrityError)
    assert "uq_link_plans_open_span" in str(error.orig)
    db.run(lambda c: _plan(c, target=THIRD, start=1, end=5))  # overlap: rejected in code
    db.run(lambda c: _plan(c, target=THIRD, status="superseded"))


def test_a_plan_is_superseded_at_most_once(db: SyncDb) -> None:
    db.run(_seed)
    old = db.run(lambda c: _plan(c, status="superseded"))
    db.run(lambda c: _plan(c, supersedes=old, status="rejected", start=40, end=45))
    error = _fails(db, lambda c: _plan(c, supersedes=old, status="rejected", start=50, end=55))
    assert isinstance(error, IntegrityError)
    assert "uq_link_plans_supersedes" in str(error.orig)


def test_the_target_version_is_pinned_by_a_foreign_key(db: SyncDb) -> None:
    db.run(_seed)
    error = _fails(db, lambda c: _plan(c, tv=7))
    assert isinstance(error, IntegrityError)
    assert "fk_link_plans_target_page_id_target_page_version_page_versions" in str(error.orig)
    error = _fails(db, lambda c: _plan(c, sv=7))
    assert "fk_link_plans_source_page_id_source_page_version_page_versions" in str(error.orig)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"target": SOURCE}, "ck_link_plans_not_self"),
        ({"start": 5, "end": 5}, "ck_link_plans_offsets_valid"),
        ({"ctx": "nothex"}, "ck_link_plans_context_hash_sha256"),
        ({"status": "bogus"}, "ck_link_plans_status_valid"),
        ({"anchor": "x" * 121}, "ck_link_plans_anchor_length"),
    ],
)
def test_plan_checks(db: SyncDb, overrides: dict[str, Any], constraint: str) -> None:
    db.run(_seed)
    error = _fails(db, lambda c: _plan(c, **overrides))
    assert isinstance(error, IntegrityError)
    assert constraint in str(error.orig)


def test_transition_status_checks(db: SyncDb) -> None:
    db.run(_seed)
    plan = db.run(_plan)
    for from_status, to_status, actor, constraint in (
        (None, "bogus", "system:x", "ck_link_plan_transitions_to_status_valid"),
        ("bogus", "proposed", "system:x", "ck_link_plan_transitions_from_status_valid"),
        (None, "proposed", "", "ck_link_plan_transitions_actor_present"),
    ):

        def _insert(
            c: Connection, f: str | None = from_status, t: str = to_status, a: str = actor
        ) -> None:
            c.execute(
                text(
                    "INSERT INTO link_plan_transitions (plan_id, from_status, to_status, actor) "
                    "VALUES (:p, :f, :t, :a)"
                ),
                {"p": plan, "f": f, "t": t, "a": a},
            )

        error = _fails(db, _insert)
        assert constraint in str(error.orig)


def test_opportunities_are_replaceable_and_unique_per_page_and_kind(db: SyncDb) -> None:
    def _go(c: Connection) -> None:
        _seed(c)
        _run(c)
        c.execute(
            text(
                "INSERT INTO opportunities (run_id, page_id, kind, score, computed_at) "
                "VALUES (:r, :p, 'orphan', 0.5, now())"
            ),
            {"r": RUN, "p": TARGET},
        )

    db.run(_go)
    error = _fails(
        db,
        lambda c: c.execute(
            text(
                "INSERT INTO opportunities (run_id, page_id, kind, score, computed_at) "
                "VALUES (:r, :p, 'orphan', 0.7, now())"
            ),
            {"r": RUN, "p": TARGET},
        ),
    )
    assert isinstance(error, IntegrityError)
    db.run(lambda c: c.execute(text("UPDATE opportunities SET score = 0.9")))
    db.run(lambda c: c.execute(text("DELETE FROM opportunities")))


# --------------------------------------------------------------------------- #
# Downgrade
# --------------------------------------------------------------------------- #
def _state(c: Connection) -> dict[str, Any]:
    return {
        "tables": set(inspect(c).get_table_names()),
        "page_version_columns": {col["name"] for col in inspect(c).get_columns("page_versions")},
        "page_version_indexes": {str(i["name"]) for i in inspect(c).get_indexes("page_versions")},
        "triggers": set(
            c.execute(
                text("SELECT DISTINCT trigger_name FROM information_schema.triggers")
            ).scalars()
        ),
        "functions": set(
            c.execute(
                text(
                    "SELECT p.proname FROM pg_proc p JOIN pg_namespace n "
                    "ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
                    "AND p.proname LIKE 'ken_%'"
                )
            ).scalars()
        ),
        "revision": c.execute(text("SELECT version_num FROM alembic_version")).scalar(),
    }


def test_downgrade_to_0003_restores_exactly_the_intelligence_schema(
    alembic_config: Config, sync_db: SyncDb
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "0003_content_intelligence")
    try:
        state = sync_db.run(_state)
        assert not set(RECOMMENDATION_TABLES) & state["tables"]
        assert "search_vector" not in state["page_version_columns"]
        assert "ix_page_versions_search_vector" not in state["page_version_indexes"]
        assert "ix_page_versions_run_id" in state["page_version_indexes"]
        assert state["functions"] == {"ken_reject_mutation"}
        assert state["triggers"] == {
            "page_versions_immutable",
            "existing_links_immutable",
            "page_analyses_immutable",
            "page_embeddings_immutable",
        }
        assert state["revision"] == "0003_content_intelligence"
    finally:
        command.upgrade(alembic_config, "head")
    state = sync_db.run(_state)
    assert set(RECOMMENDATION_TABLES) <= state["tables"]
    assert "search_vector" in state["page_version_columns"]
    assert state["functions"] == {
        "ken_reject_mutation",
        "ken_link_plan_content_guard",
        "ken_agent_run_guard",
        "ken_user_guard",
        "ken_auth_session_guard",
        "ken_page_protection_guard",
        "ken_deployment_guard",
        "ken_link_plan_status_guard",
        "ken_analytics_writer_guard",
        # Phase 8 (migration 0009): six guard functions, and no SECURITY DEFINER anywhere.
        "ken_learning_content_guard",
        "ken_learning_proposal_guard",
        "ken_learning_promoter_guard",
        "ken_skill_guard",
        "ken_experiment_guard",
        "ken_experiment_assignment_guard",
        # Phase 9 (migration 0010): the autonomy decision guard.
        "ken_autonomy_decision_guard",
    }
    assert {
        "link_plans_content_immutable",
        "link_plans_no_delete",
        "link_plan_transitions_immutable",
        "link_candidates_immutable",
    } <= state["triggers"]
    assert state["revision"] == HEAD_REVISION
