"""Migration 0005 (agent runs) against the ``*_test`` database.

Structure matches the ORM; every lifecycle CHECK; one running run per source; the
``agent_runs`` guard (no DELETE, finished rows frozen, only the mutable columns change);
append-only steps and reviews; the bounded step payload; an exact downgrade to 0004.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, Connection, UniqueConstraint, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError, IntegrityError

import ken_core.db.models  # noqa: F401 -- registers every model
from ken_contracts.agents import MAX_STEP_OUTPUT_BYTES
from ken_core.db.base import Base
from ken_core.db.models.agents import AGENT_RUN_MUTABLE_COLUMNS, APPEND_ONLY_AGENT_TABLES
from tests.integration.conftest import DEPLOYMENT_TABLES, DEPLOYMENT_TRIGGERS, SyncDb

pytestmark = pytest.mark.integration

AGENT_TABLES = ("agent_runs", "agent_steps", "agent_run_plans")
REVIEW_TABLES = ("users", "auth_sessions", "review_actions")
"""Migration 0006 (Phase 5): gone again below it."""
REVIEW_TRIGGERS = frozenset(
    {
        "users_guard",
        "users_no_delete",
        "auth_sessions_guard",
        "auth_sessions_prune_only",
        "page_protections_guard",
        "page_protections_no_delete",
        "review_actions_immutable",
    }
)
HASH = sha256(b"x").hexdigest()
CTX = sha256(b"block").hexdigest()
SOURCE = uuid.UUID("00000000-0000-4000-8000-000000000201")
TARGET = uuid.UUID("00000000-0000-4000-8000-000000000202")
OTHER = uuid.UUID("00000000-0000-4000-8000-000000000203")
JOB = uuid.UUID("00000000-0000-4000-8000-000000000204")
SPAN = "0123456789abcdef"
T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def _at(seconds: int) -> datetime:
    return datetime.fromtimestamp(T0.timestamp() + seconds, tz=UTC)


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


def _exec(c: Connection, *, sql: str, params: dict[str, Any] | None = None) -> None:
    c.execute(text(sql), params or {})


def _immutable(error: DBAPIError) -> bool:
    return _sqlstate(error) == "55000"


def _page(c: Connection, page_id: uuid.UUID, path: str) -> None:
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
            "publication_state, title, body_html, body_text, word_count, "
            "internal_link_count, external_link_count, fetched_at) VALUES (:id, :p, 1, :h, 1, "
            "'cms', 's', 'u', 'u', 'u', 'self', 'published', 't', '', '', 0, 0, 0, now())"
        ),
        {"id": uuid.uuid4(), "p": page_id, "h": HASH},
    )


def _plan(c: Connection, target: uuid.UUID = TARGET) -> uuid.UUID:
    plan_id = uuid.uuid4()
    c.execute(
        text(
            "INSERT INTO link_plans (id, source_page_id, source_page_version, source_url, "
            "target_page_id, target_page_version, target_url, anchor_text, anchor_source, "
            "block_id, block_kind, occurrence_index, start_offset, end_offset, context_hash, "
            "total_score, confidence, rationale, engine_version, policy_version, "
            "ranking_policy_version, analysis_version, status) VALUES (:id, :s, 1, "
            "'https://s.test/s', :t, 1, 'https://s.test/t', 'anchor', 'target_topic', 'b0', "
            "'p', 0, 0, 6, :ctx, 0.5, 0.5, 'r', 'e', 1, 1, 'tax1-alg1', 'proposed')"
        ),
        {"id": plan_id, "s": SOURCE, "t": target, "ctx": CTX},
    )
    return plan_id


def _seed(c: Connection) -> None:
    _page(c, SOURCE, "s")
    _page(c, TARGET, "t")
    _page(c, OTHER, "o")
    c.execute(
        text(
            "INSERT INTO job_runs (id, kind, status, triggered_by) "
            "VALUES (:id, 'agent', 'running', 't')"
        ),
        {"id": JOB},
    )


def _run(c: Connection, **overrides: Any) -> uuid.UUID:
    correlation = overrides.pop("correlation", uuid.uuid4())
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "job": JOB,
        "source": SOURCE,
        "trigger": "batch",
        "status": "running",
        "outcome": None,
        "triage": None,
        "notes": "[]",
        "corr": correlation,
        "trace": correlation.hex,
        "error": None,
        "started": T0,
        "finished": None,
    } | overrides
    c.execute(
        text(
            "INSERT INTO agent_runs (id, job_run_id, source_page_id, source_url, trigger, "
            "status, outcome, triage_decision, review_notes, correlation_id, trace_id, "
            "graph_version, error_code, started_at, finished_at) VALUES (:id, :job, :source, "
            "'https://s.test/s', :trigger, :status, :outcome, :triage, CAST(:notes AS jsonb), "
            ":corr, :trace, 'graph-v1', :error, :started, :finished)"
        ),
        values,
    )
    return uuid.UUID(str(values["id"]))


def _finish(c: Connection, run_id: uuid.UUID, status: str = "completed",
            outcome: str = "no_action") -> None:  # fmt: skip
    c.execute(
        text(
            "UPDATE agent_runs SET status = :s, outcome = :o, finished_at = now(), "
            "updated_at = now() WHERE id = :id"
        ),
        {"s": status, "o": outcome, "id": run_id},
    )


def _step(c: Connection, run_id: uuid.UUID, run_trace: str, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "run": run_id,
        "seq": 0,
        "node": "load_page",
        "status": "succeeded",
        "attempt": 1,
        "duration": 5,
        "role": None,
        "model": None,
        "fb": False,
        "fbr": None,
        "error": None,
        "tools": "[]",
        "output": "{}",
        "trace": run_trace,
        "span": SPAN,
        "started": T0,
        "finished": _at(1),
    } | overrides
    c.execute(
        text(
            "INSERT INTO agent_steps (run_id, seq, node, status, attempt, started_at, "
            "finished_at, duration_ms, model_role, model, fallback_used, fallback_reason, "
            "error_code, tool_calls, output, trace_id, span_id) VALUES (:run, :seq, :node, "
            ":status, :attempt, :started, :finished, "
            ":duration, :role, :model, :fb, :fbr, :error, CAST(:tools AS jsonb), "
            "CAST(:output AS jsonb), :trace, :span)"
        ),
        values,
    )


def _review(c: Connection, run_id: uuid.UUID, plan_id: uuid.UUID, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "run": run_id,
        "plan": plan_id,
        "verdict": "accept",
        "reasons": "[]",
        "explanation": "",
        "handed": True,
        "fb": False,
        "fbr": None,
        "model": None,
    } | overrides
    c.execute(
        text(
            "INSERT INTO agent_run_plans (run_id, plan_id, verdict, reasons, explanation, "
            "handed_off, fallback_used, fallback_reason, model, reviewed_at) VALUES (:run, "
            ":plan, :verdict, CAST(:reasons AS jsonb), :explanation, :handed, :fb, :fbr, "
            ":model, now())"
        ),
        values,
    )


def _trace(c: Connection, run_id: uuid.UUID) -> str:
    return str(
        c.execute(text("SELECT trace_id FROM agent_runs WHERE id = :id"), {"id": run_id}).scalar()
    )


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
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
                "pk": inspector.get_pk_constraint(table)["name"],
            }
            for table in AGENT_TABLES
        }

    reflected = db.run(_reflect)
    name: Any = postgresql.dialect().identifier_preparer.format_constraint  # type: ignore[no-untyped-call]
    for table in AGENT_TABLES:
        orm = Base.metadata.tables[table]
        checks = {name(c) for c in orm.constraints if isinstance(c, CheckConstraint)}
        assert checks, table
        assert reflected[table]["checks"] == checks, table
        assert reflected[table]["uniques"] == {
            name(c) for c in orm.constraints if isinstance(c, UniqueConstraint)
        }, table
        assert reflected[table]["fks"] == {name(fk) for fk in orm.foreign_key_constraints}, table
        assert reflected[table]["indexes"] == {name(i) for i in orm.indexes}, table
        assert reflected[table]["pk"] == orm.primary_key.name, table
    assert reflected["agent_runs"]["uniques"] == {"uq_agent_runs_correlation_id"}
    assert reflected["agent_run_plans"]["uniques"] == {"uq_agent_run_plans_plan_id"}
    assert reflected["agent_steps"]["uniques"] == {"uq_agent_steps_run_id_seq"}


def test_foreign_keys_restrict_deletes(db: SyncDb) -> None:
    def _rules(c: Connection) -> set[tuple[str, str]]:
        rows = c.execute(
            text(
                "SELECT tc.constraint_name, rc.delete_rule FROM information_schema."
                "table_constraints tc JOIN information_schema.referential_constraints rc "
                "ON rc.constraint_name = tc.constraint_name WHERE tc.table_name IN "
                "('agent_runs', 'agent_steps', 'agent_run_plans')"
            )
        ).all()
        return {(str(r[0]), str(r[1])) for r in rows}

    rules = db.run(_rules)
    assert len(rules) == 5
    assert {rule for _, rule in rules} == {"RESTRICT"}
    db.run(lambda c: _run(c))
    error = _fails(
        db, lambda c: c.execute(text("DELETE FROM job_runs WHERE id = :id"), {"id": JOB})
    )
    assert isinstance(error, IntegrityError)


def test_running_index_is_partial_and_unique(db: SyncDb) -> None:
    definition: str = db.run(
        lambda c: c.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_agent_runs_running_source'")
        ).scalar_one()
    )
    assert definition.startswith("CREATE UNIQUE INDEX")
    assert definition.endswith("WHERE (status = 'running'::text)")
    started: str = db.run(
        lambda c: c.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_agent_runs_started_at'")
        ).scalar_one()
    )
    assert "started_at DESC" in started


def test_triggers_and_guard_function_exist(db: SyncDb) -> None:
    def _triggers(c: Connection) -> set[tuple[str, str, str]]:
        rows = c.execute(
            text(
                "SELECT trigger_name, event_manipulation, event_object_table "
                "FROM information_schema.triggers WHERE event_object_table IN "
                "('agent_runs', 'agent_steps', 'agent_run_plans')"
            )
        ).all()
        return {(str(r[0]), str(r[1]), str(r[2])) for r in rows}

    assert db.run(_triggers) == {
        ("agent_runs_guard", "UPDATE", "agent_runs"),
        ("agent_runs_no_delete", "DELETE", "agent_runs"),
        *(
            (f"{table}_immutable", op, table)
            for table in APPEND_ONLY_AGENT_TABLES
            for op in ("UPDATE", "DELETE")
        ),
    }


# --------------------------------------------------------------------------- #
# agent_runs: identity, uniqueness, lifecycle CHECKs
# --------------------------------------------------------------------------- #
def test_trace_id_must_be_the_correlation_id_hex(db: SyncDb) -> None:
    correlation = uuid.uuid4()
    for bad in (str(correlation), correlation.hex.upper(), uuid.uuid4().hex, ""):
        error = _fails(db, partial(_run, correlation=correlation, trace=bad))
        assert isinstance(error, IntegrityError), bad
        assert "ck_agent_runs_trace_id_hex" in str(error)
    run_id = db.run(lambda c: _run(c, correlation=correlation))
    assert db.run(lambda c: _trace(c, run_id)) == correlation.hex


def test_correlation_id_is_unique(db: SyncDb) -> None:
    correlation = uuid.uuid4()
    first = db.run(lambda c: _run(c, correlation=correlation))
    db.run(lambda c: _finish(c, first))
    error = _fails(db, lambda c: _run(c, correlation=correlation, source=OTHER))
    assert isinstance(error, IntegrityError)
    assert "uq_agent_runs_correlation_id" in str(error)


def test_at_most_one_running_run_per_source(db: SyncDb) -> None:
    first = db.run(lambda c: _run(c))
    error = _fails(db, lambda c: _run(c))
    assert isinstance(error, IntegrityError)
    assert "uq_agent_runs_running_source" in str(error)
    db.run(lambda c: _run(c, source=OTHER))  # another source is independent
    db.run(lambda c: _finish(c, first))
    second = db.run(lambda c: _run(c))  # a finished run no longer blocks
    db.run(lambda c: _finish(c, second))
    count = db.run(
        lambda c: c.execute(
            text("SELECT count(*) FROM agent_runs WHERE source_page_id = :s"), {"s": SOURCE}
        ).scalar()
    )
    assert count == 2


VALID_FINISHED = [
    ("awaiting_approval", "recommend", None),
    ("awaiting_approval", "needs_human_review", None),
    ("completed", "needs_human_review", "handoff_conflict"),
    ("completed", "no_action", None),
    ("failed", "failed", "page_not_found"),
]


@pytest.mark.parametrize(("status", "outcome", "error_code"), VALID_FINISHED)
def test_valid_finished_rows_are_accepted(
    db: SyncDb, status: str, outcome: str, error_code: str | None
) -> None:
    db.run(lambda c: _run(c, status=status, outcome=outcome, error=error_code, finished=_at(5)))


FINISHED = _at(5)
INVALID_RUNS: list[tuple[str, dict[str, Any]]] = [
    ("ck_agent_runs_trigger_valid", {"trigger": "cron"}),
    ("ck_agent_runs_status_valid",
     {"status": "done", "outcome": "needs_human_review", "finished": FINISHED}),
    ("ck_agent_runs_outcome_valid",
     {"status": "completed", "outcome": "win", "finished": FINISHED}),
    ("ck_agent_runs_triage_decision_valid", {"triage": "panic"}),
    ("ck_agent_runs_error_code_valid", {"error": "oops"}),
    ("ck_agent_runs_finished_iff_not_running", {"finished": FINISHED}),
    ("ck_agent_runs_finished_iff_not_running", {"status": "completed", "outcome": "no_action"}),
    ("ck_agent_runs_outcome_iff_finished", {"outcome": "needs_human_review"}),
    ("ck_agent_runs_outcome_iff_finished", {"status": "completed", "finished": FINISHED}),
    ("ck_agent_runs_failed_iff_failed",
     {"status": "failed", "outcome": "no_action", "error": "internal", "finished": FINISHED}),
    ("ck_agent_runs_failed_iff_failed",
     {"status": "completed", "outcome": "failed", "error": "internal", "finished": FINISHED}),
    ("ck_agent_runs_recommend_is_handed_off",
     {"status": "completed", "outcome": "recommend", "finished": FINISHED}),
    ("ck_agent_runs_no_action_is_completed",
     {"status": "awaiting_approval", "outcome": "no_action", "finished": FINISHED}),
    ("ck_agent_runs_failed_has_code", {"status": "failed", "outcome": "failed",
                                       "finished": FINISHED}),
    ("ck_agent_runs_finished_after_started",
     {"status": "completed", "outcome": "no_action", "finished": _at(-1)}),
    ("ck_agent_runs_review_notes_bounded", {"notes": json.dumps(["n"] * 21)}),
    ("ck_agent_runs_review_notes_bounded", {"notes": '{"a": 1}'}),
]  # fmt: skip


@pytest.mark.parametrize(("constraint", "overrides"), INVALID_RUNS)
def test_lifecycle_checks_reject_inconsistent_runs(
    db: SyncDb, constraint: str, overrides: dict[str, Any]
) -> None:
    error = _fails(db, lambda c: _run(c, **overrides))
    assert isinstance(error, IntegrityError)
    assert constraint in str(error)


def test_twenty_review_notes_are_allowed(db: SyncDb) -> None:
    db.run(lambda c: _run(c, notes=json.dumps(["n"] * 20)))


# --------------------------------------------------------------------------- #
# agent_runs guard
# --------------------------------------------------------------------------- #
def test_a_running_row_changes_only_its_mutable_columns(db: SyncDb) -> None:
    run_id = db.run(lambda c: _run(c))
    db.run(
        lambda c: c.execute(
            text(
                "UPDATE agent_runs SET triage_decision = 'optimize', source_page_version = 1, "
                "review_notes = '[\"look\"]'::jsonb, updated_at = now() WHERE id = :id"
            ),
            {"id": run_id},
        )
    )
    immutable = {
        "job_run_id": "gen_random_uuid()",
        "source_page_id": f"'{OTHER}'::uuid",
        "source_url": "'https://s.test/x'",
        "trigger": "'manual'",
        "graph_version": "'graph-v2'",
        "config_snapshot": "'{\"x\": 1}'::jsonb",
        "started_at": "started_at - interval '1 second'",
        "created_at": "now()",
        "id": "gen_random_uuid()",
    }
    columns = {col.name for col in Base.metadata.tables["agent_runs"].columns}
    assert set(immutable) | set(AGENT_RUN_MUTABLE_COLUMNS) | {"correlation_id", "trace_id"} == (
        columns
    )
    for column, value in immutable.items():
        sql = f"UPDATE agent_runs SET {column} = {value} WHERE id = :id"  # noqa: S608
        error = _fails(db, partial(_exec, sql=sql, params={"id": run_id}))
        assert _immutable(error), column
    # correlation_id and trace_id move together (else the CHECK fires first): still immutable.
    new = uuid.uuid4()
    error = _fails(
        db,
        lambda c: c.execute(
            text("UPDATE agent_runs SET correlation_id = :c, trace_id = :t WHERE id = :id"),
            {"c": new, "t": new.hex, "id": run_id},
        ),
    )
    assert _immutable(error)
    # A no-op UPDATE of an immutable column is allowed (nothing changes).
    db.run(
        lambda c: c.execute(
            text("UPDATE agent_runs SET source_url = source_url WHERE id = :id"), {"id": run_id}
        )
    )


@pytest.mark.parametrize(("status", "outcome", "error_code"), VALID_FINISHED)
def test_a_finished_row_is_frozen(
    db: SyncDb, status: str, outcome: str, error_code: str | None
) -> None:
    run_id = db.run(lambda c: _run(c))
    db.run(
        lambda c: c.execute(
            text(
                "UPDATE agent_runs SET status = :s, outcome = :o, error_code = :e, "
                "finished_at = now(), updated_at = now() WHERE id = :id"
            ),
            {"s": status, "o": outcome, "e": error_code, "id": run_id},
        )
    )
    for statement in (
        "UPDATE agent_runs SET updated_at = now() WHERE id = :id",
        "UPDATE agent_runs SET review_notes = '[\"late\"]'::jsonb WHERE id = :id",
        "UPDATE agent_runs SET status = 'running', outcome = NULL, finished_at = NULL "
        "WHERE id = :id",
        "UPDATE agent_runs SET source_url = source_url WHERE id = :id",
    ):
        error = _fails(db, partial(_exec, sql=statement, params={"id": run_id}))
        assert _immutable(error), statement


def test_agent_runs_are_never_deleted(db: SyncDb) -> None:
    running = db.run(lambda c: _run(c))
    finished = db.run(lambda c: _run(c, source=OTHER))
    db.run(lambda c: _finish(c, finished))
    for run_id in (running, finished):
        error = _fails(
            db,
            partial(_exec, sql="DELETE FROM agent_runs WHERE id = :id", params={"id": run_id}),
        )
        assert _immutable(error)


# --------------------------------------------------------------------------- #
# agent_steps
# --------------------------------------------------------------------------- #
def test_steps_are_append_only(db: SyncDb) -> None:
    run_id = db.run(lambda c: _run(c))
    trace = db.run(lambda c: _trace(c, run_id))
    db.run(lambda c: _step(c, run_id, trace))
    for statement in ("UPDATE agent_steps SET duration_ms = 9", "DELETE FROM agent_steps"):
        error = _fails(db, partial(_exec, sql=statement))
        assert _immutable(error), statement
    error = _fails(db, lambda c: _step(c, run_id, trace))  # (run_id, seq) is unique
    assert isinstance(error, IntegrityError)
    assert "uq_agent_steps_run_id_seq" in str(error)


def test_step_payload_is_bounded(db: SyncDb) -> None:
    run_id = db.run(lambda c: _run(c))
    trace = db.run(lambda c: _trace(c, run_id))
    # jsonb::text renders {"x": "<n chars>"} as n + 9 bytes and ["<n chars>"] as n + 4.
    fits_output = json.dumps({"x": "a" * (MAX_STEP_OUTPUT_BYTES - 9)})
    fits_tools = json.dumps(["a" * (MAX_STEP_OUTPUT_BYTES - 4)])
    db.run(lambda c: _step(c, run_id, trace, seq=0, output=fits_output, tools=fits_tools))
    sizes = db.run(
        lambda c: tuple(
            c.execute(
                text(
                    "SELECT octet_length(output::text), octet_length(tool_calls::text) "
                    "FROM agent_steps WHERE run_id = :r"
                ),
                {"r": run_id},
            ).one()
        )
    )
    assert sizes == (MAX_STEP_OUTPUT_BYTES, MAX_STEP_OUTPUT_BYTES)
    too_big_output = json.dumps({"x": "a" * (MAX_STEP_OUTPUT_BYTES - 8)})
    too_big_tools = json.dumps(["a" * (MAX_STEP_OUTPUT_BYTES - 3)])
    cases: list[dict[str, Any]] = [{"output": too_big_output}, {"tools": too_big_tools}]
    for overrides in cases:
        error = _fails(db, partial(_step, run_id=run_id, run_trace=trace, seq=1, **overrides))
        assert isinstance(error, IntegrityError)
        assert "ck_agent_steps_payload_bounded" in str(error)


INVALID_STEPS: list[tuple[str, dict[str, Any]]] = [
    ("ck_agent_steps_node_valid", {"node": "improvise"}),
    ("ck_agent_steps_status_valid", {"status": "maybe"}),
    ("ck_agent_steps_model_role_valid", {"role": "oracle"}),
    ("ck_agent_steps_model_has_role", {"model": "qwen"}),
    ("ck_agent_steps_fallback_reason_valid", {"fb": True, "fbr": "bored"}),
    ("ck_agent_steps_fallback_reason_iff", {"fb": True}),
    ("ck_agent_steps_fallback_reason_iff", {"fbr": "model_disabled"}),
    ("ck_agent_steps_error_code_valid", {"error": "oops"}),
    ("ck_agent_steps_counts_valid", {"seq": -1}),
    ("ck_agent_steps_counts_valid", {"attempt": 0}),
    ("ck_agent_steps_counts_valid", {"duration": -1}),
    ("ck_agent_steps_finished_after_started", {"finished": _at(-1)}),
    ("ck_agent_steps_trace_id_format", {"trace": "A" * 32}),
    ("ck_agent_steps_span_id_format", {"span": "0123456789ABCDEF"}),
    ("ck_agent_steps_span_id_format", {"span": "0123"}),
]


@pytest.mark.parametrize(("constraint", "overrides"), INVALID_STEPS)
def test_step_checks(db: SyncDb, constraint: str, overrides: dict[str, Any]) -> None:
    run_id = db.run(lambda c: _run(c))
    trace = db.run(lambda c: _trace(c, run_id))
    error = _fails(db, lambda c: _step(c, run_id, trace, **overrides))
    assert isinstance(error, IntegrityError)
    assert constraint in str(error)


def test_a_valid_model_step_is_accepted(db: SyncDb) -> None:
    run_id = db.run(lambda c: _run(c))
    trace = db.run(lambda c: _trace(c, run_id))
    db.run(lambda c: _step(c, run_id, trace, node="critique", role="reasoning", model="m"))
    db.run(
        lambda c: _step(
            c, run_id, trace, seq=1, node="plan", role="fast", fb=True, fbr="model_disabled"
        )
    )


# --------------------------------------------------------------------------- #
# agent_run_plans
# --------------------------------------------------------------------------- #
def test_reviews_are_append_only_and_a_plan_is_reviewed_once(db: SyncDb) -> None:
    run_id = db.run(lambda c: _run(c))
    plan_id = db.run(_plan)
    db.run(lambda c: _review(c, run_id, plan_id))
    for statement in (
        "UPDATE agent_run_plans SET handed_off = false",
        "DELETE FROM agent_run_plans",
    ):
        error = _fails(db, partial(_exec, sql=statement))
        assert _immutable(error), statement
    db.run(lambda c: _finish(c, run_id))
    other_run = db.run(lambda c: _run(c))
    error = _fails(db, lambda c: _review(c, other_run, plan_id, verdict="reject", handed=False))
    assert isinstance(error, IntegrityError)
    assert "uq_agent_run_plans_plan_id" in str(error)


def test_a_rejected_plan_is_never_handed_off(db: SyncDb) -> None:
    run_id = db.run(lambda c: _run(c))
    plan_id = db.run(_plan)
    error = _fails(db, lambda c: _review(c, run_id, plan_id, verdict="reject", handed=True))
    assert isinstance(error, IntegrityError)
    assert "ck_agent_run_plans_reject_not_handed_off" in str(error)
    db.run(lambda c: _review(c, run_id, plan_id, verdict="reject", handed=False))


INVALID_REVIEWS: list[tuple[str, dict[str, Any]]] = [
    ("ck_agent_run_plans_verdict_valid", {"verdict": "approve"}),
    ("ck_agent_run_plans_fallback_reason_valid", {"fb": True, "fbr": "bored"}),
    ("ck_agent_run_plans_fallback_reason_iff", {"fb": True}),
    ("ck_agent_run_plans_fallback_reason_iff", {"fbr": "model_timeout"}),
    ("ck_agent_run_plans_explanation_bounded", {"explanation": "x" * 301}),
    ("ck_agent_run_plans_reasons_array", {"reasons": '{"a": 1}'}),
]


@pytest.mark.parametrize(("constraint", "overrides"), INVALID_REVIEWS)
def test_review_checks(db: SyncDb, constraint: str, overrides: dict[str, Any]) -> None:
    run_id = db.run(lambda c: _run(c))
    plan_id = db.run(_plan)
    error = _fails(db, lambda c: _review(c, run_id, plan_id, **overrides))
    assert isinstance(error, IntegrityError)
    assert constraint in str(error)


def test_a_review_needs_a_real_plan_and_run(db: SyncDb) -> None:
    run_id = db.run(lambda c: _run(c))
    plan_id = db.run(_plan)
    for run, plan in ((run_id, uuid.uuid4()), (uuid.uuid4(), plan_id)):
        error = _fails(db, partial(_review, run_id=run, plan_id=plan))
        assert isinstance(error, IntegrityError)
    db.run(lambda c: _review(c, run_id, plan_id, explanation="x" * 300))


# --------------------------------------------------------------------------- #
# Downgrade
# --------------------------------------------------------------------------- #
def _state(c: Connection) -> dict[str, Any]:
    return {
        "tables": set(inspect(c).get_table_names()),
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


def test_downgrade_to_0004_restores_exactly_the_recommendations_schema(
    alembic_config: Config, migrated: None, sync_db: SyncDb
) -> None:
    head = sync_db.run(_state)
    command.downgrade(alembic_config, "0004_recommendations")
    try:
        state = sync_db.run(_state)
        assert not set(AGENT_TABLES) & state["tables"]
        assert (
            state["tables"]
            == head["tables"] - set(AGENT_TABLES) - set(REVIEW_TABLES) - DEPLOYMENT_TABLES
        )
        assert state["functions"] == {"ken_reject_mutation", "ken_link_plan_content_guard"}
        assert state["triggers"] == head["triggers"] - REVIEW_TRIGGERS - DEPLOYMENT_TRIGGERS - {
            "agent_runs_guard",
            "agent_runs_no_delete",
            "agent_steps_immutable",
            "agent_run_plans_immutable",
        }
        assert not any(t.startswith("agent_") for t in state["triggers"])
        assert state["revision"] == "0004_recommendations"
    finally:
        command.upgrade(alembic_config, "head")
    state = sync_db.run(_state)
    assert state == head
    assert state["revision"] == "0007_controlled_deployment"
