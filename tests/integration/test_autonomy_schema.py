"""Migration 0010 (ADR-0017 section 2, ADR-0018 sections 4 and 6) against the ``*_test``
database.

What these tests are FOR, in order of importance:

* the embedding conversion **preserves values**: a ``vector`` column full of data becomes a
  ``real[]`` column with the same numbers, and the dimension CHECK survives in the new form.
  This is the one step of the phase that could silently destroy data;
* ``autonomy_decisions`` cannot record a lie: a fired block with a publish decision, a publish
  without rollback availability, a shadow publish, a shadow row without its counterfactual, an
  ``agent:`` actor, or a human decision with no reason are all REFUSED by the database;
* a non-shadow decision has no effect without the matching ``link_plan_transitions`` row in the
  SAME transaction -- the 0007 / 0009 ``txid`` pattern, without which the CHECKs test one
  row's shape only;
* the retry budget cannot be spent twice by a race, and a shadow pass cannot record an attempt;
* the new statuses are accepted by every frozen literal enum copy an older migration holds
  (``link_plans``, ``link_plan_transitions``, ``review_actions``), the new job kinds by
  ``job_runs``, and the new transitions by the replaced guard function;
* ``page_graph_metrics`` is recomputable-by-replace and its flags cannot contradict its counts;
* both new tables are append-only;
* an exact downgrade to 0009 and back.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from ken_contracts.autonomy import BlockCode
from ken_contracts.jobs import JobKind
from ken_core.db.models.autonomy import APPEND_ONLY_AUTONOMY_TABLES
from tests.integration.conftest import (
    AUTONOMY_FUNCTIONS,
    AUTONOMY_TABLES,
    AUTONOMY_TRIGGERS,
    HEAD_REVISION,
    MODULE_TABLES,
    PRIORITY_FUNCTIONS,
    PRIORITY_TABLES,
    PRIORITY_TRIGGERS,
    SyncDb,
)
from tests.integration.test_review_schema import SOURCE, TARGET, _seed, _state

pytestmark = pytest.mark.integration

PLAN = uuid.UUID("00000000-0000-4000-8000-000000000304")
ENGINE = "autonomy-engine-1"
SYSTEM = "system:autonomy-engine"


@pytest.fixture
def db(migrated: None, sync_db: SyncDb) -> SyncDb:
    sync_db.run(_seed)
    return sync_db


def _plan_at(c: Connection, plan_id: uuid.UUID, status: str, *, start: int = 100) -> None:
    """A plan inserted directly at ``status``.

    INSERT is not guarded -- only a status CHANGE is -- so this is how a test starts a plan
    part-way along the ladder without forging a transition history.
    """
    c.execute(
        text(
            "INSERT INTO link_plans (id, source_page_id, source_page_version, source_url, "
            "target_page_id, target_page_version, target_url, anchor_text, anchor_source, "
            "block_id, block_kind, occurrence_index, start_offset, end_offset, context_hash, "
            "total_score, confidence, rationale, engine_version, policy_version, "
            "ranking_policy_version, analysis_version, status) VALUES (:id, :s, 1, "
            "'https://s.test/s', :t, 1, 'https://s.test/t', 'anchor', 'target_topic', 'b0', "
            "'p', 0, :start, :end, :ctx, 0.5, 0.5, 'r', 'e', 1, 1, 'tax1-alg1', :status)"
        ),
        {
            "id": plan_id,
            "s": SOURCE,
            "t": TARGET,
            "ctx": "e" * 64,
            "start": start,
            "end": start + 6,
            "status": status,
        },
    )


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


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
def _decision(
    c: Connection,
    *,
    decision: str = "auto_publish",
    mode: str = "autonomous",
    blocks: str = "[]",
    rollback_available: bool = True,
    profile: str = "standard",
    would_have: str | None = None,
    actor: str = SYSTEM,
    reason: str | None = None,
    critic: str = "accept",
    plan_id: uuid.UUID = PLAN,
) -> None:
    c.execute(
        text(
            "INSERT INTO autonomy_decisions (plan_id, decision, mode, risk_tier, confidence, "
            "critic_outcome, policy_version, blocks, rollback_available, "
            "verification_profile, would_have_decision, engine_version, actor, "
            "override_reason) VALUES (:plan, :decision, :mode, 'low', 0.9, :critic, 1, "
            "CAST(:blocks AS jsonb), :rollback, :profile, :would_have, :engine, :actor, "
            ":reason)"
        ),
        {
            "plan": plan_id,
            "decision": decision,
            "mode": mode,
            "critic": critic,
            "blocks": blocks,
            "rollback": rollback_available,
            "profile": profile,
            "would_have": would_have,
            "engine": ENGINE,
            "actor": actor,
            "reason": reason,
        },
    )


def _attempt(
    c: Connection,
    *,
    attempt: int = 1,
    max_attempts: int = 3,
    decision: str = "auto_publish",
    mode: str = "autonomous",
    succeeded: bool = True,
    failure_code: str | None = None,
    rolled_back: bool = False,
) -> None:
    c.execute(
        text(
            "INSERT INTO publish_attempts (source_page_id, target_page_id, plan_id, attempt, "
            "max_attempts, decision, mode, verification_profile, succeeded, rolled_back, "
            "failure_code, actor) VALUES (:s, :t, :plan, :attempt, :max, :decision, :mode, "
            "'standard', :ok, :rolled_back, :code, :actor)"
        ),
        {
            "s": SOURCE,
            "t": TARGET,
            "plan": PLAN,
            "attempt": attempt,
            "max": max_attempts,
            "decision": decision,
            "mode": mode,
            "ok": succeeded,
            "rolled_back": rolled_back,
            "code": failure_code,
            "actor": SYSTEM,
        },
    )


def _metrics(
    c: Connection,
    *,
    in_degree: int = 2,
    is_orphan: bool = False,
    is_nearly_orphan: bool = True,
    version: str = "graph-alg1",
    page: uuid.UUID = TARGET,
) -> None:
    c.execute(
        text(
            "INSERT INTO page_graph_metrics (page_id, page_version, in_degree, out_degree, "
            "internal_pagerank, hub_score, authority_score, crawl_depth, is_orphan, "
            "is_nearly_orphan, is_bridge, component_id, cluster_id, metrics_version) VALUES "
            "(:p, 1, :in_degree, 3, 0.1, 0.2, 0.3, 2, :orphan, :nearly, false, 0, 1, :v)"
        ),
        {
            "p": page,
            "in_degree": in_degree,
            "orphan": is_orphan,
            "nearly": is_nearly_orphan,
            "v": version,
        },
    )


# --------------------------------------------------------------------------- #
# 1. The embedding conversion preserves values
# --------------------------------------------------------------------------- #
def test_the_embedding_conversion_preserves_values_and_the_dimension_check(
    alembic_config: Config, migrated: None, sync_db: SyncDb
) -> None:
    """ADR-0017 section 2. The only step of this phase that could destroy data.

    A ``real[]`` round trip through 0010's downgrade and upgrade (which, where the extension
    exists, goes through pgvector's own text form and back) must return the SAME numbers --
    float4 text output round-trips exactly, which is why ``real[]`` was chosen over ``bytea``.
    """
    page = uuid.UUID("00000000-0000-4000-8000-0000000003a1")
    vector = [0.1, -0.25, 0.3333333, 1.0]
    sync_db.run(_seed)

    def _insert(c: Connection) -> None:
        c.execute(
            text(
                "INSERT INTO page_embeddings (id, page_id, page_version, model, provider, "
                "dimensions, recipe_version, recipe_fingerprint, input_hash, embedding) "
                "VALUES (:id, :p, 1, 'm', 'mock', 4, 1, 'r1-a', :h, :v)"
            ),
            {"id": page, "p": SOURCE, "h": "c" * 64, "v": vector},
        )

    sync_db.run(_insert)

    def _read(c: Connection) -> Any:
        return c.execute(
            text(
                "SELECT embedding, array_length(embedding, 1), "
                "format_type(a.atttypid, a.atttypmod) FROM page_embeddings, pg_attribute a "
                "WHERE a.attrelid = 'page_embeddings'::regclass AND a.attname = 'embedding' "
                "AND page_embeddings.id = :id"
            ),
            {"id": page},
        ).one()

    before = sync_db.run(_read)
    assert before[1] == 4
    assert before[2] == "real[]"
    assert [pytest.approx(v, abs=1e-6) for v in vector] == list(before[0])

    command.downgrade(alembic_config, "0009_learning")
    try:
        # With no extension installed the column correctly STAYS real[] (and the CHECK with
        # it): that is the right 0009 state for a database whose 0003 never created one.
        state = sync_db.run(_read)
        assert state[1] == 4
    finally:
        command.upgrade(alembic_config, "head")

    after = sync_db.run(_read)
    assert after[2] == "real[]"
    assert list(after[0]) == list(before[0]), "the conversion must preserve every value"

    # The dimension rule survived the conversion, in its new form.
    error = _fails(
        sync_db,
        lambda c: c.execute(
            text("UPDATE page_embeddings SET dimensions = 3 WHERE id = :id"), {"id": page}
        ),
    )
    # page_embeddings is append-only, so the UPDATE is refused by the trigger first; insert a
    # mismatching row instead to reach the CHECK.
    assert _sqlstate(error) in {"55000", "23514"}

    mismatch = _fails(
        sync_db,
        lambda c: c.execute(
            text(
                "INSERT INTO page_embeddings (id, page_id, page_version, model, provider, "
                "dimensions, recipe_version, recipe_fingerprint, input_hash, embedding) "
                "VALUES (:id, :p, 1, 'm2', 'mock', 9, 1, 'r1-a', :h, :v)"
            ),
            {"id": uuid.uuid4(), "p": SOURCE, "h": "d" * 64, "v": [1.0, 2.0]},
        ),
    )
    assert "ck_page_embeddings_dimensions_match" in str(mismatch.orig)


# --------------------------------------------------------------------------- #
# 2. Structure
# --------------------------------------------------------------------------- #
def test_the_three_tables_and_the_guard_exist(db: SyncDb) -> None:
    state = db.run(_state)
    assert state["tables"] >= AUTONOMY_TABLES
    assert state["functions"] >= AUTONOMY_FUNCTIONS
    assert state["triggers"] >= AUTONOMY_TRIGGERS
    assert set(APPEND_ONLY_AUTONOMY_TABLES) == {"autonomy_decisions", "publish_attempts"}


def test_the_decision_table_carries_a_txid_with_a_volatile_default(db: SyncDb) -> None:
    def _default(c: Connection) -> Any:
        return c.execute(
            text(
                "SELECT column_default FROM information_schema.columns WHERE table_name = "
                "'autonomy_decisions' AND column_name = 'txid'"
            )
        ).scalar_one()

    assert "pg_current_xact_id()" in str(db.run(_default))


def test_the_orm_matches_the_migration_for_the_new_tables(db: SyncDb) -> None:
    """Spot-check that the columns the autonomy workstream will write actually exist."""
    columns = db.run(
        lambda c: {col["name"] for col in inspect(c).get_columns("autonomy_decisions")}
    )
    assert {
        "plan_id",
        "decision",
        "mode",
        "risk_tier",
        "confidence",
        "calibration_version",
        "critic_outcome",
        "critic_verdict",
        "policy_version",
        "blocks",
        "rollback_available",
        "verification_profile",
        "would_have_decision",
        "engine_version",
        "job_run_id",
        "actor",
        "override_reason",
        "txid",
        "evaluated_at",
    } <= columns

    attempts = db.run(lambda c: {col["name"] for col in inspect(c).get_columns("publish_attempts")})
    assert {
        "source_page_id",
        "target_page_id",
        "plan_id",
        "deployment_id",
        "attempt",
        "max_attempts",
        "decision",
        "mode",
        "verification_profile",
        "succeeded",
        "rolled_back",
        "failure_code",
        "cooldown_until",
        "actor",
    } <= attempts


# --------------------------------------------------------------------------- #
# 3. The frozen literal enum copies accept the new values
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", ["autonomy_decided", "blocked"])
def test_the_new_plan_statuses_pass_the_0004_checks(db: SyncDb, status: str) -> None:
    plan = uuid.uuid4()
    db.run(lambda c: _plan_at(c, plan, status, start=200 + len(status)))
    db.run(
        lambda c: c.execute(
            text(
                "INSERT INTO link_plan_transitions (plan_id, from_status, to_status, actor) "
                "VALUES (:plan, 'policy_checked', :status, :actor)"
            ),
            {"plan": plan, "status": status, "actor": SYSTEM},
        )
    )


@pytest.mark.parametrize(
    "constraint",
    [
        "ck_link_plans_status_valid",
        "ck_link_plan_transitions_to_status_valid",
        "ck_link_plan_transitions_from_status_valid",
        "ck_review_actions_from_status_valid",
        "ck_review_actions_to_status_valid",
        "ck_review_actions_new_plan_status_valid",
    ],
)
def test_every_frozen_literal_copy_of_the_plan_status_enum_was_replaced(
    db: SyncDb, constraint: str
) -> None:
    """0004 and 0006 each froze a literal copy of ``LinkPlanStatus`` into a CHECK. Both
    copies must now list the new statuses, or the first write of one fails -- exactly the
    trap the Phase 8 freeze found with ``job_runs.kind``."""
    definition: str = db.run(
        lambda c: c.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
            {"name": constraint},
        ).scalar_one()
    )
    assert "autonomy_decided" in definition, constraint
    assert "blocked" in definition, constraint


@pytest.mark.parametrize("kind", ["autonomy", "graph_metrics", "vector_index"])
def test_the_new_job_kinds_pass_the_0003_check(db: SyncDb, kind: str) -> None:
    """The trap the Phase 8 freeze found: ``job_runs.kind`` holds a frozen literal copy of
    ``JobKind``, so a new member needs a migration or the first run fails."""
    assert kind in {k.value for k in JobKind}
    db.run(
        lambda c: c.execute(
            text(
                "INSERT INTO job_runs (id, kind, status, triggered_by) "
                "VALUES (:id, :kind, 'running', 'cli')"
            ),
            {"id": uuid.uuid4(), "kind": kind},
        )
    )


def test_an_unknown_job_kind_is_still_refused(db: SyncDb) -> None:
    error = _fails(
        db,
        lambda c: c.execute(
            text(
                "INSERT INTO job_runs (id, kind, status, triggered_by) "
                "VALUES (:id, 'not_a_kind', 'running', 'cli')"
            ),
            {"id": uuid.uuid4()},
        ),
    )
    assert "ck_job_runs_kind_valid" in str(error.orig)


def test_the_replaced_guard_allows_the_new_transitions_and_still_refuses_invented_ones(
    db: SyncDb,
) -> None:
    """0007's guard holds a literal copy of ``ALLOWED_TRANSITIONS``; 0010 REPLACED the
    function. One plan walks the whole ADR-0018 ladder: the autonomous path, a block, an
    operator override back onto it, and finally the deploy hand-off. The invented move
    ``blocked -> deployed`` must still fail -- a blocked plan may never reach the CMS
    without passing the autonomy step again.
    """
    plan = uuid.uuid4()
    db.run(lambda c: _plan_at(c, plan, "policy_checked", start=300))

    def _move(from_status: str, to_status: str) -> Callable[[Connection], None]:
        def _apply(c: Connection) -> None:
            c.execute(
                text(
                    "INSERT INTO link_plan_transitions (plan_id, from_status, to_status, actor) "
                    "VALUES (:plan, :f, :t, :actor)"
                ),
                {"plan": plan, "f": from_status, "t": to_status, "actor": SYSTEM},
            )
            c.execute(
                text("UPDATE link_plans SET status = :t WHERE id = :id"),
                {"t": to_status, "id": plan},
            )

        return _apply

    def _status() -> str:
        return str(
            db.run(
                lambda c: c.execute(
                    text("SELECT status FROM link_plans WHERE id = :id"), {"id": plan}
                ).scalar_one()
            )
        )

    db.run(_move("policy_checked", "autonomy_decided"))
    assert _status() == "autonomy_decided"
    db.run(_move("autonomy_decided", "blocked"))
    assert _status() == "blocked"

    error = _fails(db, _move("blocked", "deployed"))
    assert "not an allowed transition" in str(error.orig)
    assert _status() == "blocked", "the refused move must leave the plan where it was"

    db.run(_move("blocked", "autonomy_decided"))  # an operator override re-enters the path
    db.run(_move("autonomy_decided", "validated"))  # and the deployment service takes over
    assert _status() == "validated"


# --------------------------------------------------------------------------- #
# 4. autonomy_decisions cannot record a lie
# --------------------------------------------------------------------------- #
def _with_transition(to_status: str) -> Callable[[Connection], None]:
    def _apply(c: Connection) -> None:
        c.execute(
            text(
                "INSERT INTO link_plan_transitions (plan_id, from_status, to_status, actor) "
                "VALUES (:plan, 'policy_checked', :to, :actor)"
            ),
            {"plan": PLAN, "to": to_status, "actor": SYSTEM},
        )

    return _apply


def test_a_decision_with_its_same_transaction_transition_row_is_accepted(db: SyncDb) -> None:
    def _apply(c: Connection) -> None:
        _with_transition("autonomy_decided")(c)
        _decision(c)

    db.run(_apply)
    assert (
        db.run(lambda c: c.execute(text("SELECT count(*) FROM autonomy_decisions")).scalar_one())
        == 1
    )


def test_a_decision_without_a_same_transaction_transition_row_is_refused(db: SyncDb) -> None:
    """The 0007 / 0009 pattern. Without it the CHECKs test one row's SHAPE only, so a caller
    could record the decision in one transaction and move the plan in another."""
    error = _fails(db, lambda c: _decision(c))
    assert "no same-transaction transition" in str(error.orig)
    assert _sqlstate(error) == "55000"


def test_a_transition_row_from_a_different_transaction_does_not_satisfy_the_guard(
    db: SyncDb,
) -> None:
    db.run(_with_transition("autonomy_decided"))  # committed on its own
    error = _fails(db, lambda c: _decision(c))
    assert "no same-transaction transition" in str(error.orig)


def test_a_block_decision_needs_a_transition_to_blocked_not_to_autonomy_decided(
    db: SyncDb,
) -> None:
    def _wrong(c: Connection) -> None:
        _with_transition("autonomy_decided")(c)
        _decision(c, decision="block", blocks=f'["{BlockCode.SELF_LINK.value}"]')

    error = _fails(db, _wrong)
    assert "no same-transaction transition to blocked" in str(error.orig)

    def _right(c: Connection) -> None:
        _with_transition("blocked")(c)
        _decision(c, decision="block", blocks=f'["{BlockCode.SELF_LINK.value}"]')

    db.run(_right)


def test_a_shadow_decision_must_not_move_the_plan(db: SyncDb) -> None:
    """``shadow`` records what WOULD have happened and publishes nothing, so a shadow row
    accompanied by a plan move is a contradiction the database refuses."""

    def _shadow_without_move(c: Connection) -> None:
        _decision(c, mode="shadow", decision="block", would_have="auto_publish")

    db.run(_shadow_without_move)

    def _shadow_with_move(c: Connection) -> None:
        _with_transition("autonomy_decided")(c)
        _decision(c, mode="shadow", decision="block", would_have="auto_publish")

    error = _fails(db, _shadow_with_move)
    assert "shadow autonomy decision must not move" in str(error.orig)


@pytest.mark.parametrize(
    ("kwargs", "constraint"),
    [
        # A fired block with a publish decision.
        (
            {"decision": "auto_publish", "blocks": '["self_link"]'},
            "ck_autonomy_decisions_blocks_force_block",
        ),
        # A publish without rollback availability.
        (
            {"decision": "auto_publish", "rollback_available": False},
            "ck_autonomy_decisions_no_publish_without_rollback",
        ),
        # The extra-verification decision with the standard profile.
        (
            {"decision": "auto_publish_with_extra_verification", "profile": "standard"},
            "ck_autonomy_decisions_extra_verification_is_strict",
        ),
        # Shadow that publishes.
        (
            {"mode": "shadow", "decision": "auto_publish", "would_have": "auto_publish"},
            "ck_autonomy_decisions_shadow_never_publishes",
        ),
        # Shadow without its counterfactual.
        (
            {"mode": "shadow", "decision": "block"},
            "ck_autonomy_decisions_counterfactual_iff_shadow",
        ),
        # A counterfactual outside shadow.
        (
            {"would_have": "auto_publish"},
            "ck_autonomy_decisions_counterfactual_iff_shadow",
        ),
        # An agent deciding anything.
        ({"actor": "agent:critic@1"}, "ck_autonomy_decisions_actor_is_not_an_agent"),
        # A human decision with no recorded reason.
        ({"actor": "user:admin"}, "ck_autonomy_decisions_override_records_a_reason"),
        # A system decision claiming to be an override.
        ({"reason": "because"}, "ck_autonomy_decisions_override_records_a_reason"),
        # no_action is an earlier outcome, never a block.
        ({"critic": "not_a_verdict"}, "ck_autonomy_decisions_critic_outcome_valid"),
    ],
    ids=[
        "block-with-publish",
        "publish-without-rollback",
        "extra-verification-not-strict",
        "shadow-publishes",
        "shadow-without-counterfactual",
        "counterfactual-outside-shadow",
        "agent-actor",
        "human-without-reason",
        "system-with-reason",
        "unknown-critic-outcome",
    ],
)
def test_the_database_refuses_an_impossible_decision_row(
    db: SyncDb, kwargs: dict[str, Any], constraint: str
) -> None:
    def _apply(c: Connection) -> None:
        _with_transition("blocked" if kwargs.get("decision") == "block" else "autonomy_decided")(c)
        _decision(c, **kwargs)

    error = _fails(db, _apply)
    assert constraint in str(error.orig)


def test_a_human_override_records_its_reason(db: SyncDb) -> None:
    def _apply(c: Connection) -> None:
        _with_transition("autonomy_decided")(c)
        _decision(c, actor="user:operator", reason="the destination was republished")

    db.run(_apply)
    assert (
        db.run(
            lambda c: c.execute(text("SELECT override_reason FROM autonomy_decisions")).scalar_one()
        )
        == "the destination was republished"
    )


# --------------------------------------------------------------------------- #
# 5. The retry budget
# --------------------------------------------------------------------------- #
def test_the_budget_cannot_be_spent_twice_for_one_pair(db: SyncDb) -> None:
    """ADR-0018 section 4: the UNIQUE index is what makes this impossible rather than
    unlikely -- a race's second INSERT of the same attempt number is REFUSED, not merged."""
    db.run(lambda c: _attempt(c, attempt=1))
    error = _fails(db, lambda c: _attempt(c, attempt=1, succeeded=False, failure_code="x"))
    assert isinstance(error, IntegrityError)
    assert "uq_publish_attempts_pair_attempt" in str(error.orig)
    db.run(lambda c: _attempt(c, attempt=2, succeeded=False, failure_code="verify_failed"))


def test_an_attempt_cannot_exceed_its_budget(db: SyncDb) -> None:
    error = _fails(db, lambda c: _attempt(c, attempt=4, max_attempts=3))
    assert "ck_publish_attempts_attempt_within_budget" in str(error.orig)


def test_a_shadow_pass_can_never_record_an_attempt(db: SyncDb) -> None:
    error = _fails(db, lambda c: _attempt(c, mode="shadow"))
    assert "ck_publish_attempts_shadow_never_attempts" in str(error.orig)


def test_a_block_is_never_attempted(db: SyncDb) -> None:
    error = _fails(db, lambda c: _attempt(c, decision="block"))
    assert "ck_publish_attempts_a_block_is_never_attempted" in str(error.orig)


def test_a_failed_attempt_must_carry_a_code(db: SyncDb) -> None:
    error = _fails(db, lambda c: _attempt(c, succeeded=False))
    assert "ck_publish_attempts_failure_has_a_code" in str(error.orig)


def test_a_rolled_back_attempt_is_not_a_success(db: SyncDb) -> None:
    error = _fails(db, lambda c: _attempt(c, succeeded=True, rolled_back=True))
    assert "ck_publish_attempts_rolled_back_is_not_success" in str(error.orig)


# --------------------------------------------------------------------------- #
# 6. page_graph_metrics
# --------------------------------------------------------------------------- #
def test_graph_metrics_are_replaced_per_recipe_version_not_appended(db: SyncDb) -> None:
    """Recomputable by REPLACE: the identity is (page, version, metrics_version), so a re-run
    of one recipe overwrites its own row while another recipe's row coexists."""
    db.run(_metrics)
    error = _fails(db, _metrics)
    assert isinstance(error, IntegrityError)
    assert "uq_page_graph_metrics_page_id_page_version_metrics_version" in str(error.orig)

    db.run(lambda c: _metrics(c, version="graph-alg2"))
    # And, unlike the append-only tables, an UPDATE is allowed: the numbers are derived.
    db.run(
        lambda c: c.execute(
            text(
                "UPDATE page_graph_metrics SET in_degree = 5, hub_score = 0.9 "
                "WHERE metrics_version = 'graph-alg1'"
            )
        )
    )
    assert (
        db.run(lambda c: c.execute(text("SELECT count(*) FROM page_graph_metrics")).scalar_one())
        == 2
    )


def test_the_orphan_flag_cannot_contradict_the_inbound_count(db: SyncDb) -> None:
    error = _fails(db, lambda c: _metrics(c, in_degree=3, is_orphan=True))
    assert "ck_page_graph_metrics_orphan_iff_no_inbound" in str(error.orig)
    error = _fails(db, lambda c: _metrics(c, in_degree=0, is_orphan=False))
    assert "ck_page_graph_metrics_orphan_iff_no_inbound" in str(error.orig)
    # An orphan is necessarily nearly-orphan too.
    error = _fails(db, lambda c: _metrics(c, in_degree=0, is_orphan=True, is_nearly_orphan=False))
    assert "ck_page_graph_metrics_orphan_implies_nearly" in str(error.orig)
    db.run(lambda c: _metrics(c, in_degree=0, is_orphan=True, is_nearly_orphan=True))


def test_graph_scores_are_bounded(db: SyncDb) -> None:
    error = _fails(
        db,
        lambda c: c.execute(
            text(
                "INSERT INTO page_graph_metrics (page_id, page_version, in_degree, out_degree, "
                "internal_pagerank, hub_score, authority_score, is_orphan, is_nearly_orphan, "
                "is_bridge, metrics_version) VALUES (:p, 1, 1, 1, 1.5, 0.1, 0.1, false, false, "
                "false, 'graph-alg1')"
            ),
            {"p": TARGET},
        ),
    )
    assert "ck_page_graph_metrics_pagerank_range" in str(error.orig)


# --------------------------------------------------------------------------- #
# 7. Append-only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE autonomy_decisions SET decision = 'auto_publish'",
        "DELETE FROM autonomy_decisions",
        "UPDATE publish_attempts SET succeeded = false",
        "DELETE FROM publish_attempts",
    ],
)
def test_the_autonomy_record_is_append_only(db: SyncDb, statement: str) -> None:
    """ADR-0018 section 6: this is the evidence Phase 9 calibration and Phase 8 learning read.
    Evidence that can be rewritten is not evidence."""

    def _seed_rows(c: Connection) -> None:
        _with_transition("autonomy_decided")(c)
        _decision(c)

    db.run(_seed_rows)
    db.run(lambda c: _attempt(c, attempt=1))

    error = _fails(db, lambda c: c.execute(text(statement)))
    assert _sqlstate(error) == "55000"
    assert "append-only" in str(error.orig)


# --------------------------------------------------------------------------- #
# 8. Round trip
# --------------------------------------------------------------------------- #
def test_downgrade_to_0009_restores_exactly_the_learning_schema(
    alembic_config: Config, migrated: None, sync_db: SyncDb
) -> None:
    head = sync_db.run(_state)
    command.downgrade(alembic_config, "0009_learning")
    try:
        state = sync_db.run(_state)
        assert state["tables"] == head["tables"] - AUTONOMY_TABLES - MODULE_TABLES - PRIORITY_TABLES
        assert state["functions"] == head["functions"] - AUTONOMY_FUNCTIONS - PRIORITY_FUNCTIONS
        assert state["triggers"] == head["triggers"] - AUTONOMY_TRIGGERS - PRIORITY_TRIGGERS
        assert state["revision"] == "0009_learning"
        # The frozen literal copies are restored too: a downgraded database refuses the new
        # statuses and the new job kinds.
        error = _fails(
            sync_db,
            lambda c: c.execute(
                text(
                    "INSERT INTO job_runs (id, kind, status, triggered_by) "
                    "VALUES (:id, 'graph_metrics', 'running', 'cli')"
                ),
                {"id": uuid.uuid4()},
            ),
        )
        assert "ck_job_runs_kind_valid" in str(error.orig)
    finally:
        command.upgrade(alembic_config, "head")
    state = sync_db.run(_state)
    assert state == head
    assert state["revision"] == HEAD_REVISION


# --------------------------------------------------------------------------- #
# 9. The LEGACY conversion path
#
# The test above exercises the no-op path, because migration 0003 was amended and a fresh
# test database never has a pgvector column. This test builds the state a REAL database is
# in -- extension installed, `embedding` a pgvector `vector`, a `vector_dims` CHECK -- and
# runs 0010 against it. It is the only coverage of the one step that could destroy data, so
# it uses values whose float4 text form is awkward on purpose.
# --------------------------------------------------------------------------- #
LEGACY_VECTOR = "[0.1,-0.3333333,1e-08,123456.78,-1]"
LEGACY_ARRAY = "{0.1,-0.3333333,1e-08,123456.78,-1}"


def _pgvector_available(db: SyncDb) -> bool:
    return bool(
        db.run(
            lambda c: c.execute(
                text("SELECT count(*) FROM pg_available_extensions WHERE name = 'vector'")
            ).scalar_one()
        )
    )


def test_a_real_pgvector_column_converts_losslessly_both_ways(
    alembic_config: Config, migrated: None, sync_db: SyncDb
) -> None:
    if not _pgvector_available(sync_db):
        pytest.skip("this server has no pgvector, so the legacy state cannot be built")

    sync_db.run(_seed)
    command.downgrade(alembic_config, "0009_learning")
    try:

        def _make_legacy(c: Connection) -> None:
            """Exactly what the ORIGINAL migration 0003 left behind."""
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            c.execute(
                text(
                    "ALTER TABLE page_embeddings "
                    "DROP CONSTRAINT ck_page_embeddings_dimensions_match"
                )
            )
            c.execute(
                text(
                    "ALTER TABLE page_embeddings ALTER COLUMN embedding TYPE vector "
                    "USING ('[' || array_to_string(embedding, ',') || ']')::vector"
                )
            )
            c.execute(
                text(
                    "ALTER TABLE page_embeddings ADD CONSTRAINT "
                    "ck_page_embeddings_dimensions_match "
                    "CHECK (vector_dims(embedding) = dimensions)"
                )
            )
            c.execute(
                text(
                    "INSERT INTO page_embeddings (id, page_id, page_version, model, provider, "
                    "dimensions, recipe_version, recipe_fingerprint, input_hash, embedding) "
                    "VALUES (:id, :p, 1, 'nomic-embed-text', 'ollama', 5, 1, 'r1-legacy', :h, "
                    "CAST(:v AS vector))"
                ),
                {"id": uuid.uuid4(), "p": SOURCE, "h": "f" * 64, "v": LEGACY_VECTOR},
            )

        sync_db.run(_make_legacy)
        assert _column_type(sync_db) == "vector"

        # The conversion.
        command.upgrade(alembic_config, "head")
        assert _column_type(sync_db) == "real[]"
        assert _embedding_text(sync_db) == LEGACY_ARRAY, "every value must survive"
        assert "array_length(embedding, 1) = dimensions" in _dimension_check(sync_db)

        # And back: where the extension exists, the downgrade restores the pgvector column
        # AND its CHECK, so 0009 is genuinely the 0009 state for that database.
        command.downgrade(alembic_config, "0009_learning")
        assert _column_type(sync_db) == "vector"
        assert _embedding_text(sync_db) == LEGACY_VECTOR
        assert "vector_dims(embedding) = dimensions" in _dimension_check(sync_db)

        command.upgrade(alembic_config, "head")
        assert _column_type(sync_db) == "real[]"
        assert _embedding_text(sync_db) == LEGACY_ARRAY
    finally:
        # Leave the database as every other test expects it: at head, with no extension.
        command.upgrade(alembic_config, "head")
        sync_db.run(lambda c: c.execute(text("TRUNCATE page_embeddings CASCADE")))
        sync_db.run(lambda c: c.execute(text("DROP EXTENSION IF EXISTS vector")))


def _column_type(db: SyncDb) -> str:
    return str(
        db.run(
            lambda c: c.execute(
                text(
                    "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                    "WHERE a.attrelid = 'page_embeddings'::regclass AND a.attname = 'embedding'"
                )
            ).scalar_one()
        )
    )


def _embedding_text(db: SyncDb) -> str:
    return str(
        db.run(
            lambda c: c.execute(text("SELECT embedding::text FROM page_embeddings")).scalar_one()
        )
    )


def _dimension_check(db: SyncDb) -> str:
    return str(
        db.run(
            lambda c: c.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_page_embeddings_dimensions_match'"
                )
            ).scalar_one()
        )
    )
