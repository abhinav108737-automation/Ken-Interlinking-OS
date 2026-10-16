"""Migration 0007 (ADR-0014 s5 and s8) against the ``*_test`` database: the ``deployments``
guard (never deleted, mutable columns only, the state machine, frozen terminal rows), the
append-only items and verification runs, every CHECK constraint, the literal tables' parity
with the code, and an exact downgrade to 0006.

The link-plan status-integrity trigger itself is covered in test_recommendations_schema.py
(raw UPDATE refused at commit, savepoints, SET CONSTRAINTS ALL IMMEDIATE, parity).
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import uuid
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from ken_contracts.deployment import DeploymentStatus
from ken_core.db.models.deployment import (
    DEPLOYMENT_MUTABLE_COLUMNS,
    DEPLOYMENT_TRANSITIONS,
    FROZEN_DEPLOYMENT_STATUSES,
    ItemStage,
)
from tests.integration.conftest import (
    AUTONOMY_FUNCTIONS,
    AUTONOMY_TABLES,
    AUTONOMY_TRIGGERS,
    HEAD_REVISION,
    ANALYTICS_FUNCTIONS,
    ANALYTICS_TABLES,
    ANALYTICS_TRIGGERS,
    DEPLOYMENT_FUNCTIONS,
    DEPLOYMENT_TABLES,
    DEPLOYMENT_TRIGGERS,
    LEARNING_FUNCTIONS,
    LEARNING_TABLES,
    LEARNING_TRIGGERS,
    SyncDb,
)
from tests.integration.test_review_schema import (
    PLAN,
    SOURCE,
    TARGET,
    USER,
    _seed,
    _state,
)

pytestmark = pytest.mark.integration

FP = sha256(b"fingerprint").hexdigest()
D = DeploymentStatus
ALL = list(DeploymentStatus)
NOT_APPLIED = {D.APPLYING, D.FAILED, D.CONFLICT}


def _migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "0007_controlled_deployment.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0007_for_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(migrated: None, sync_db: SyncDb) -> SyncDb:
    sync_db.run(_seed)
    return sync_db


def _fails(db: SyncDb, fn: Callable[[Connection], Any]) -> DBAPIError:
    with pytest.raises(DBAPIError) as info:
        db.run(fn)
    return info.value


def _sqlstate(error: DBAPIError) -> str | None:
    origin: Any = error.orig
    for candidate in (origin, getattr(origin, "__cause__", None)):
        code = getattr(candidate, "sqlstate", None)
        if isinstance(code, str):
            return code
    return None


def _columns(status: DeploymentStatus, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "operation_id": uuid.uuid4(),
        "fp": FP,
        "req": '{"x":1}',
        "status": status.value,
        "actor": "user:otto",
        "user_id": USER,
        "applied": None if status in NOT_APPLIED else "now()",
        "from_status": D.APPLIED.value if status is D.ROLLING_BACK else None,
        "note": "",
        "preview": "[]",
        "error_code": None,
        "attempts": 1,
    }
    if "status_text" in overrides:
        values["status"] = overrides.pop("status_text")
    values.update(overrides)
    return values


def _insert(c: Connection, status: DeploymentStatus = D.APPLYING, **overrides: Any) -> uuid.UUID:
    v = _columns(status, **overrides)
    applied = v.pop("applied")
    c.execute(
        text(
            "INSERT INTO deployments (id, operation_id, request_fingerprint, request_json, "  # noqa: S608
            "cms_provider, cms_page_id, source_page_id, source_page_version, status, actor, "
            "user_id, note, preview, error_code, attempts, applied_at, rollback_from_status) "
            "VALUES (:id, :operation_id, :fp, :req, 'mock', 'cms-1', :source, 1, :status, "
            ":actor, :user_id, :note, CAST(:preview AS jsonb), :error_code, :attempts, "
            f"{'now()' if applied else 'NULL'}, :from_status)"
        ),
        {**v, "source": SOURCE},
    )
    return uuid.UUID(str(v["id"]))


def _item(c: Connection, deployment: uuid.UUID, **overrides: Any) -> None:
    values = {
        "d": deployment,
        "p": PLAN,
        "change": PLAN,
        "stage": ItemStage.REQUESTED,
        "fp": None,
        "codes": "[]",
    } | overrides
    c.execute(
        text(
            "INSERT INTO deployment_items (deployment_id, plan_id, change_id, stage, href, "
            "anchor_text, target_page_id, fingerprint, codes) VALUES (:d, :p, :change, :stage, "
            "'https://s.test/t', 'anchor', :t, :fp, CAST(:codes AS jsonb))"
        ),
        {**values, "t": TARGET},
    )


def _verification(c: Connection, deployment: uuid.UUID, **overrides: Any) -> None:
    values = {"d": deployment, "passed": True, "inconclusive": False, "checks": "[]"} | overrides
    c.execute(
        text(
            "INSERT INTO deployment_verifications (deployment_id, passed, inconclusive, "
            "http_checked, checks) VALUES (:d, :passed, :inconclusive, false, "
            "CAST(:checks AS jsonb))"
        ),
        values,
    )


# --------------------------------------------------------------------------- #
# Parity of the migration's literal tables with the code
# --------------------------------------------------------------------------- #
def test_the_literal_deployment_tables_match_the_code() -> None:
    module = _migration()
    code_pairs = {
        (a.value, b.value) for a, targets in DEPLOYMENT_TRANSITIONS.items() for b in targets
    }
    assert set(module.DEPLOYMENT_TRANSITIONS) == code_pairs
    assert len(module.DEPLOYMENT_TRANSITIONS) == len(code_pairs)
    assert set(module.FROZEN_STATUSES) == {s.value for s in FROZEN_DEPLOYMENT_STATUSES}
    assert tuple(module.DEPLOYMENT_MUTABLE_COLUMNS) == DEPLOYMENT_MUTABLE_COLUMNS
    assert set(DEPLOYMENT_TRANSITIONS) == set(DeploymentStatus)
    # The terminal statuses have no way out, and nothing else is terminal.
    assert {s for s, t in DEPLOYMENT_TRANSITIONS.items() if not t} == FROZEN_DEPLOYMENT_STATUSES
    # The stage vocabulary of the items table is the code's.
    assert set(ItemStage.ALL) == {s.strip("'") for s in module._STAGES.split(", ")}


# --------------------------------------------------------------------------- #
# deployments: the state machine and the guard
# --------------------------------------------------------------------------- #
def test_every_status_change_follows_the_state_machine(db: SyncDb) -> None:
    """All 56 (from, to) pairs: allowed ones commit, the rest are refused (55000)."""
    outcomes: dict[tuple[str, str], bool] = {}

    def _all(c: Connection) -> None:
        for a, b in itertools.permutations(ALL, 2):
            savepoint = c.begin_nested()
            try:
                row = _insert(c, a)
                c.execute(
                    text(
                        "UPDATE deployments SET status = :b, applied_at = "  # noqa: S608
                        f"{'NULL' if b in NOT_APPLIED else 'coalesce(applied_at, now())'}, "
                        "rollback_from_status = :f WHERE id = :id"
                    ),
                    {
                        "b": b.value,
                        "f": a.value if b is D.ROLLING_BACK else None,
                        "id": row,
                    },
                )
                outcomes[(a.value, b.value)] = True
            except DBAPIError as exc:
                assert _sqlstate(exc) in {"55000", "23514"}, (a, b, exc)
                outcomes[(a.value, b.value)] = False
            finally:
                savepoint.rollback()

    db.run(_all)
    allowed = {(a.value, b.value) for a, targets in DEPLOYMENT_TRANSITIONS.items() for b in targets}
    assert {pair for pair, ok in outcomes.items() if ok} == allowed
    assert len(outcomes) == 56


@pytest.mark.parametrize("status", sorted(FROZEN_DEPLOYMENT_STATUSES))
def test_a_finished_row_is_frozen(db: SyncDb, status: DeploymentStatus) -> None:
    row = db.run(lambda c: _insert(c, status))
    error = _fails(
        db,
        lambda c: c.execute(
            text("UPDATE deployments SET updated_at = now() + interval '1 s' WHERE id = :id"),
            {"id": row},
        ),
    )
    assert _sqlstate(error) == "55000"
    assert "frozen" in str(error.orig)


def test_deployments_are_never_deleted(db: SyncDb) -> None:
    row = db.run(lambda c: _insert(c, D.APPLIED))
    error = _fails(
        db, lambda c: c.execute(text("DELETE FROM deployments WHERE id = :id"), {"id": row})
    )
    assert _sqlstate(error) == "55000"
    assert "never deleted" in str(error.orig)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("operation_id", "gen_random_uuid()"),
        ("request_fingerprint", f"'{sha256(b'other').hexdigest()}'"),
        ("request_json", "'{}'"),
        ("cms_provider", "'http'"),
        ("cms_page_id", "'cms-2'"),
        ("actor", "'user:mallory'"),
        ("note", "'changed'"),
        ("preview", "'[{}]'::jsonb"),
        ("created_at", "now() - interval '1 day'"),
        ("source_page_version", "2"),
    ],
)
def test_immutable_columns_never_change(db: SyncDb, column: str, value: str) -> None:
    row = db.run(lambda c: _insert(c, D.APPLIED))
    error = _fails(
        db,
        lambda c: c.execute(
            text(f"UPDATE deployments SET {column} = {value} WHERE id = :id"),  # noqa: S608
            {"id": row},
        ),
    )
    assert _sqlstate(error) == "55000"
    assert "immutable" in str(error.orig)


def test_mutable_columns_may_change(db: SyncDb) -> None:
    row = db.run(lambda c: _insert(c, D.APPLIED))
    db.run(
        lambda c: c.execute(
            text(
                "UPDATE deployments SET new_version = 'v2', previous_version = 'v1', "
                "cms_deployment_id = 'dep-1', verified_at = now(), attempts = 2, "
                "error_code = 'cms_unavailable', updated_at = now() WHERE id = :id"
            ),
            {"id": row},
        )
    )
    found = db.run(
        lambda c: c.execute(
            text("SELECT new_version, attempts FROM deployments WHERE id = :id"), {"id": row}
        ).one()
    )
    assert tuple(found) == ("v2", 2)


def test_the_operation_id_is_unique(db: SyncDb) -> None:
    op = uuid.uuid4()
    db.run(lambda c: _insert(c, operation_id=op))
    error = _fails(db, lambda c: _insert(c, operation_id=op))
    assert _sqlstate(error) == "23505"
    assert "uq_deployments_operation_id" in str(error.orig)


def test_a_rollback_operation_id_is_unique(db: SyncDb) -> None:
    op = uuid.uuid4()

    def _two(c: Connection) -> None:
        for _ in range(2):
            row = _insert(c, D.APPLIED)
            c.execute(
                text("UPDATE deployments SET rollback_operation_id = :o WHERE id = :id"),
                {"o": op, "id": row},
            )

    error = _fails(db, _two)
    assert "uq_deployments_rollback_operation_id" in str(error.orig)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"fp": "not-a-sha"}, "ck_deployments_fingerprint_sha256"),
        ({"fp": FP.upper()}, "ck_deployments_fingerprint_sha256"),
        ({"actor": "system:deployment-service"}, "ck_deployments_actor_is_user"),
        ({"note": "x" * 501}, "ck_deployments_note_bounded"),
        ({"error_code": "Not A Code!"}, "ck_deployments_error_code_vocabulary"),
        ({"preview": "{}"}, "ck_deployments_preview_array"),
        ({"attempts": 0}, "ck_deployments_attempts_positive"),
        ({"status_text": "deployed", "applied": "now()"}, "ck_deployments_status_valid"),
    ],
)
def test_deployment_checks(db: SyncDb, overrides: dict[str, Any], constraint: str) -> None:
    error = _fails(db, lambda c: _insert(c, D.APPLYING, **overrides))
    assert _sqlstate(error) == "23514"
    assert constraint in str(error.orig)


def test_applied_at_is_set_iff_the_change_reached_the_page(db: SyncDb) -> None:
    error = _fails(db, lambda c: _insert(c, D.APPLYING, applied="now()"))
    assert "ck_deployments_applied_at_iff_applied" in str(error.orig)
    error = _fails(db, lambda c: _insert(c, D.APPLIED, applied=None))
    assert "ck_deployments_applied_at_iff_applied" in str(error.orig)


def test_rolling_back_iff_its_from_status_is_recorded(db: SyncDb) -> None:
    error = _fails(db, lambda c: _insert(c, D.ROLLING_BACK, from_status=None))
    assert "ck_deployments_rolling_back_iff_from_status" in str(error.orig)
    error = _fails(db, lambda c: _insert(c, D.APPLIED, from_status="applied"))
    assert "ck_deployments_rolling_back_iff_from_status" in str(error.orig)


def test_rollback_columns_are_checked(db: SyncDb) -> None:
    row = db.run(lambda c: _insert(c, D.APPLIED))
    for sql, constraint in (
        ("rollback_reason = 'because'", "ck_deployments_rollback_reason_valid"),
        ("rollback_actor = 'agent:x'", "ck_deployments_rollback_actor_is_user"),
        ("rollback_fingerprint = 'x'", "ck_deployments_rollback_fingerprint_sha256"),
        ("rollback_note = repeat('x', 501)", "ck_deployments_rollback_note_bounded"),
        ("rollback_plan_ids = '{}'::jsonb", "ck_deployments_rollback_plan_ids_array"),
    ):

        def _update(c: Connection, sql: str = sql) -> Any:
            return c.execute(
                text(f"UPDATE deployments SET {sql} WHERE id = :id"),  # noqa: S608
                {"id": row},
            )

        error = _fails(db, _update)
        assert constraint in str(error.orig), sql


# --------------------------------------------------------------------------- #
# Append-only items and verification runs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE deployment_items SET stage = 'applied'",
        "DELETE FROM deployment_items",
        "UPDATE deployment_verifications SET passed = false",
        "DELETE FROM deployment_verifications",
    ],
)
def test_items_and_verifications_are_append_only(db: SyncDb, sql: str) -> None:
    def _rows(c: Connection) -> None:
        row = _insert(c, D.APPLIED)
        _item(c, row)
        _verification(c, row)

    db.run(_rows)
    error = _fails(db, lambda c: c.execute(text(sql)))
    assert _sqlstate(error) == "55000"


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"change": TARGET}, "ck_deployment_items_change_id_is_plan_id"),
        ({"stage": "deployed"}, "ck_deployment_items_stage_valid"),
        ({"fp": "nope"}, "ck_deployment_items_fingerprint_sha256"),
        ({"codes": "{}"}, "ck_deployment_items_codes_array"),
    ],
)
def test_item_checks(db: SyncDb, overrides: dict[str, Any], constraint: str) -> None:
    row = db.run(lambda c: _insert(c, D.APPLIED))
    error = _fails(db, lambda c: _item(c, row, **overrides))
    assert constraint in str(error.orig)


def test_a_verification_run_is_never_both_passed_and_inconclusive(db: SyncDb) -> None:
    row = db.run(lambda c: _insert(c, D.APPLIED))
    error = _fails(db, lambda c: _verification(c, row, inconclusive=True))
    assert "ck_deployment_verifications_passed_xor_inconclusive" in str(error.orig)
    error = _fails(db, lambda c: _verification(c, row, checks=json.dumps({})))
    assert "ck_deployment_verifications_checks_array" in str(error.orig)


def test_foreign_keys_restrict(db: SyncDb) -> None:
    error = _fails(db, lambda c: _insert(c, user_id=uuid.uuid4()))
    assert _sqlstate(error) == "23503"

    def _orphan_item(c: Connection) -> None:
        _item(c, uuid.uuid4())

    assert _sqlstate(_fails(db, _orphan_item)) == "23503"


# --------------------------------------------------------------------------- #
# txid and the downgrade
# --------------------------------------------------------------------------- #
def test_transition_rows_record_the_top_level_transaction(db: SyncDb) -> None:
    def _go(c: Connection) -> tuple[Any, Any]:
        with c.begin_nested():
            c.execute(
                text(
                    "INSERT INTO link_plan_transitions (plan_id, from_status, to_status, actor) "
                    "VALUES (:p, 'proposed', 'rejected', 'system:test')"
                ),
                {"p": PLAN},
            )
        stored: Any = c.execute(
            text("SELECT txid FROM link_plan_transitions WHERE plan_id = :p"), {"p": PLAN}
        ).scalar_one()
        return stored, c.execute(text("SELECT pg_current_xact_id()")).scalar_one()

    stored, current = db.run(_go)
    assert str(stored) == str(current)


def test_downgrade_to_0006_restores_exactly_the_review_schema(
    alembic_config: Config, migrated: None, sync_db: SyncDb
) -> None:
    head = sync_db.run(_state)

    def _txid(c: Connection) -> bool:
        return bool(
            c.execute(
                text(
                    "SELECT 1 FROM information_schema.columns WHERE table_name = "
                    "'link_plan_transitions' AND column_name = 'txid'"
                )
            ).scalar()
        )

    assert sync_db.run(_txid)
    command.downgrade(alembic_config, "0006_review_control_plane")
    try:
        state = sync_db.run(_state)
        assert (
            state["tables"]
            == head["tables"] - DEPLOYMENT_TABLES - ANALYTICS_TABLES - LEARNING_TABLES - AUTONOMY_TABLES
        )
        assert (
            state["functions"]
            == head["functions"] - DEPLOYMENT_FUNCTIONS - ANALYTICS_FUNCTIONS - LEARNING_FUNCTIONS - AUTONOMY_FUNCTIONS
        )
        assert (
            state["triggers"]
            == head["triggers"] - DEPLOYMENT_TRIGGERS - ANALYTICS_TRIGGERS - LEARNING_TRIGGERS - AUTONOMY_TRIGGERS
        )
        assert state["revision"] == "0006_review_control_plane"
        assert not sync_db.run(_txid)

        # 0006 semantics again: a raw status UPDATE commits (no integrity trigger below 0007).
        def _raw(c: Connection) -> Any:
            _seed(c)
            c.execute(
                text("UPDATE link_plans SET status = 'superseded' WHERE id = :p"), {"p": PLAN}
            )
            return c.execute(
                text("SELECT status FROM link_plans WHERE id = :p"), {"p": PLAN}
            ).scalar()

        assert sync_db.run(_raw) == "superseded"
    finally:
        sync_db.run(
            lambda c: c.execute(text("TRUNCATE link_plans, page_versions, pages, users CASCADE"))
        )
        command.upgrade(alembic_config, "head")
    state = sync_db.run(_state)
    assert state == head
    assert state["revision"] == HEAD_REVISION
