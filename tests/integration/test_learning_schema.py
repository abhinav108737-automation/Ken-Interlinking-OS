"""Migration 0009 (ADR-0016 section 9 as amended by the binding section 15) against the
``*_test`` database.

The pre-fan-out review found the phase's central safety claim DECORATIVE. These tests are the
reason it is not. In order of importance:

* a status change with NO same-transaction, ``txid``-matching transition row FAILS -- the hole
  the review identified, where any writer could INSERT ``proposed -> promoted`` with
  ``actor = 'user:admin'`` in one transaction and UPDATE the proposal in the next (15.7.1);
* a ``promoted`` transition is refused WITHOUT the transaction-local ``ken.learning_promoter``
  setting, with an ``agent:`` actor, or without an ``evaluation_run_id`` (15.7.2 / section 9.2);
* that setting does not leak to the next transaction on a pooled connection (the Phase 7 test
  is the model);
* content immutability, the status ladder, and no DELETE anywhere;
* at most one promoted skill per name; a promoted skill always names a human and an evaluation;
* a ``holdout`` above ``treatment_fraction = 0.20`` is REFUSED by the database; a ``holdout``
  cannot be activated without the transaction-local ``ken.learning_experiment_activator``
  setting, while a ``policy_ab`` activation is unaffected; and a holdout's arms must be assigned
  by TARGET page (the fix for the review's arm-contamination bug);
* one live experiment per page, and assignment only while the experiment is a draft;
* ``policy_evaluations.metrics`` must carry ``by_decision_class`` (15.7.11);
* ``job_runs.kind`` accepts ``'reflection'``, without which ``make reflect`` cannot run;
* an exact downgrade to 0008 and back.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ken_contracts.learning import (
    OPEN_PROPOSAL_STATUSES,
    PROPOSAL_TRANSITIONS,
    ProposalStatus,
)
from ken_core.db.models.learning import APPEND_ONLY_LEARNING_TABLES
from tests.integration.conftest import (
    LEARNING_ACTIVATOR_ON,
    LEARNING_FUNCTIONS,
    LEARNING_PROMOTER_ON,
    LEARNING_TABLES,
    LEARNING_TRIGGERS,
    SyncDb,
)
from tests.integration.test_review_schema import SOURCE, TARGET, _seed, _state

pytestmark = pytest.mark.integration

JOB = uuid.UUID("00000000-0000-4000-8000-000000000901")
PROPOSAL = uuid.UUID("00000000-0000-4000-8000-000000000902")
EVALUATION = uuid.UUID("00000000-0000-4000-8000-000000000903")
CANDIDATE = uuid.UUID("00000000-0000-4000-8000-000000000904")
SKILL = uuid.UUID("00000000-0000-4000-8000-000000000905")
EXPERIMENT = uuid.UUID("00000000-0000-4000-8000-000000000906")
OTHER_EXPERIMENT = uuid.UUID("00000000-0000-4000-8000-000000000907")
FINGERPRINT = "a" * 64
SHA = "b" * 64
METRICS = json.dumps({"by_decision_class": {}, "cases_total": 0})


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
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


def _guarded(error: DBAPIError) -> bool:
    """A trigger refusal (55000), as every 0009 guard raises."""
    return _sqlstate(error) == "55000"


def _job(c: Connection, kind: str = "reflection") -> None:
    c.execute(
        text(
            "INSERT INTO job_runs (id, kind, status, triggered_by) "  # noqa: S608
            f"VALUES (:id, '{kind}', 'running', 'cli')"
        ),
        {"id": JOB},
    )


def _proposal(c: Connection, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "id": PROPOSAL,
        "kind": "policy",
        "status": "proposed",
        "target_kind": "ranking",
        "base_version": "1",
        "title": "Shift weight towards taxonomy",
        "rationale": "Reviewers edited anchors on semantic-only picks.",
        "body": json.dumps({"kind": "policy", "changes": []}),
        "evidence": json.dumps({"observations": []}),
        "fingerprint": FINGERPRINT,
        "proposed_by": "system:learning",
        "job": JOB,
    } | overrides
    c.execute(
        text(
            "INSERT INTO learning_proposals (id, kind, status, target_kind, base_version, "
            "title, rationale, body, evidence, fingerprint, proposed_by, job_run_id) "
            "VALUES (:id, :kind, :status, :target_kind, :base_version, :title, :rationale, "
            "CAST(:body AS jsonb), CAST(:evidence AS jsonb), :fingerprint, :proposed_by, :job)"
        ),
        values,
    )


def _evaluation(c: Connection, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "id": EVALUATION,
        "job": JOB,
        "mode": "offline",
        "proposal": PROPOSAL,
        "skill": None,
        "candidate": None,
        "suites": json.dumps(["critic"]),
        "manifests": json.dumps({"critic": SHA}),
        "metrics": METRICS,
        "gate": "pass",
    } | overrides
    c.execute(
        text(
            "INSERT INTO policy_evaluations (id, job_run_id, mode, proposal_id, skill_id, "
            "candidate_id, suites, dataset_manifests, metrics, gate_result, runner_version, "
            "model_provider, started_at) VALUES (:id, :job, :mode, :proposal, :skill, "
            ":candidate, CAST(:suites AS jsonb), CAST(:manifests AS jsonb), "
            "CAST(:metrics AS jsonb), :gate, '1', 'mock', now())"
        ),
        values,
    )


def _candidate(c: Connection, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "id": CANDIDATE,
        "proposal": PROPOSAL,
        "kind": "ranking",
        "base": "1",
        "version": "2",
        "document": json.dumps({"policy": "ranking", "version": 2}),
        "sha": SHA,
    } | overrides
    c.execute(
        text(
            "INSERT INTO policy_candidates (id, proposal_id, policy_kind, base_version, "
            "candidate_version, document, document_sha256) VALUES (:id, :proposal, :kind, "
            ":base, :version, CAST(:document AS jsonb), :sha)"
        ),
        values,
    )


def _skill(c: Connection, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "id": SKILL,
        "name": "select_natural_anchor",
        "version": 1,
        "description": "Pick a descriptive anchor phrase.",
        "instructions": "Prefer a noun phrase already present in the block text.",
        "input_schema": json.dumps({"type": "object"}),
        "output_schema": json.dumps({"type": "object"}),
        "suites": json.dumps(["anchor-selection"]),
        "role": "reasoning",
        "source": "human",
        "proposal": None,
        "status": "draft",
    } | overrides
    c.execute(
        text(
            "INSERT INTO skills (id, name, version, description, instructions, input_schema, "
            "output_schema, suites, model_role, source, proposal_id, status) VALUES (:id, "
            ":name, :version, :description, :instructions, CAST(:input_schema AS jsonb), "
            "CAST(:output_schema AS jsonb), CAST(:suites AS jsonb), :role, :source, "
            ":proposal, :status)"
        ),
        values,
    )


def _experiment(c: Connection, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "id": EXPERIMENT,
        "key": "ranking-ab-1",
        "description": "Shadow the candidate ranking policy on half the sources.",
        "unit": "source_page",
        "kind": "policy_ab",
        "candidate": CANDIDATE,
        "proposal": PROPOSAL,
        "fraction": 0.5,
        "status": "draft",
        "by": "user:ops",
    } | overrides
    c.execute(
        text(
            "INSERT INTO experiments (id, key, description, unit, kind, candidate_id, "
            "proposal_id, treatment_fraction, status, created_by) VALUES (:id, :key, "
            ":description, :unit, :kind, :candidate, :proposal, :fraction, :status, :by)"
        ),
        values,
    )


def _holdout(c: Connection, **overrides: Any) -> None:
    """A holdout: assigned by TARGET page, no candidate, fraction at the policy default."""
    _experiment(
        c,
        **{
            "id": OTHER_EXPERIMENT,
            "key": "inbound-holdout-1",
            "description": "Withhold inbound links from a tenth of target pages.",
            "unit": "target_page",
            "kind": "holdout",
            "candidate": None,
            "fraction": 0.1,
            **overrides,
        },
    )


def _assignment(c: Connection, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "experiment": EXPERIMENT,
        "page": SOURCE,
        "arm": "treatment",
        "by": "system:learning",
    } | overrides
    c.execute(
        text(
            "INSERT INTO experiment_assignments (experiment_id, page_id, arm, assigned_by) "
            "VALUES (:experiment, :page, :arm, :by)"
        ),
        values,
    )


def _transition(c: Connection, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "proposal": PROPOSAL,
        "from": "proposed",
        "to": "offline_test",
        "actor": "system:learning",
        "reason": None,
        "note": None,
        "evaluation": None,
    } | overrides
    c.execute(
        text(
            "INSERT INTO learning_proposal_transitions (proposal_id, from_status, to_status, "
            "actor, reason, note, evaluation_run_id) VALUES (:proposal, :from, :to, :actor, "
            ":reason, :note, :evaluation)"
        ),
        values,
    )


def _set_status(c: Connection, status: str, proposal: uuid.UUID = PROPOSAL) -> None:
    c.execute(
        text("UPDATE learning_proposals SET status = :s WHERE id = :id"),
        {"s": status, "id": proposal},
    )


def _base(c: Connection) -> None:
    _seed(c)
    _job(c)
    _proposal(c)
    _candidate(c)
    _evaluation(c)


@pytest.fixture
def db(migrated: None, sync_db: SyncDb) -> SyncDb:
    sync_db.run(_base)
    return sync_db


def _count(db: SyncDb, table: str) -> int:
    return int(
        db.run(lambda c: c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608
    )


def _experiment_status(db: SyncDb, experiment: uuid.UUID) -> str:
    return str(
        db.run(
            lambda c: c.execute(
                text("SELECT status FROM experiments WHERE id = :i"), {"i": experiment}
            ).scalar_one()
        )
    )


def _status(db: SyncDb) -> str:
    return str(
        db.run(
            lambda c: c.execute(
                text("SELECT status FROM learning_proposals WHERE id = :id"), {"id": PROPOSAL}
            ).scalar_one()
        )
    )


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
def test_the_seven_tables_and_six_guard_functions_exist(db: SyncDb) -> None:
    state = db.run(_state)
    assert state["tables"] >= LEARNING_TABLES
    assert state["functions"] >= LEARNING_FUNCTIONS
    assert state["triggers"] >= LEARNING_TRIGGERS


def test_the_transitions_table_carries_a_txid_with_a_volatile_default(db: SyncDb) -> None:
    """Without this column the append-only CHECKs are forgeable across two transactions
    (ADR-0016 15.7.1). It mirrors ``link_plan_transitions.txid`` from 0007."""

    def _column(c: Connection) -> Any:
        rows: Any = inspect(c).get_columns("learning_proposal_transitions")
        return next(row for row in rows if row["name"] == "txid")

    column = db.run(_column)
    assert column["nullable"] is False
    assert "pg_current_xact_id()" in str(column["default"])


def test_the_status_integrity_trigger_is_a_deferred_constraint_trigger(db: SyncDb) -> None:
    def _trigger(c: Connection) -> tuple[Any, ...]:
        return tuple(
            c.execute(
                text(
                    "SELECT t.tgdeferrable, t.tginitdeferred, t.tgconstraint <> 0 "
                    "FROM pg_trigger t WHERE t.tgname = 'learning_proposals_status_integrity'"
                )
            ).one()
        )

    assert db.run(_trigger) == (True, True, True)


def test_no_security_definer_function_was_introduced(db: SyncDb) -> None:
    """Every guard is a plain trigger: they defend against application bugs, not a DB
    superuser (who can set session_replication_role = replica)."""

    def _definers(c: Connection) -> list[str]:
        return list(
            c.execute(
                text(
                    "SELECT p.proname FROM pg_proc p JOIN pg_namespace n "
                    "ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.prosecdef"
                )
            ).scalars()
        )

    assert db.run(_definers) == []


# --------------------------------------------------------------------------- #
# THE central invariant: same-transaction, txid-matching transitions (15.7.1)
# --------------------------------------------------------------------------- #
def test_a_status_change_without_any_transition_row_fails(db: SyncDb) -> None:
    assert _guarded(_fails(db, lambda c: _set_status(c, "offline_test")))
    assert _status(db) == "proposed"


def test_a_transition_row_written_in_an_earlier_transaction_does_not_authorise_the_change(
    db: SyncDb,
) -> None:
    """The hole the review found. The transitions table is append-only, so without ``txid`` a
    writer could record ``user:admin`` once and then update the proposal at leisure."""
    db.run(_transition)
    assert _count(db, "learning_proposal_transitions") == 1
    assert _guarded(_fails(db, lambda c: _set_status(c, "offline_test")))
    assert _status(db) == "proposed"


def test_a_transition_row_in_the_same_transaction_authorises_the_change(db: SyncDb) -> None:
    def _advance(c: Connection) -> None:
        _transition(c)
        _set_status(c, "offline_test")

    db.run(_advance)
    assert _status(db) == "offline_test"


def test_the_transition_row_must_match_the_exact_pair(db: SyncDb) -> None:
    def _mismatched(c: Connection) -> None:
        _transition(c, **{"to": "shadow"})
        _set_status(c, "offline_test")

    assert _guarded(_fails(db, _mismatched))


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("proposed", "promoted"),
        ("proposed", "shadow"),
        ("rejected", "offline_test"),
        ("offline_test", "proposed"),
    ],
)
def test_a_transition_outside_the_ladder_is_refused(db: SyncDb, source: str, target: str) -> None:
    assert ProposalStatus(target) not in PROPOSAL_TRANSITIONS[ProposalStatus(source)], (
        "the test case must be a transition the contract forbids"
    )

    def _jump(c: Connection) -> None:
        if source != "proposed":
            _transition(c, **{"to": source, "actor": "user:ops", "evaluation": None})
            c.execute(
                text("UPDATE learning_proposals SET status = :s WHERE id = :i"),
                {"s": source, "i": PROPOSAL},
            )
        _transition(c, **{"from": source, "to": target})
        _set_status(c, target)

    assert _guarded(_fails(db, _jump))


# --------------------------------------------------------------------------- #
# The promoter guard: exactly one code path may promote (15.7.2)
# --------------------------------------------------------------------------- #
def _to_offline_test(c: Connection) -> None:
    _transition(c)
    _set_status(c, "offline_test")


def _promote(c: Connection, *, guc: bool = True, **overrides: Any) -> None:
    if guc:
        c.execute(text(LEARNING_PROMOTER_ON))
    values: dict[str, Any] = {
        "from": "offline_test",
        "to": "promoted",
        "actor": "user:ops",
        "evaluation": EVALUATION,
    } | overrides
    _transition(c, **values)
    _set_status(c, "promoted")


def test_a_promotion_without_the_promoter_setting_is_refused(db: SyncDb) -> None:
    db.run(_to_offline_test)
    assert _guarded(_fails(db, lambda c: _promote(c, guc=False)))
    assert _status(db) == "offline_test"
    assert _count(db, "learning_proposal_transitions") == 1


def test_a_promotion_with_the_promoter_setting_succeeds(db: SyncDb) -> None:
    db.run(_to_offline_test)
    db.run(_promote)
    assert _status(db) == "promoted"


def test_clearing_the_promoter_setting_after_writing_the_row_still_fails(db: SyncDb) -> None:
    """The deferred guard re-checks the setting at COMMIT, so the two triggers are not
    redundant in the one order that could otherwise slip through: set the setting, write the
    transition, clear it, then update the proposal."""
    db.run(_to_offline_test)

    def _forge(c: Connection) -> None:
        c.execute(text(LEARNING_PROMOTER_ON))
        _transition(
            c,
            **{
                "from": "offline_test",
                "to": "promoted",
                "actor": "user:ops",
                "evaluation": EVALUATION,
            },
        )
        c.execute(text("SELECT set_config('ken.learning_promoter', '', true)"))
        _set_status(c, "promoted")

    assert _guarded(_fails(db, _forge))
    assert _status(db) == "offline_test"


def test_a_promotion_by_an_agent_actor_is_refused(db: SyncDb) -> None:
    """``agent:*`` actors never promote (ADR-0012). A CHECK, so it holds for any writer."""
    db.run(_to_offline_test)
    error = _fails(db, lambda c: _promote(c, actor="agent:reflector@1"))
    assert _sqlstate(error) == "23514"
    assert "human_only_targets" in str(error.orig)


@pytest.mark.parametrize("actor", ["system:learning", "user:X", "user:ab"])
def test_only_a_well_shaped_human_actor_may_promote(db: SyncDb, actor: str) -> None:
    db.run(_to_offline_test)
    assert _sqlstate(_fails(db, lambda c: _promote(c, actor=actor))) == "23514"


def test_a_promotion_without_an_evaluation_run_is_refused(db: SyncDb) -> None:
    db.run(_to_offline_test)
    error = _fails(db, lambda c: _promote(c, evaluation=None))
    assert _sqlstate(error) == "23514"
    assert "promoted_cites_evaluation" in str(error.orig)


@pytest.mark.parametrize("target", ["rejected", "rolled_back"])
def test_rejecting_and_rolling_back_also_need_a_human(db: SyncDb, target: str) -> None:
    db.run(_to_offline_test)
    if target == "rolled_back":
        db.run(_promote)
        source = "promoted"
    else:
        source = "offline_test"

    def _attempt(c: Connection) -> None:
        _transition(c, **{"from": source, "to": target, "actor": "system:learning"})
        _set_status(c, target)

    assert _sqlstate(_fails(db, _attempt)) == "23514"


async def test_the_promoter_setting_never_leaks_to_the_next_transaction(
    db: SyncDb, test_database_url: str
) -> None:
    """One pooled connection (pool_size=1): the setting made in transaction 1 is gone in
    transaction 2 on the SAME backend, so a promotion cannot be carried over.

    Everything runs on the async engine: ``SyncDb.run`` calls ``asyncio.run``, which cannot be
    reached from inside a running loop (the ``db`` fixture seeded the rows before the loop
    started)."""
    engine: AsyncEngine = create_async_engine(test_database_url, pool_size=1, max_overflow=0)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO learning_proposal_transitions (proposal_id, from_status, "
                    "to_status, actor) VALUES (:p, 'proposed', 'offline_test', "
                    "'system:learning')"
                ),
                {"p": PROPOSAL},
            )
            await conn.execute(
                text("UPDATE learning_proposals SET status = 'offline_test' WHERE id = :p"),
                {"p": PROPOSAL},
            )
        async with engine.begin() as conn:
            pid_1: Any = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await conn.execute(text(LEARNING_PROMOTER_ON))
            assert (
                await conn.execute(text("SELECT current_setting('ken.learning_promoter', true)"))
            ).scalar_one() == "on"
        async with engine.connect() as conn:
            pid_2: Any = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            assert pid_2 == pid_1
            setting: Any = (
                await conn.execute(text("SELECT current_setting('ken.learning_promoter', true)"))
            ).scalar_one()
            assert setting in ("", None)
            await conn.rollback()
            with pytest.raises(DBAPIError) as info:
                async with conn.begin():
                    await conn.execute(
                        text(
                            "INSERT INTO learning_proposal_transitions (proposal_id, "
                            "from_status, to_status, actor, evaluation_run_id) VALUES "
                            "(:p, 'offline_test', 'promoted', 'user:ops', :e)"
                        ),
                        {"p": PROPOSAL, "e": EVALUATION},
                    )
            assert _sqlstate(info.value) == "55000"
        async with engine.connect() as conn:
            status: Any = (
                await conn.execute(
                    text("SELECT status FROM learning_proposals WHERE id = :p"), {"p": PROPOSAL}
                )
            ).scalar_one()
        assert status == "offline_test"
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# Immutability, append-only, no DELETE
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("title", "'rewritten'"),
        ("rationale", "'rewritten'"),
        ("fingerprint", f"'{SHA}'"),
        ("proposed_by", "'agent:x@1'"),
        ("body", "'{}'::jsonb"),
        ("evidence", "'{}'::jsonb"),
        ("job_run_id", "gen_random_uuid()"),
    ],
)
def test_proposal_content_is_immutable(db: SyncDb, column: str, value: str) -> None:
    error = _fails(
        db,
        lambda c: c.execute(
            text(f"UPDATE learning_proposals SET {column} = {value} WHERE id = :i"),  # noqa: S608
            {"i": PROPOSAL},
        ),
    )
    assert _guarded(error)


def test_updated_at_may_move_without_a_transition(db: SyncDb) -> None:
    db.run(
        lambda c: c.execute(
            text("UPDATE learning_proposals SET updated_at = now() WHERE id = :i"),
            {"i": PROPOSAL},
        )
    )
    assert _status(db) == "proposed"


@pytest.mark.parametrize(
    "table", ["learning_proposals", "skills", "experiments", *APPEND_ONLY_LEARNING_TABLES]
)
def test_nothing_is_ever_deleted(db: SyncDb, table: str) -> None:
    db.run(_skill)
    db.run(_experiment)
    db.run(_assignment)
    db.run(_transition)
    assert _count(db, table) >= 1
    assert _guarded(_fails(db, lambda c: c.execute(text(f"DELETE FROM {table}"))))  # noqa: S608
    assert _count(db, table) >= 1


_APPEND_ONLY_TIMESTAMP = {
    "learning_proposal_transitions": "created_at",
    "policy_candidates": "created_at",
    "policy_evaluations": "started_at",
    "experiment_assignments": "assigned_at",
}


@pytest.mark.parametrize("table", APPEND_ONLY_LEARNING_TABLES)
def test_append_only_tables_reject_update_too(db: SyncDb, table: str) -> None:
    db.run(_skill)
    db.run(_experiment)
    db.run(_assignment)
    db.run(_transition)
    column = _APPEND_ONLY_TIMESTAMP[table]
    assert _guarded(
        _fails(
            db,
            lambda c: c.execute(text(f"UPDATE {table} SET {column} = now()")),  # noqa: S608
        )
    )


def test_the_promoter_setting_does_not_unlock_a_delete(db: SyncDb) -> None:
    """Unlike Phase 7's writer setting, ``ken.learning_promoter`` grants NO delete: learning
    rows are an audit trail, and a replace-import has no analogue here."""
    db.run(_transition)

    def _attempt(c: Connection) -> None:
        c.execute(text(LEARNING_PROMOTER_ON))
        c.execute(text("DELETE FROM learning_proposal_transitions"))

    assert _guarded(_fails(db, _attempt))


# --------------------------------------------------------------------------- #
# Dedupe: one OPEN proposal per fingerprint
# --------------------------------------------------------------------------- #
def test_a_reflection_rerun_is_idempotent_while_the_proposal_is_open(db: SyncDb) -> None:
    assert _sqlstate(_fails(db, lambda c: _proposal(c, id=uuid.uuid4()))) == "23505"


def test_the_same_fingerprint_may_be_proposed_again_after_a_terminal_outcome(db: SyncDb) -> None:
    def _withdraw(c: Connection) -> None:
        _transition(c, **{"to": "withdrawn", "actor": "user:ops", "reason": "out_of_scope"})
        _set_status(c, "withdrawn")

    db.run(_withdraw)
    db.run(lambda c: _proposal(c, id=uuid.uuid4()))
    assert _count(db, "learning_proposals") == 2


def test_the_partial_index_covers_exactly_the_open_statuses(db: SyncDb) -> None:
    def _predicate(c: Connection) -> str:
        return str(
            c.execute(
                text(
                    "SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname = 'ix_learning_proposals_open_fingerprint'"
                )
            ).scalar_one()
        )

    predicate = _predicate_text = db.run(_predicate)
    for status in ProposalStatus:
        present = f"'{status.value}'" in _predicate_text
        assert present == (status in OPEN_PROPOSAL_STATUSES), (status, predicate)


# --------------------------------------------------------------------------- #
# Proposals can never claim a human author
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("actor", ["user:ops", "nonsense", "", "user:admin"])
def test_a_proposal_cannot_be_authored_by_a_human_actor(db: SyncDb, actor: str) -> None:
    error = _fails(db, lambda c: _proposal(c, id=uuid.uuid4(), fingerprint=SHA, proposed_by=actor))
    assert _sqlstate(error) == "23514"
    assert "proposed_by_not_human" in str(error.orig)


def test_an_agent_may_author_a_proposal(db: SyncDb) -> None:
    db.run(
        lambda c: _proposal(c, id=uuid.uuid4(), fingerprint=SHA, proposed_by="agent:reflector@1")
    )
    assert _count(db, "learning_proposals") == 2


def test_a_skill_proposal_has_no_target_kind_and_a_policy_proposal_must_have_one(
    db: SyncDb,
) -> None:
    assert (
        _sqlstate(
            _fails(db, lambda c: _proposal(c, id=uuid.uuid4(), fingerprint=SHA, target_kind=None))
        )
        == "23514"
    )
    assert (
        _sqlstate(
            _fails(
                db,
                lambda c: _proposal(
                    c, id=uuid.uuid4(), fingerprint=SHA, kind="skill", target_kind="ranking"
                ),
            )
        )
        == "23514"
    )
    db.run(lambda c: _proposal(c, id=uuid.uuid4(), fingerprint=SHA, kind="skill", target_kind=None))


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #
def test_at_most_one_promoted_skill_per_name(db: SyncDb) -> None:
    other = uuid.uuid4()
    db.run(lambda c: _skill(c, status="offline_test"))
    db.run(lambda c: _skill(c, id=other, version=2, status="offline_test"))

    def _promote_skill(skill_id: uuid.UUID) -> Callable[[Connection], Any]:
        return lambda c: c.execute(
            text(
                "UPDATE skills SET status = 'promoted', promoted_by = 'user:ops', "
                "promoted_at = now(), evaluation_run_id = :e WHERE id = :i"
            ),
            {"e": EVALUATION, "i": skill_id},
        )

    db.run(_promote_skill(SKILL))
    assert _sqlstate(_fails(db, _promote_skill(other))) == "23505"


def test_a_promoted_skill_always_names_a_human_and_an_evaluation(db: SyncDb) -> None:
    db.run(lambda c: _skill(c, status="offline_test"))
    assert _guarded(
        _fails(
            db,
            lambda c: c.execute(
                text("UPDATE skills SET status = 'promoted' WHERE id = :i"), {"i": SKILL}
            ),
        )
    )
    assert _guarded(
        _fails(
            db,
            lambda c: c.execute(
                text(
                    "UPDATE skills SET status = 'promoted', promoted_by = 'agent:x@1', "
                    "promoted_at = now(), evaluation_run_id = :e WHERE id = :i"
                ),
                {"e": EVALUATION, "i": SKILL},
            ),
        )
    )


def test_skill_content_is_immutable_so_a_revision_is_a_new_version_row(db: SyncDb) -> None:
    db.run(_skill)
    assert _guarded(
        _fails(
            db,
            lambda c: c.execute(
                text("UPDATE skills SET instructions = 'rewritten' WHERE id = :i"), {"i": SKILL}
            ),
        )
    )
    db.run(lambda c: _skill(c, id=uuid.uuid4(), version=2))
    assert _count(db, "skills") == 2


def test_a_skill_status_may_only_move_along_its_ladder(db: SyncDb) -> None:
    db.run(_skill)
    assert _guarded(
        _fails(
            db,
            lambda c: c.execute(
                text("UPDATE skills SET status = 'retired' WHERE id = :i"), {"i": SKILL}
            ),
        )
    )


def test_a_reflection_sourced_skill_must_cite_its_proposal(db: SyncDb) -> None:
    assert (
        _sqlstate(_fails(db, lambda c: _skill(c, id=uuid.uuid4(), source="reflection"))) == "23514"
    )
    db.run(lambda c: _skill(c, id=uuid.uuid4(), source="reflection", proposal=PROPOSAL))


# --------------------------------------------------------------------------- #
# Evaluations
# --------------------------------------------------------------------------- #
def test_metrics_must_carry_a_by_decision_class_object(db: SyncDb) -> None:
    """ADR-0016 15.7.11: Phase 9 reads per-class calibration from here, so the DB -- not only
    the contract -- requires the key."""

    def _insert(metrics: str) -> Callable[[Connection], Any]:
        return lambda c: _evaluation(c, id=uuid.uuid4(), metrics=metrics)

    for metrics in ('{"cases_total": 0}', '{"by_decision_class": []}', "{}"):
        error = _fails(db, _insert(metrics))
        assert _sqlstate(error) == "23514"
        assert "metrics_by_decision_class" in str(error.orig)


def test_an_evaluation_describes_exactly_one_subject(db: SyncDb) -> None:
    db.run(_skill)
    assert (
        _sqlstate(_fails(db, lambda c: _evaluation(c, id=uuid.uuid4(), proposal=None, skill=None)))
        == "23514"
    )
    assert (
        _sqlstate(
            _fails(db, lambda c: _evaluation(c, id=uuid.uuid4(), proposal=PROPOSAL, skill=SKILL))
        )
        == "23514"
    )
    db.run(lambda c: _evaluation(c, id=uuid.uuid4(), proposal=None, skill=SKILL))


def test_a_candidate_bound_evaluation_needs_its_proposal(db: SyncDb) -> None:
    db.run(_skill)
    assert (
        _sqlstate(
            _fails(
                db,
                lambda c: _evaluation(
                    c, id=uuid.uuid4(), proposal=None, skill=SKILL, candidate=CANDIDATE
                ),
            )
        )
        == "23514"
    )


def test_an_evaluation_cannot_be_its_own_baseline(db: SyncDb) -> None:
    other = uuid.uuid4()
    assert (
        _sqlstate(
            _fails(
                db,
                lambda c: c.execute(
                    text(
                        "INSERT INTO policy_evaluations (id, job_run_id, mode, proposal_id, "
                        "suites, dataset_manifests, metrics, gate_result, runner_version, "
                        "model_provider, started_at, baseline_evaluation_id) VALUES "
                        "(:i, :j, 'offline', :p, '[\"critic\"]'::jsonb, '{}'::jsonb, "
                        "CAST(:m AS jsonb), 'pass', '1', 'mock', now(), :i)"
                    ),
                    {"i": other, "j": JOB, "p": PROPOSAL, "m": METRICS},
                ),
            )
        )
        == "23514"
    )


# --------------------------------------------------------------------------- #
# Experiments: holdout does not exist (16.4)
# --------------------------------------------------------------------------- #
def test_both_kinds_are_accepted(db: SyncDb) -> None:
    db.run(_experiment)
    db.run(_holdout)
    assert _count(db, "experiments") == 2


def test_an_unknown_kind_is_still_rejected(db: SyncDb) -> None:
    error = _fails(db, lambda c: _experiment(c, id=uuid.uuid4(), key="ab-x", kind="bandit"))
    assert _sqlstate(error) == "23514"
    assert "kind_valid" in str(error.orig)


@pytest.mark.parametrize("fraction", [0.21, 0.5, 1.0])
def test_a_holdout_over_twenty_percent_is_refused_by_the_database(
    db: SyncDb, fraction: float
) -> None:
    """The hard maximum lives in the DATABASE, so no policy file, no CLI flag and no service
    bug can widen how much of a live site has links withheld from it."""
    error = _fails(db, lambda c: _holdout(c, fraction=fraction))
    assert _sqlstate(error) == "23514"
    assert "holdout_fraction_capped" in str(error.orig)
    db.run(lambda c: _holdout(c, fraction=0.2))


def test_the_unit_is_pinned_to_the_kind(db: SyncDb) -> None:
    """The arm-contamination fix: a holdout compares TARGET pages, so it assigns target
    pages -- the database will not let the two come apart."""
    assert _sqlstate(_fails(db, lambda c: _holdout(c, unit="source_page"))) == "23514"
    assert (
        _sqlstate(
            _fails(db, lambda c: _experiment(c, id=uuid.uuid4(), key="ab-u", unit="target_page"))
        )
        == "23514"
    )


def test_a_holdout_carries_no_candidate_and_a_policy_ab_must(db: SyncDb) -> None:
    assert _sqlstate(_fails(db, lambda c: _holdout(c, candidate=CANDIDATE))) == "23514"
    assert (
        _sqlstate(_fails(db, lambda c: _experiment(c, id=uuid.uuid4(), key="ab-c", candidate=None)))
        == "23514"
    )


def _activate(experiment: uuid.UUID, *, guc: bool) -> Callable[[Connection], Any]:
    def _go(c: Connection) -> None:
        if guc:
            c.execute(text(LEARNING_ACTIVATOR_ON))
        c.execute(
            text("UPDATE experiments SET status = 'active', started_at = now() WHERE id = :i"),
            {"i": experiment},
        )

    return _go


def test_activating_a_holdout_without_the_activator_setting_is_refused(db: SyncDb) -> None:
    """``Settings.experiments_allow_holdout`` defaults false, and the lifecycle service is the
    only thing that sets this transaction-local GUC -- so a raw UPDATE cannot start a holdout
    either. 'The user has not enabled holdouts' is enforced by the database."""
    db.run(_holdout)
    assert _guarded(_fails(db, _activate(OTHER_EXPERIMENT, guc=False)))
    assert _experiment_status(db, OTHER_EXPERIMENT) == "draft"


def test_activating_a_holdout_with_the_activator_setting_succeeds(db: SyncDb) -> None:
    db.run(_holdout)
    db.run(_activate(OTHER_EXPERIMENT, guc=True))
    assert _experiment_status(db, OTHER_EXPERIMENT) == "active"


def test_activating_a_policy_ab_needs_no_activator_setting(db: SyncDb) -> None:
    """Both arms of a policy_ab get links, so it is not gated: only the ranking policy varies."""
    db.run(_experiment)
    db.run(_activate(EXPERIMENT, guc=False))
    assert _experiment_status(db, EXPERIMENT) == "active"


async def test_the_activator_setting_never_leaks_to_the_next_transaction(
    db: SyncDb, test_database_url: str
) -> None:
    """Transaction-local, like the promoter setting: a holdout cannot be activated by a later
    transaction riding on an earlier one's setting.

    Everything runs on the async engine -- ``SyncDb.run`` calls ``asyncio.run``, which cannot be
    reached from inside a running loop (the ``db`` fixture seeded the base rows before the loop
    started)."""
    del db
    engine: AsyncEngine = create_async_engine(test_database_url, pool_size=1, max_overflow=0)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO experiments (id, key, description, unit, kind, "
                    "treatment_fraction, status, created_by) VALUES (:i, "
                    "'inbound-holdout-2', 'd', 'target_page', 'holdout', 0.1, 'draft', "
                    "'user:ops')"
                ),
                {"i": OTHER_EXPERIMENT},
            )
        async with engine.begin() as conn:
            pid_1: Any = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await conn.execute(text(LEARNING_ACTIVATOR_ON))
        async with engine.connect() as conn:
            pid_2: Any = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            assert pid_2 == pid_1
            await conn.rollback()
            with pytest.raises(DBAPIError) as info:
                async with conn.begin():
                    await conn.execute(
                        text(
                            "UPDATE experiments SET status = 'active', started_at = now() "
                            "WHERE id = :i"
                        ),
                        {"i": OTHER_EXPERIMENT},
                    )
            assert _sqlstate(info.value) == "55000"
    finally:
        await engine.dispose()


def test_a_policy_ab_experiment_must_name_its_candidate(db: SyncDb) -> None:
    assert (
        _sqlstate(_fails(db, lambda c: _experiment(c, id=uuid.uuid4(), key="ab-2", candidate=None)))
        == "23514"
    )


def test_an_experiment_is_created_by_a_human_on_source_pages(db: SyncDb) -> None:
    assert (
        _sqlstate(
            _fails(db, lambda c: _experiment(c, id=uuid.uuid4(), key="ab-3", by="system:learning"))
        )
        == "23514"
    )
    assert (
        _sqlstate(
            _fails(
                db,
                lambda c: c.execute(
                    text(
                        "INSERT INTO experiments (id, key, description, unit, kind, "
                        "candidate_id, treatment_fraction, status, created_by) VALUES "
                        "(gen_random_uuid(), 'ab-4', 'd', 'target_page', 'policy_ab', :c, "
                        "0.5, 'draft', 'user:ops')"
                    ),
                    {"c": CANDIDATE},
                ),
            )
        )
        == "23514"
    )


@pytest.mark.parametrize("fraction", [0.0, -0.1, 1.5])
def test_the_treatment_fraction_is_bounded(db: SyncDb, fraction: float) -> None:
    assert (
        _sqlstate(
            _fails(db, lambda c: _experiment(c, id=uuid.uuid4(), key="ab-5", fraction=fraction))
        )
        == "23514"
    )


def test_one_live_experiment_per_page(db: SyncDb) -> None:
    db.run(_experiment)
    db.run(_assignment)
    db.run(lambda c: _experiment(c, id=OTHER_EXPERIMENT, key="ranking-ab-2"))
    assert _guarded(_fails(db, lambda c: _assignment(c, experiment=OTHER_EXPERIMENT)))


def test_a_page_may_be_reassigned_once_the_earlier_experiment_ended(db: SyncDb) -> None:
    db.run(_experiment)
    db.run(_assignment)
    db.run(
        lambda c: c.execute(
            text("UPDATE experiments SET status = 'ended', ended_at = now() WHERE id = :i"),
            {"i": EXPERIMENT},
        )
    )
    db.run(lambda c: _experiment(c, id=OTHER_EXPERIMENT, key="ranking-ab-2"))
    db.run(lambda c: _assignment(c, experiment=OTHER_EXPERIMENT))
    assert _count(db, "experiment_assignments") == 2


def test_pages_are_assigned_only_while_the_experiment_is_a_draft(db: SyncDb) -> None:
    db.run(_experiment)
    db.run(
        lambda c: c.execute(
            text("UPDATE experiments SET status = 'active', started_at = now() WHERE id = :i"),
            {"i": EXPERIMENT},
        )
    )
    assert _guarded(_fails(db, _assignment))


def test_a_page_is_assigned_at_most_once_per_experiment(db: SyncDb) -> None:
    db.run(_experiment)
    db.run(_assignment)
    assert _sqlstate(_fails(db, lambda c: _assignment(c, arm="control"))) == "23505"
    db.run(lambda c: _assignment(c, page=TARGET, arm="control"))


def test_experiment_content_is_immutable_and_its_status_follows_its_ladder(db: SyncDb) -> None:
    db.run(_experiment)
    assert _guarded(
        _fails(
            db,
            lambda c: c.execute(
                text("UPDATE experiments SET treatment_fraction = 0.9 WHERE id = :i"),
                {"i": EXPERIMENT},
            ),
        )
    )
    db.run(
        lambda c: c.execute(
            text("UPDATE experiments SET status = 'ended', ended_at = now() WHERE id = :i"),
            {"i": EXPERIMENT},
        )
    )
    assert _guarded(
        _fails(
            db,
            lambda c: c.execute(
                text("UPDATE experiments SET status = 'active' WHERE id = :i"), {"i": EXPERIMENT}
            ),
        )
    )


# --------------------------------------------------------------------------- #
# job_runs gains the reflection kind
# --------------------------------------------------------------------------- #
def test_job_runs_accepts_the_reflection_kind(migrated: None, sync_db: SyncDb) -> None:
    """``JobKind`` is a Pydantic enum, but ``job_runs.kind`` carries a frozen literal copy, so
    a new member is a migration -- not just a contract edit."""
    sync_db.run(lambda c: _job(c, "reflection"))
    assert (
        int(
            sync_db.run(
                lambda c: c.execute(
                    text("SELECT count(*) FROM job_runs WHERE kind = 'reflection'")
                ).scalar_one()
            )
        )
        == 1
    )


def test_job_runs_still_rejects_an_unknown_kind(migrated: None, sync_db: SyncDb) -> None:
    assert _sqlstate(_fails(sync_db, lambda c: _job(c, "made_up"))) == "23514"


# --------------------------------------------------------------------------- #
# Downgrade
# --------------------------------------------------------------------------- #
def test_downgrade_to_0008_restores_exactly_the_analytics_schema(
    alembic_config: Config, migrated: None, sync_db: SyncDb
) -> None:
    head = sync_db.run(_state)
    command.downgrade(alembic_config, "0008_analytics")
    try:
        state = sync_db.run(_state)
        assert state["tables"] == head["tables"] - LEARNING_TABLES
        assert state["functions"] == head["functions"] - LEARNING_FUNCTIONS
        assert state["triggers"] == head["triggers"] - LEARNING_TRIGGERS
        assert state["revision"] == "0008_analytics"
        # The job_runs CHECK is restored too, so a downgraded database refuses a reflection run.
        assert _sqlstate(_fails(sync_db, lambda c: _job(c, "reflection"))) == "23514"
    finally:
        command.upgrade(alembic_config, "head")
    state = sync_db.run(_state)
    assert state == head
    assert state["revision"] == "0009_learning"
