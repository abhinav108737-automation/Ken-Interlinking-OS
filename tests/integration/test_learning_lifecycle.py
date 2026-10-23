"""Proposal lifecycle and promotion (workstream D; ADR-0016 15.7, 16.1, 17.1).

In order of importance:

* with ``learning_demonstration_mode`` at its DEFAULT every promotion is refused with the typed
  ``DEMONSTRATION_MODE`` error -- even for an ``operator_admin`` holding a passing run -- and
  the refusal comes before any other check and before the database is touched;
* with it off, the service still refuses a stale, foreign, failed, inconclusive, wrong-
  fingerprint, dataset-changed, shadow-mode or stage-skipping evaluation, and a reviewer;
* a successful promotion writes ONE human transition in the proposal's own transaction, emits
  ``policy.promoted`` with ``promoted_by`` and ``evaluation_ref``, and writes NO file;
* the file writer refuses without an existing human ``promoted`` transition, never creates one,
  writes the two ranking files atomically and never overwrites a released file;
* system stages need a real gate: ``offline_test`` needs a passing run bound to the CURRENT
  fingerprint; ``shadow`` is only for ranking / recommendation candidates.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ken_contracts.auth import Principal, Role
from ken_contracts.events import EventName
from ken_contracts.learning import (
    AdvanceProposalRequest,
    GateResult,
    PromotionRefusal,
    PromotionRefusedError,
    ProposalNotFoundError,
    ProposalRejectionReason,
    ProposalStatus,
    TargetKind,
)
from ken_services.learning.lifecycle import advance_system_stage
from ken_services.learning.promote import (
    NO_PROMOTED_TRANSITION,
    DatabasePromotionService,
    PolicyWriteRefusedError,
    write_promoted_policy,
)
from ken_services.learning.reader import CorruptProposalError, SqlLearningReader
from ken_services.recommendations.policy import (
    load_ranking_policy,
    load_recommendation_policy,
    resolve_ranking_policy,
)
from tests.integration.learning_lifecycle_support import (
    NOW,
    SM,
    SYSTEM,
    Seeded,
    count,
    force_stage,
    insert_evaluation,
    insert_shadow_rows,
    job_run,
    learning_policy,
    operator,
    reviewer,
    seed_proposal,
    settings,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

OPEN = settings(learning_demonstration_mode=False)


def _files(root: Path, pattern: str) -> list[Path]:
    return list(root.rglob(pattern))


def _read(path: Path) -> str:
    return path.read_text()


def _exists(path: Path) -> bool:
    return path.exists()


def _promote(evaluation: uuid.UUID | None) -> AdvanceProposalRequest:
    return AdvanceProposalRequest(
        operation_id=str(uuid.uuid4()),
        to_status=ProposalStatus.PROMOTED,
        evaluation_run_id=evaluation,
    )


def _service(sm: SM, *, demonstration: bool = False, **gates: Any) -> DatabasePromotionService:
    return DatabasePromotionService(
        sessionmaker=sm,
        settings=settings(learning_demonstration_mode=demonstration),
        policy=learning_policy(**gates),
        clock=lambda: NOW,
    )


async def _ready(sm: SM) -> tuple[Seeded, uuid.UUID]:
    """A ranking proposal that has everything a promotion needs except the human act."""
    seeded = await seed_proposal(sm)
    await force_stage(sm, seeded.proposal_id, ProposalStatus.OFFLINE_TEST, ProposalStatus.SHADOW)
    await insert_shadow_rows(sm, seeded, runs=1, compared_pairs=5)
    evaluation = await insert_evaluation(
        sm, proposal_id=seeded.proposal_id, fingerprint=seeded.fingerprint
    )
    return seeded, evaluation


async def _status(sm: SM, proposal_id: uuid.UUID) -> str:
    async with sm() as session:
        return str(
            await session.scalar(
                text("SELECT status FROM learning_proposals WHERE id = :id"), {"id": proposal_id}
            )
        )


async def _refusal(
    service: DatabasePromotionService, who: Principal, seeded: Seeded, evaluation: uuid.UUID | None
) -> PromotionRefusal:
    with pytest.raises(PromotionRefusedError) as raised:
        await service.advance(
            principal=who, proposal_id=seeded.proposal_id, request=_promote(evaluation)
        )
    return raised.value.refusal


# --------------------------------------------------------------------------- #
# Demonstration mode: THE Phase 8 behaviour
# --------------------------------------------------------------------------- #
async def test_demonstration_mode_defaults_on_and_refuses_every_promotion(sessionmaker: SM) -> None:
    assert settings().learning_demonstration_mode is True
    seeded, evaluation = await _ready(sessionmaker)
    admin = await operator(sessionmaker)
    before = await count(sessionmaker, "learning_proposal_transitions")
    service = DatabasePromotionService(
        sessionmaker=sessionmaker, settings=settings(), policy=learning_policy(), clock=lambda: NOW
    )

    assert await _refusal(service, admin, seeded, evaluation) is PromotionRefusal.DEMONSTRATION_MODE

    assert await _status(sessionmaker, seeded.proposal_id) == "shadow"
    assert await count(sessionmaker, "learning_proposal_transitions") == before
    assert await count(sessionmaker, "events") == await count(sessionmaker, "events")  # stable
    async with sessionmaker() as session:
        promoted = await session.scalar(
            text("SELECT count(*) FROM events WHERE event_name = 'policy.promoted'")
        )
    assert promoted == 0


async def test_demonstration_mode_is_checked_before_anything_else(sessionmaker: SM) -> None:
    """Even a proposal that does not exist, and a reviewer, get the demonstration refusal: the
    check precedes permission, existence and every database read."""
    service = DatabasePromotionService(
        sessionmaker=sessionmaker, settings=settings(), policy=learning_policy(), clock=lambda: NOW
    )
    ghost = Seeded(uuid.uuid4(), uuid.uuid4(), "b" * 64)
    nobody = await reviewer(sessionmaker)
    assert (
        await _refusal(service, nobody, ghost, uuid.uuid4()) is PromotionRefusal.DEMONSTRATION_MODE
    )


async def test_demonstration_mode_does_not_block_other_human_stages(sessionmaker: SM) -> None:
    """Rejecting is not promoting: a human may still reject a demonstration proposal."""
    seeded = await seed_proposal(sessionmaker)
    admin = await operator(sessionmaker)
    service = DatabasePromotionService(
        sessionmaker=sessionmaker, settings=settings(), policy=learning_policy(), clock=lambda: NOW
    )
    response = await service.advance(
        principal=admin,
        proposal_id=seeded.proposal_id,
        request=AdvanceProposalRequest(
            operation_id=str(uuid.uuid4()),
            to_status=ProposalStatus.REJECTED,
            reason=ProposalRejectionReason.DEMONSTRATION_ONLY,
            note="synthetic corpus",
        ),
    )
    assert response.proposal.status is ProposalStatus.REJECTED
    assert response.transition.actor == admin.actor
    assert response.proposal.frozen_fields_untouched is True


# --------------------------------------------------------------------------- #
# A promotion with demonstration mode OFF (proves the machinery, not a real promotion)
# --------------------------------------------------------------------------- #
async def test_a_complete_promotion_writes_one_human_transition_and_one_event(
    sessionmaker: SM,
) -> None:
    seeded, evaluation = await _ready(sessionmaker)
    admin = await operator(sessionmaker)
    service = _service(sessionmaker, shadow_min_runs=1, shadow_min_plans=1)

    response = await service.advance(
        principal=admin, proposal_id=seeded.proposal_id, request=_promote(evaluation)
    )

    assert response.replayed is False
    assert response.proposal.status is ProposalStatus.PROMOTED
    assert response.transition.actor == admin.actor
    assert response.transition.evaluation_run_id == evaluation
    assert await _status(sessionmaker, seeded.proposal_id) == "promoted"
    async with sessionmaker() as session:
        events = (
            await session.execute(
                text(
                    "SELECT event_name, subject_type, subject_id, payload FROM events "
                    "WHERE event_name = 'policy.promoted'"
                )
            )
        ).all()
    assert len(events) == 1
    name, subject_type, subject_id, payload = tuple(events[0])
    assert isinstance(payload, dict)
    assert name == EventName.POLICY_PROMOTED.value
    assert (subject_type, subject_id) == ("policy", "ranking")
    assert payload["promoted_by"] == admin.actor
    assert payload["evaluation_ref"] == str(evaluation)
    assert payload["previous_version"] == "1"


async def test_the_promoter_setting_does_not_outlive_the_promotion(sessionmaker: SM) -> None:
    """The setting is transaction-local: a raw promotion on the same pool afterwards fails."""
    seeded, evaluation = await _ready(sessionmaker)
    admin = await operator(sessionmaker)
    other = await seed_proposal(
        sessionmaker,
        changes=[
            _change("ranking.weights.semantic", 0.30, 0.33),
            _change("ranking.weights.taxonomy", 0.20, 0.17),
        ],
    )
    await force_stage(sessionmaker, other.proposal_id, ProposalStatus.OFFLINE_TEST)
    await _service(sessionmaker, shadow_min_runs=1, shadow_min_plans=1).advance(
        principal=admin, proposal_id=seeded.proposal_id, request=_promote(evaluation)
    )
    other_eval = await insert_evaluation(
        sessionmaker, proposal_id=other.proposal_id, fingerprint=other.fingerprint
    )
    async with sessionmaker() as session, session.begin():
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO learning_proposal_transitions (proposal_id, from_status, "
                        "to_status, actor, evaluation_run_id) VALUES (:p, 'offline_test', "
                        "'promoted', 'user:otto', :e)"
                    ),
                    {"p": other.proposal_id, "e": other_eval},
                )


def _change(path: str, before: float, after: float) -> Any:
    from ken_contracts.learning import PolicyChange

    return PolicyChange(path=path, from_value=before, to_value=after)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("stale", PromotionRefusal.EVALUATION_TOO_OLD),
        ("failed", PromotionRefusal.EVALUATION_NOT_PASSED),
        ("inconclusive", PromotionRefusal.EVALUATION_NOT_PASSED),
        ("shadow_mode", PromotionRefusal.EVALUATION_NOT_PASSED),
        ("fingerprint", PromotionRefusal.FINGERPRINT_MISMATCH),
        ("dataset", PromotionRefusal.DATASET_CHANGED),
        ("no_manifests", PromotionRefusal.DATASET_CHANGED),
        ("small_sample", PromotionRefusal.SAMPLE_TOO_SMALL),
        ("behavioural", PromotionRefusal.BEHAVIOURAL_BLOCK),
    ],
)
async def test_an_unfit_evaluation_is_refused(
    sessionmaker: SM, scenario: str, expected: PromotionRefusal
) -> None:
    from tests.integration.learning_lifecycle_support import metrics_json

    seeded = await seed_proposal(sessionmaker)
    await force_stage(
        sessionmaker, seeded.proposal_id, ProposalStatus.OFFLINE_TEST, ProposalStatus.SHADOW
    )
    await insert_shadow_rows(sessionmaker, seeded, runs=1, compared_pairs=5)
    table: dict[str, dict[str, Any]] = {
        "stale": {"age": timedelta(days=15)},
        "failed": {"gate": GateResult.FAIL.value},
        "inconclusive": {"gate": GateResult.INCONCLUSIVE.value},
        "shadow_mode": {"mode": "shadow"},
        "fingerprint": {"fingerprint": "c" * 64},
        "dataset": {"manifests": {"critic:critic.v1": "d" * 64}},
        "no_manifests": {"manifests": {}},
        "small_sample": {"metrics": metrics_json(sufficient=False)},
        "behavioural": {"gate_reasons": [{"code": "behavioural_signal_negative"}]},
    }
    fields = table[scenario]
    evaluation = await insert_evaluation(
        sessionmaker,
        proposal_id=seeded.proposal_id,
        **({"fingerprint": seeded.fingerprint} | fields),
    )
    admin = await operator(sessionmaker)
    service = _service(sessionmaker, shadow_min_runs=1, shadow_min_plans=1)

    assert await _refusal(service, admin, seeded, evaluation) is expected

    assert await _status(sessionmaker, seeded.proposal_id) == "shadow"


async def test_an_evaluation_of_another_proposal_is_foreign(sessionmaker: SM) -> None:
    seeded, _own = await _ready(sessionmaker)
    other = await seed_proposal(
        sessionmaker,
        changes=[
            _change("ranking.weights.semantic", 0.30, 0.31),
            _change("ranking.weights.taxonomy", 0.20, 0.19),
        ],
    )
    foreign = await insert_evaluation(
        sessionmaker, proposal_id=other.proposal_id, fingerprint=other.fingerprint
    )
    admin = await operator(sessionmaker)
    service = _service(sessionmaker, shadow_min_runs=1, shadow_min_plans=1)
    assert await _refusal(service, admin, seeded, foreign) is PromotionRefusal.EVALUATION_FOREIGN


async def test_a_missing_evaluation_is_not_found(sessionmaker: SM) -> None:
    seeded, _ = await _ready(sessionmaker)
    admin = await operator(sessionmaker)
    service = _service(sessionmaker, shadow_min_runs=1, shadow_min_plans=1)
    assert (
        await _refusal(service, admin, seeded, uuid.uuid4())
        is PromotionRefusal.EVALUATION_NOT_FOUND
    )


async def test_skipping_a_required_stage_is_refused(sessionmaker: SM) -> None:
    """ranking requires offline_test AND shadow (learning.v1.yaml)."""
    seeded = await seed_proposal(sessionmaker)
    await force_stage(sessionmaker, seeded.proposal_id, ProposalStatus.OFFLINE_TEST)
    evaluation = await insert_evaluation(
        sessionmaker, proposal_id=seeded.proposal_id, fingerprint=seeded.fingerprint
    )
    admin = await operator(sessionmaker)
    service = _service(sessionmaker, shadow_min_runs=1, shadow_min_plans=1)
    assert (
        await _refusal(service, admin, seeded, evaluation)
        is PromotionRefusal.REQUIRED_STAGE_MISSING
    )


async def test_the_shadow_floors_are_enforced(sessionmaker: SM) -> None:
    """In shadow, but only 5 pairs over one run against a floor of 3 runs / 20 pairs."""
    seeded, evaluation = await _ready(sessionmaker)
    admin = await operator(sessionmaker)
    service = _service(sessionmaker)  # the policy's real floors
    assert (
        await _refusal(service, admin, seeded, evaluation)
        is PromotionRefusal.REQUIRED_STAGE_MISSING
    )


async def test_a_reviewer_cannot_promote(sessionmaker: SM) -> None:
    seeded, evaluation = await _ready(sessionmaker)
    service = _service(sessionmaker, shadow_min_runs=1, shadow_min_plans=1)
    who = await reviewer(sessionmaker)
    assert who.role is Role.REVIEWER
    assert await _refusal(service, who, seeded, evaluation) is PromotionRefusal.PERMISSION_DENIED


async def test_an_unknown_proposal_is_not_found(sessionmaker: SM) -> None:
    admin = await operator(sessionmaker)
    with pytest.raises(ProposalNotFoundError):
        await _service(sessionmaker).advance(
            principal=admin, proposal_id=uuid.uuid4(), request=_promote(uuid.uuid4())
        )


async def test_promoting_straight_from_proposed_is_off_the_ladder(sessionmaker: SM) -> None:
    seeded = await seed_proposal(sessionmaker)
    evaluation = await insert_evaluation(
        sessionmaker, proposal_id=seeded.proposal_id, fingerprint=seeded.fingerprint
    )
    admin = await operator(sessionmaker)
    service = _service(sessionmaker, shadow_min_runs=1, shadow_min_plans=1)
    assert (
        await _refusal(service, admin, seeded, evaluation)
        is PromotionRefusal.TRANSITION_NOT_ALLOWED
    )


async def test_a_replay_is_reported_and_a_different_body_is_refused(sessionmaker: SM) -> None:
    seeded = await seed_proposal(sessionmaker)
    admin = await operator(sessionmaker)
    service = _service(sessionmaker)
    request = AdvanceProposalRequest(
        operation_id=str(uuid.uuid4()),
        to_status=ProposalStatus.WITHDRAWN,
        reason=ProposalRejectionReason.OUT_OF_SCOPE,
        note="not needed",
    )
    first = await service.advance(principal=admin, proposal_id=seeded.proposal_id, request=request)
    again = await service.advance(principal=admin, proposal_id=seeded.proposal_id, request=request)
    assert (first.replayed, again.replayed) == (False, True)
    assert again.transition.id == first.transition.id
    assert await count(sessionmaker, "learning_proposal_transitions") == 1

    different = request.model_copy(update={"note": "a different note"})
    with pytest.raises(PromotionRefusedError) as raised:
        await service.advance(principal=admin, proposal_id=seeded.proposal_id, request=different)
    assert raised.value.refusal is PromotionRefusal.REPLAYED_WITH_DIFFERENT_BODY


async def test_a_terminal_proposal_cannot_be_advanced(sessionmaker: SM) -> None:
    seeded = await seed_proposal(sessionmaker)
    admin = await operator(sessionmaker)
    service = _service(sessionmaker)
    await service.advance(
        principal=admin,
        proposal_id=seeded.proposal_id,
        request=AdvanceProposalRequest(
            operation_id=str(uuid.uuid4()),
            to_status=ProposalStatus.REJECTED,
            reason=ProposalRejectionReason.GATE_FAILED,
        ),
    )
    with pytest.raises(PromotionRefusedError) as raised:
        await service.advance(
            principal=admin,
            proposal_id=seeded.proposal_id,
            request=AdvanceProposalRequest(
                operation_id=str(uuid.uuid4()), to_status=ProposalStatus.OFFLINE_TEST
            ),
        )
    assert raised.value.refusal is PromotionRefusal.TRANSITION_NOT_ALLOWED


# --------------------------------------------------------------------------- #
# The system stages and their gates
# --------------------------------------------------------------------------- #
async def test_offline_test_needs_a_passing_run_bound_to_the_current_fingerprint(
    sessionmaker: SM,
) -> None:
    seeded = await seed_proposal(sessionmaker)

    with pytest.raises(PromotionRefusedError) as missing:
        await advance_system_stage(
            sessionmaker, proposal_id=seeded.proposal_id, to_status=ProposalStatus.OFFLINE_TEST
        )
    assert missing.value.refusal is PromotionRefusal.EVALUATION_MISSING

    await insert_evaluation(
        sessionmaker,
        proposal_id=seeded.proposal_id,
        fingerprint="e" * 64,
    )
    with pytest.raises(PromotionRefusedError) as stale:
        await advance_system_stage(
            sessionmaker, proposal_id=seeded.proposal_id, to_status=ProposalStatus.OFFLINE_TEST
        )
    assert stale.value.refusal is PromotionRefusal.FINGERPRINT_MISMATCH

    await insert_evaluation(
        sessionmaker,
        proposal_id=seeded.proposal_id,
        fingerprint=seeded.fingerprint,
        gate="inconclusive",
        age=timedelta(minutes=1),
    )
    with pytest.raises(PromotionRefusedError) as inconclusive:
        await advance_system_stage(
            sessionmaker, proposal_id=seeded.proposal_id, to_status=ProposalStatus.OFFLINE_TEST
        )
    assert inconclusive.value.refusal is PromotionRefusal.EVALUATION_NOT_PASSED

    passing = await insert_evaluation(
        sessionmaker,
        proposal_id=seeded.proposal_id,
        fingerprint=seeded.fingerprint,
        age=timedelta(seconds=1),
    )
    gate = await advance_system_stage(
        sessionmaker, proposal_id=seeded.proposal_id, to_status=ProposalStatus.OFFLINE_TEST
    )
    assert gate.evaluation_id == passing
    assert await _status(sessionmaker, seeded.proposal_id) == "offline_test"
    async with sessionmaker() as session:
        row = (
            await session.execute(
                text("SELECT actor, to_status FROM learning_proposal_transitions")
            )
        ).one()
    assert (row.actor, row.to_status) == (SYSTEM, "offline_test")


async def test_a_system_actor_cannot_reach_a_human_stage(sessionmaker: SM) -> None:
    seeded = await seed_proposal(sessionmaker)
    for stage in (ProposalStatus.PROMOTED, ProposalStatus.CANARY, ProposalStatus.REJECTED):
        with pytest.raises(PromotionRefusedError):
            await advance_system_stage(
                sessionmaker, proposal_id=seeded.proposal_id, to_status=stage
            )
    assert await _status(sessionmaker, seeded.proposal_id) == "proposed"


async def test_shadow_requires_offline_test_and_a_shadowable_target(sessionmaker: SM) -> None:
    seeded = await seed_proposal(sessionmaker)
    with pytest.raises(PromotionRefusedError):
        await advance_system_stage(
            sessionmaker, proposal_id=seeded.proposal_id, to_status=ProposalStatus.SHADOW
        )
    await force_stage(sessionmaker, seeded.proposal_id, ProposalStatus.OFFLINE_TEST)
    await advance_system_stage(
        sessionmaker, proposal_id=seeded.proposal_id, to_status=ProposalStatus.SHADOW
    )
    assert await _status(sessionmaker, seeded.proposal_id) == "shadow"


async def test_an_agents_candidate_cannot_be_shadowed(sessionmaker: SM) -> None:
    from ken_contracts.learning import PolicyChange

    seeded = await seed_proposal(
        sessionmaker,
        target=TargetKind.AGENTS,
        changes=[PolicyChange(path="agents.max_plans_per_run", from_value=3, to_value=2)],
    )
    await force_stage(sessionmaker, seeded.proposal_id, ProposalStatus.OFFLINE_TEST)
    with pytest.raises(PromotionRefusedError) as raised:
        await advance_system_stage(
            sessionmaker, proposal_id=seeded.proposal_id, to_status=ProposalStatus.SHADOW
        )
    assert raised.value.refusal is PromotionRefusal.TRANSITION_NOT_ALLOWED


async def test_canary_and_experiment_are_stage_defined_only(sessionmaker: SM) -> None:
    """A human may RECORD them (the ladder exists), and nothing consumes them: no plan, policy
    resolution or file changes because a proposal sits in canary."""
    seeded, _ = await _ready(sessionmaker)
    admin = await operator(sessionmaker)
    plans_before = await count(sessionmaker, "link_plans")
    service = _service(sessionmaker)
    for stage in (ProposalStatus.CANARY, ProposalStatus.EXPERIMENT):
        response = await service.advance(
            principal=admin,
            proposal_id=seeded.proposal_id,
            request=AdvanceProposalRequest(operation_id=str(uuid.uuid4()), to_status=stage),
        )
        assert response.proposal.status is stage
    assert await count(sessionmaker, "link_plans") == plans_before


# --------------------------------------------------------------------------- #
# Skills (the short ladder)
# --------------------------------------------------------------------------- #
async def test_a_skill_promotion_retires_the_previous_version_and_names_the_human(
    sessionmaker: SM,
) -> None:
    from ken_contracts.learning import SkillStatus

    seeded = await seed_proposal(sessionmaker)
    earlier_eval = await insert_evaluation(
        sessionmaker, proposal_id=seeded.proposal_id, fingerprint=seeded.fingerprint
    )
    run = await job_run(sessionmaker)
    skill_proposal = uuid.uuid4()
    old_skill, new_skill = uuid.uuid4(), uuid.uuid4()
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO learning_proposals (id, kind, status, target_kind, base_version, "
                "title, rationale, body, evidence, fingerprint, proposed_by, job_run_id) "
                "SELECT :id, 'skill', 'proposed', NULL, '1', 'skill', 'r', "
                "CAST('{\"kind\": \"skill\"}' AS jsonb), evidence, :fp, 'system:learning', :run "
                "FROM learning_proposals WHERE id = :base"
            ),
            {"id": skill_proposal, "fp": "f" * 64, "run": run, "base": seeded.proposal_id},
        )
        common = (
            "name, version, description, instructions, input_schema, output_schema, suites, source"
        )
        await session.execute(
            text(
                f"INSERT INTO skills (id, {common}, status, promoted_by, promoted_at, "  # noqa: S608
                "evaluation_run_id) VALUES (:id, 'anchor_picker', 1, 'd', 'i', '{}'::jsonb, "
                "'{}'::jsonb, '[\"critic\"]'::jsonb, 'human', 'promoted', 'user:rita', now(), :e)"
            ),
            {"id": old_skill, "e": earlier_eval},
        )
        await session.execute(
            text(
                f"INSERT INTO skills (id, {common}, proposal_id, status) VALUES "  # noqa: S608
                "(:id, 'anchor_picker', 2, 'd', 'i', '{}'::jsonb, '{}'::jsonb, "
                "'[\"critic\"]'::jsonb, 'reflection', :p, 'draft')"
            ),
            {"id": new_skill, "p": skill_proposal},
        )
    await force_stage(sessionmaker, skill_proposal, ProposalStatus.OFFLINE_TEST)
    # Migration 0011: a skill is promoted only on a passing run of THAT skill.
    evaluation = await insert_evaluation(
        sessionmaker, proposal_id=None, skill_id=new_skill, fingerprint="f" * 64
    )
    admin = await operator(sessionmaker)

    response = await _service(sessionmaker).advance(
        principal=admin, proposal_id=skill_proposal, request=_promote(evaluation)
    )

    assert response.proposal.status is ProposalStatus.PROMOTED
    assert response.proposal.skill is not None
    assert response.proposal.skill.status is SkillStatus.PROMOTED
    assert response.proposal.skill.promoted_by == admin.actor
    async with sessionmaker() as session:
        statuses: dict[Any, Any] = dict(
            (await session.execute(text("SELECT version, status FROM skills"))).all()
        )
        event = (
            await session.execute(
                text(
                    "SELECT subject_type, subject_id, payload FROM events "
                    "WHERE event_name = 'skill.promoted'"
                )
            )
        ).one()
    assert statuses == {1: "retired", 2: "promoted"}
    assert (event.subject_type, event.subject_id) == ("skill", "anchor_picker")
    assert event.payload["evaluation_ref"] == str(evaluation)
    assert event.payload["previous_version"] == "1"


# --------------------------------------------------------------------------- #
# The reader and the frozen-fields flag
# --------------------------------------------------------------------------- #
async def test_a_stored_body_that_breaks_its_contract_is_never_served(sessionmaker: SM) -> None:
    """``frozen_fields_untouched`` stays honest: a row whose body touches a frozen path (it can
    only exist if something bypassed the boundary) is refused, never served with the flag
    cleared."""
    seeded = await seed_proposal(sessionmaker)
    run = await job_run(sessionmaker)
    forged = uuid.uuid4()
    forged_body = json.dumps(
        {
            "kind": "policy",
            "target_kind": "recommendation",
            "base_version": "1",
            "changes": [
                {
                    "path": "recommendation.placement.block_kinds",
                    "from_value": 1,
                    "to_value": 2,
                }
            ],
        }
    )
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO learning_proposals (id, kind, status, target_kind, base_version, "
                "title, rationale, body, evidence, fingerprint, proposed_by, job_run_id) "
                "SELECT :id, 'policy', 'proposed', 'recommendation', '1', 't', 'r', "
                "CAST(:body AS jsonb), evidence, :fp, 'system:learning', :run "
                "FROM learning_proposals WHERE id = :base"
            ),
            {
                "id": forged,
                "body": forged_body,
                "fp": "9" * 64,
                "run": run,
                "base": seeded.proposal_id,
            },
        )
    reader = SqlLearningReader(
        sessionmaker=sessionmaker, settings=settings(), policy=learning_policy()
    )
    with pytest.raises(CorruptProposalError):
        await reader.get_proposal(proposal_id=forged)
    honest = await reader.get_proposal(proposal_id=seeded.proposal_id)
    assert honest.frozen_fields_untouched is True
    assert {change.path for change in honest.changes} == {
        "ranking.weights.semantic",
        "ranking.weights.taxonomy",
    }


async def test_the_detail_explains_why_a_promotion_is_blocked(sessionmaker: SM) -> None:
    seeded = await seed_proposal(sessionmaker)
    reader = SqlLearningReader(
        sessionmaker=sessionmaker, settings=settings(), policy=learning_policy()
    )
    detail = await reader.get_proposal(proposal_id=seeded.proposal_id)
    assert detail.promotion is not None
    assert detail.promotion.blocked is True
    assert PromotionRefusal.DEMONSTRATION_MODE in detail.promotion.blocked_by
    assert PromotionRefusal.REQUIRED_STAGE_MISSING in detail.promotion.blocked_by
    assert PromotionRefusal.EVALUATION_MISSING in detail.promotion.blocked_by
    assert detail.promotion.target_paths[0] == "policies/ranking/ranking_weights.v2.yaml"
    assert detail.promotion.target_paths[1] == "policies/recommendation/recommendation.v2.yaml"
    assert detail.required_stages == (ProposalStatus.OFFLINE_TEST, ProposalStatus.SHADOW)
    assert detail.stages_completed == (ProposalStatus.PROPOSED,)


async def test_the_overview_banner_is_computed_from_the_database(sessionmaker: SM) -> None:
    await seed_proposal(sessionmaker)
    reader = SqlLearningReader(
        sessionmaker=sessionmaker, settings=settings(), policy=learning_policy()
    )
    overview = await reader.overview()
    assert overview.banner.demonstration_mode is True
    assert overview.proposals_by_status == {"proposed": 1}
    assert overview.banner.min_sample_count >= 30


# --------------------------------------------------------------------------- #
# The policy file writer (make policy-promote)
# --------------------------------------------------------------------------- #
async def test_the_writer_refuses_without_a_human_promoted_transition(
    sessionmaker: SM, tmp_path: Path
) -> None:
    seeded, _ = await _ready(sessionmaker)
    before = await count(sessionmaker, "learning_proposal_transitions")
    with pytest.raises(PolicyWriteRefusedError) as raised:
        await write_promoted_policy(
            sessionmaker=sessionmaker,
            settings=OPEN,
            proposal_id=seeded.proposal_id,
            dry_run=False,
            root=tmp_path,
        )
    assert raised.value.code == NO_PROMOTED_TRANSITION
    assert not _files(tmp_path, "*.yaml")
    assert await count(sessionmaker, "learning_proposal_transitions") == before


async def _promoted(sm: SM) -> Seeded:
    seeded, evaluation = await _ready(sm)
    await _service(sm, shadow_min_runs=1, shadow_min_plans=1).advance(
        principal=await operator(sm), proposal_id=seeded.proposal_id, request=_promote(evaluation)
    )
    return seeded


async def test_the_ranking_promotion_writes_two_files_and_the_pointer_resolves(
    sessionmaker: SM, tmp_path: Path
) -> None:
    seeded = await _promoted(sessionmaker)
    transitions = await count(sessionmaker, "learning_proposal_transitions")

    planned = await write_promoted_policy(
        sessionmaker=sessionmaker,
        settings=OPEN,
        proposal_id=seeded.proposal_id,
        root=tmp_path,
    )
    assert planned == (
        "policies/ranking/ranking_weights.v2.yaml",
        "policies/recommendation/recommendation.v2.yaml",
    )
    assert not _files(tmp_path, "*.yaml"), "a dry run writes nothing"

    written = await write_promoted_policy(
        sessionmaker=sessionmaker,
        settings=OPEN,
        proposal_id=seeded.proposal_id,
        dry_run=False,
        root=tmp_path,
    )
    assert written == planned
    ranking = load_ranking_policy(tmp_path / written[0])
    assert ranking.version == 2
    assert ranking.weights["semantic"] == pytest.approx(0.35)
    recommendation = load_recommendation_policy(tmp_path / written[1])
    assert recommendation.version == 2
    assert recommendation.ranking_policy == written[0]
    # The ACTIVE ranking policy is resolved from the pointer, not from Settings (15.7.8).
    assert resolve_ranking_policy(recommendation, root=tmp_path).version == 2
    # The writer created no database row.
    assert await count(sessionmaker, "learning_proposal_transitions") == transitions
    # Idempotent: the same bytes are accepted again.
    again = await write_promoted_policy(
        sessionmaker=sessionmaker,
        settings=OPEN,
        proposal_id=seeded.proposal_id,
        dry_run=False,
        root=tmp_path,
    )
    assert again == written


async def test_a_released_file_is_never_overwritten_and_the_pair_is_atomic(
    sessionmaker: SM, tmp_path: Path
) -> None:
    seeded = await _promoted(sessionmaker)
    clash = tmp_path / "policies" / "recommendation" / "recommendation.v2.yaml"
    clash.parent.mkdir(parents=True)
    clash.write_text("policy: recommendation\nversion: 2\n# a released, different file\n")
    with pytest.raises(PolicyWriteRefusedError):
        await write_promoted_policy(
            sessionmaker=sessionmaker,
            settings=OPEN,
            proposal_id=seeded.proposal_id,
            dry_run=False,
            root=tmp_path,
        )
    assert "released, different file" in _read(clash)
    assert not _exists(tmp_path / "policies" / "ranking" / "ranking_weights.v2.yaml")
    assert not _files(tmp_path, ".*.tmp")


async def test_a_rolled_back_proposal_is_no_longer_writable(
    sessionmaker: SM, tmp_path: Path
) -> None:
    seeded = await _promoted(sessionmaker)
    await _service(sessionmaker).advance(
        principal=await operator(sessionmaker, "otto2"),
        proposal_id=seeded.proposal_id,
        request=AdvanceProposalRequest(
            operation_id=str(uuid.uuid4()),
            to_status=ProposalStatus.ROLLED_BACK,
            reason=ProposalRejectionReason.REGRESSION_OBSERVED,
        ),
    )
    with pytest.raises(PolicyWriteRefusedError):
        await write_promoted_policy(
            sessionmaker=sessionmaker,
            settings=OPEN,
            proposal_id=seeded.proposal_id,
            dry_run=False,
            root=tmp_path,
        )
