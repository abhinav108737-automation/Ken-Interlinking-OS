"""The critic is a SIGNAL for the autonomy engine, never a decision (ADR-0018 s1, ADR-0019).

Real corpus, real engine plans, the real LangGraph supervisor over the database backend, and
the REAL autonomy engine reading what the agent run stored. The claims:

* a critic ``accept`` on a plan with a DETERMINISTIC BLOCK still ends in ``BLOCK`` -- the
  engine computes the blocks from stored facts first, and nothing the critic says can clear one;
* what the agent hands on arrives where the engine looks for it (``policy_checked``), and the
  engine reads the stored verdict back as ``ACCEPT``;
* ``uncertain`` and a degraded model are NO_ACTION: the engine declines them, they never become
  a decision, and nothing is published;
* ``reject`` is read back as REJECT and blocks;
* the agent run itself writes no decision, no deployment and no publish attempt.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from ken_agents.runner import AgentRunResult, build_config_snapshot, run_one_source
from ken_contracts.agents import AgentTrigger, CriticAssessment
from ken_contracts.autonomy import AutonomyDecision, BlockCode, CriticOutcome
from ken_contracts.model import ModelUnavailableError
from ken_core.providers.model.mock import MockModelProvider
from ken_services.agents.backend import DatabaseAgentBackend
from ken_services.agents.policy import load_configured_agent_policy
from ken_services.agents.store import DatabaseAgentRunStore
from ken_services.autonomy.compose import build_engine
from ken_services.autonomy.engine import CriticDeclinedError, DatabaseAutonomyEngine
from tests.integration.agent_support import job_run
from tests.integration.autonomy_support import Autonomy, autonomy, protect
from tests.integration.deployment_support import count
from tests.integration.recommendation_support import CHARGING, EV, SETTINGS, SM, rows

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

D = AutonomyDecision
POLICY = load_configured_agent_policy(SETTINGS)


@pytest.fixture
async def a(sessionmaker: SM, tmp_path: Path) -> Autonomy:
    return await autonomy(sessionmaker, tmp_path)


def _model(verdict: str) -> MockModelProvider:
    model = MockModelProvider(SETTINGS)
    model.register(
        CriticAssessment,
        {"critiques": [{"ref": f"c{i}", "verdict": verdict} for i in range(10)]},
    )
    return model


class _Unavailable(MockModelProvider):
    async def complete_structured(self, **kwargs: Any) -> Any:
        raise ModelUnavailableError("the model server is down")


async def _review(sessionmaker: SM, a: Autonomy, provider: MockModelProvider) -> AgentRunResult:
    source_id = a.world.plans[(EV, CHARGING)].source_page_id
    result = await run_one_source(
        source_page_id=source_id,
        job_run_id=await job_run(sessionmaker),
        trigger=AgentTrigger.MANUAL,
        backend=DatabaseAgentBackend(sessionmaker=sessionmaker, settings=a.settings),
        store=DatabaseAgentRunStore(sessionmaker=sessionmaker),
        provider=provider,
        policy=POLICY,
        config_snapshot=build_config_snapshot(
            policy=POLICY,
            recommendation_policy_version=1,
            ranking_policy_version=1,
            provider_configured=True,
        ),
    )
    assert result is not None and result.state is not None
    return result


def _engine(a: Autonomy, mode: str = "autonomous") -> DatabaseAutonomyEngine:
    """The engine through the autonomy package's own composition root (not a test helper that
    may lag it), in ``mode``."""
    settings = a.with_mode(mode, autonomy_autonomous_acknowledged=True).settings
    return build_engine(sessionmaker=a.sessionmaker, settings=settings, clock=a.clock)


async def test_an_agent_review_writes_no_decision_no_deployment_no_attempt(
    sessionmaker: SM, a: Autonomy
) -> None:
    result = await _review(sessionmaker, a, _model("accept"))
    assert result.plans_handed_off > 0
    for table in ("autonomy_decisions", "deployments", "publish_attempts"):
        assert await count(sessionmaker, table) == 0, table
    assert not await rows(
        sessionmaker,
        "SELECT 1 FROM link_plan_transitions WHERE actor LIKE 'agent:%' "
        "AND to_status NOT IN ('critiqued', 'policy_checked')",
    )


async def test_a_critic_accept_reaches_the_engine_as_an_accept(
    sessionmaker: SM, a: Autonomy
) -> None:
    result = await _review(sessionmaker, a, _model("accept"))
    assert result.state is not None
    plan_id = result.state.link_plans[0]

    evaluation = await _engine(a).evaluate(plan_id=plan_id)

    assert evaluation.critic_outcome is CriticOutcome.ACCEPT
    assert evaluation.decision is not D.BLOCK, evaluation.blocks
    assert evaluation.blocks == ()


async def test_a_critic_accept_cannot_clear_a_deterministic_block(
    sessionmaker: SM, a: Autonomy
) -> None:
    """THE safety claim. The critic accepted; the destination is then protected. The engine
    evaluates the block from stored facts FIRST, so the decision is BLOCK whatever was said."""
    result = await _review(sessionmaker, a, _model("accept"))
    assert result.state is not None
    plan_id = result.state.link_plans[0]
    target = (
        await rows(sessionmaker, "SELECT target_page_id FROM link_plans WHERE id = :p", p=plan_id)
    )[0].target_page_id
    await protect(sessionmaker, target)

    for mode in ("shadow", "guarded", "autonomous"):
        evaluation = await _engine(a, mode).evaluate(plan_id=plan_id)
        assert evaluation.critic_outcome is CriticOutcome.ACCEPT, "the critic did say accept"
        assert evaluation.decision is D.BLOCK, mode
        assert BlockCode.PROTECTED_PAGE in evaluation.blocks, mode

    recorded = await _engine(a).evaluate_and_record(plan_id=plan_id)
    assert recorded.decision is D.BLOCK
    assert await count(sessionmaker, "deployments") == 0, "a blocked plan publishes nothing"


async def test_uncertain_is_no_action_the_engine_declines_it_and_nothing_publishes(
    sessionmaker: SM, a: Autonomy
) -> None:
    result = await _review(sessionmaker, a, _model("uncertain"))
    assert result.plans_handed_off == 0 and result.plans_reviewed > 0
    stored = await rows(sessionmaker, "SELECT verdict, handed_off FROM agent_run_plans")
    assert {(r.verdict, r.handed_off) for r in stored} == {("uncertain", False)}

    engine = _engine(a)
    plan_id = next(iter(result.state.recorded_plans))  # type: ignore[union-attr]
    with pytest.raises(CriticDeclinedError):
        await engine.evaluate(plan_id=plan_id)
    source_id = a.world.plans[(EV, CHARGING)].source_page_id
    swept = await engine.evaluate_source(source_page_id=source_id)
    assert plan_id not in {e.plan_id for e in swept}, "a declined plan is never decided"
    assert await count(sessionmaker, "deployments") == 0


async def test_a_model_outage_degrades_to_no_action_never_to_an_accept(
    sessionmaker: SM, a: Autonomy
) -> None:
    result = await _review(sessionmaker, a, _Unavailable(SETTINGS))
    assert result.plans_handed_off == 0
    stored = await rows(
        sessionmaker, "SELECT verdict, handed_off, fallback_reason, model FROM agent_run_plans"
    )
    assert stored and {(r.verdict, r.handed_off, r.fallback_reason, r.model) for r in stored} == {
        ("uncertain", False, "model_unavailable", None)
    }
    plan_id = result.state.recorded_plans[0]  # type: ignore[union-attr]
    with pytest.raises(CriticDeclinedError):
        await _engine(a).evaluate(plan_id=plan_id)


async def test_a_critic_reject_is_read_back_as_reject_and_blocks(
    sessionmaker: SM, a: Autonomy
) -> None:
    result = await _review(sessionmaker, a, _model("reject"))
    plan_id: uuid.UUID = result.state.recorded_plans[0]  # type: ignore[union-attr]
    evaluation = await _engine(a).evaluate(plan_id=plan_id)
    assert evaluation.critic_outcome is CriticOutcome.REJECT
    assert evaluation.decision is D.BLOCK
