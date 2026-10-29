"""SHADOW on the real pipeline (workstream D; ADR-0016 section 5, 17.1).

The load-bearing assertion is the first: **a shadow pass writes ZERO plan rows** -- and, more
broadly, nothing the production pipeline writes. It is a row-count assertion around the call
over every table a recommendation run touches; only ``policy_evaluations`` may grow, by exactly
one row with ``mode = shadow`` that is never a gate.

The second is fidelity: shadowing the BASELINE policy against its own run reports an empty
diff. That is what proves the shadow's restatement of the pipeline's selection has not drifted
from the pipeline itself; if ``pipeline.py`` changes how it selects, this test fails first.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from ken_contracts.learning import ProposalStatus, TargetKind
from ken_contracts.recommendations import RecommendationPolicy
from ken_services.learning.reader import SqlLearningReader
from ken_services.learning.shadow import (
    ShadowDiff,
    ShadowRefusal,
    ShadowRefusedError,
    shadow_pass,
)
from ken_services.recommendations.pipeline import load_policies
from tests.integration.learning_lifecycle_support import (
    SM,
    add_identity_candidate,
    count,
    force_stage,
    learning_policy,
    seed_proposal,
)
from tests.integration.recommendation_support import SETTINGS, recommend, seed_corpus

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

WRITTEN_BY_RECOMMENDATION = (
    "link_plans",
    "link_plan_transitions",
    "link_candidates",
    "opportunities",
    "events",
    "review_actions",
    "job_runs",
    "learning_proposals",
    "learning_proposal_transitions",
    "policy_candidates",
)


async def _world(sm: SM, tmp_path: Path) -> tuple[uuid.UUID, uuid.UUID]:
    """A real corpus, a real recommendation run, and a proposal in ``offline_test``."""
    await seed_corpus(sm, tmp_path)
    run = await recommend(sm)
    assert run.counters["plans_created"] > 0, run.counters
    seeded = await seed_proposal(sm)
    await force_stage(sm, seeded.proposal_id, ProposalStatus.OFFLINE_TEST)
    return run.id, seeded.proposal_id


async def _snapshot(sm: SM) -> dict[str, int]:
    return {table: await count(sm, table) for table in WRITTEN_BY_RECOMMENDATION}


async def _shadow(sm: SM, candidate_id: uuid.UUID, run_id: uuid.UUID) -> uuid.UUID:
    async with sm() as session, session.begin():
        return await shadow_pass(
            session,
            settings=SETTINGS,
            policy=learning_policy(),
            candidate_id=candidate_id,
            recommendation_run_id=run_id,
        )


async def _diff(sm: SM, evaluation_id: uuid.UUID) -> ShadowDiff:
    async with sm() as session:
        stored: dict[str, Any] = (
            await session.execute(
                text("SELECT metrics FROM policy_evaluations WHERE id = :id"),
                {"id": evaluation_id},
            )
        ).scalar_one()
    return ShadowDiff.model_validate(stored["shadow"])


def _baseline_ranking() -> dict[str, Any]:
    _policy, ranking = load_policies(SETTINGS)
    return ranking.model_dump(mode="json")


async def test_a_shadow_pass_writes_zero_plans_and_exactly_one_evaluation(
    sessionmaker: SM, tmp_path: Path
) -> None:
    run_id, proposal_id = await _world(sessionmaker, tmp_path)
    candidate = await add_identity_candidate(sessionmaker, proposal_id, _baseline_ranking())
    plans_before = await count(sessionmaker, "link_plans")
    evaluations_before = await count(sessionmaker, "policy_evaluations")
    before = await _snapshot(sessionmaker)

    evaluation_id = await _shadow(sessionmaker, candidate, run_id)

    assert await count(sessionmaker, "link_plans") == plans_before > 0
    assert await _snapshot(sessionmaker) == before, "a shadow pass wrote a row it must not write"
    assert await count(sessionmaker, "policy_evaluations") == evaluations_before + 1
    async with sessionmaker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT mode, gate_result, job_run_id, proposal_id, candidate_id "
                    "FROM policy_evaluations WHERE id = :id"
                ),
                {"id": evaluation_id},
            )
        ).one()
    assert row.mode == "shadow"
    assert row.gate_result == "inconclusive", "a shadow row is never a gate"
    assert row.job_run_id == run_id, "recorded inside the production job"
    assert (row.proposal_id, row.candidate_id) == (proposal_id, candidate)


async def test_shadowing_the_baseline_against_itself_reports_an_empty_diff(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """Fidelity of the restatement of the pipeline's selection."""
    run_id, proposal_id = await _world(sessionmaker, tmp_path)
    candidate = await add_identity_candidate(sessionmaker, proposal_id, _baseline_ranking())

    diff = await _diff(sessionmaker, await _shadow(sessionmaker, candidate, run_id))

    assert diff.production_plans > 0
    assert diff.candidate_plans == diff.production_plans
    assert (diff.added_count, diff.dropped_count, diff.reordered_count) == (0, 0, 0)
    assert diff.unchanged == diff.compared_pairs == diff.production_plans
    assert diff.score_deltas == ()
    assert diff.max_abs_score_delta is None or diff.max_abs_score_delta == 0.0
    assert "Not a claim" in diff.note


async def test_a_stricter_recommendation_policy_drops_pairs_and_adds_none(
    sessionmaker: SM, tmp_path: Path
) -> None:
    run_id, proposal_id = await _world(sessionmaker, tmp_path)
    policy, _ranking = load_policies(SETTINGS)
    document = policy.model_dump(mode="json")
    document["selection"]["min_score"] = 0.999
    RecommendationPolicy.model_validate(document)
    candidate = await add_identity_candidate(sessionmaker, proposal_id, document)
    before = await _snapshot(sessionmaker)

    diff = await _diff(sessionmaker, await _shadow(sessionmaker, candidate, run_id))

    assert diff.dropped_count == diff.production_plans > 0
    assert diff.added_count == 0
    assert diff.candidate_plans == 0
    assert await _snapshot(sessionmaker) == before


async def test_a_weights_candidate_reports_reorders_and_score_deltas_consistently(
    sessionmaker: SM, tmp_path: Path
) -> None:
    run_id, proposal_id = await _world(sessionmaker, tmp_path)
    document = _baseline_ranking()
    document["weights"]["semantic"], document["weights"]["taxonomy"] = 0.05, 0.45
    document["version"] = 2
    candidate = await add_identity_candidate(sessionmaker, proposal_id, document)

    evaluation_id = await _shadow(sessionmaker, candidate, run_id)
    diff = await _diff(sessionmaker, evaluation_id)

    assert diff.compared_pairs == (
        diff.unchanged + diff.reordered_count + diff.added_count + diff.dropped_count
    )
    assert diff.candidate_plans == diff.production_plans - diff.dropped_count + diff.added_count
    assert diff.mean_abs_score_delta is not None or diff.compared_pairs == diff.added_count
    # The reader serves the row as an ordinary evaluation (the extra `shadow` key is split off).
    reader = SqlLearningReader(
        sessionmaker=sessionmaker, settings=SETTINGS, policy=learning_policy()
    )
    detail = await reader.get_evaluation(evaluation_id=evaluation_id)
    assert detail.mode.value == "shadow"
    assert detail.gate_result.value == "inconclusive"
    assert detail.metrics.by_suite[0].suite.value == "policy-regression"


async def test_a_shadow_requires_a_proposal_that_passed_offline_test(
    sessionmaker: SM, tmp_path: Path
) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    run = await recommend(sessionmaker)
    seeded = await seed_proposal(sessionmaker)  # still `proposed`
    with pytest.raises(ShadowRefusedError) as raised:
        await _shadow(sessionmaker, seeded.candidate_id, run.id)
    assert raised.value.args == (ShadowRefusal.PROPOSAL_NOT_SHADOWABLE.value,)


async def test_an_agents_candidate_is_not_shadowable(sessionmaker: SM, tmp_path: Path) -> None:
    from ken_contracts.learning import PolicyChange

    await seed_corpus(sessionmaker, tmp_path)
    run = await recommend(sessionmaker)
    seeded = await seed_proposal(
        sessionmaker,
        target=TargetKind.AGENTS,
        changes=[PolicyChange(path="agents.max_plans_per_run", from_value=3, to_value=2)],
    )
    await force_stage(sessionmaker, seeded.proposal_id, ProposalStatus.OFFLINE_TEST)
    with pytest.raises(ShadowRefusedError) as raised:
        await _shadow(sessionmaker, seeded.candidate_id, run.id)
    assert raised.value.args == (ShadowRefusal.TARGET_NOT_SHADOWABLE.value,)


async def test_a_run_that_is_not_a_recommendation_run_is_refused(
    sessionmaker: SM, tmp_path: Path
) -> None:
    run_id, proposal_id = await _world(sessionmaker, tmp_path)
    candidate = await add_identity_candidate(sessionmaker, proposal_id, _baseline_ranking())
    async with sessionmaker() as session:
        other = (
            (await session.execute(text("SELECT id FROM job_runs WHERE kind <> 'recommendation'")))
            .scalars()
            .first()
        )
    assert other is not None and other != run_id
    with pytest.raises(ShadowRefusedError) as raised:
        await _shadow(sessionmaker, candidate, other)
    assert raised.value.args == (ShadowRefusal.NOT_A_RECOMMENDATION_RUN.value,)


async def test_a_baseline_that_moved_since_the_run_is_refused(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """The comparison would not be like for like if the policy files changed under the run."""
    run_id, proposal_id = await _world(sessionmaker, tmp_path)
    candidate = await add_identity_candidate(sessionmaker, proposal_id, _baseline_ranking())
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text(
                "UPDATE job_runs SET config_snapshot = jsonb_set(config_snapshot, "
                "'{policy,version}', '999') WHERE id = :id"
            ),
            {"id": run_id},
        )
    with pytest.raises(ShadowRefusedError) as raised:
        await _shadow(sessionmaker, candidate, run_id)
    assert raised.value.args == (ShadowRefusal.BASELINE_CHANGED.value,)
