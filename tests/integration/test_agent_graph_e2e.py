"""The agent supervisor end to end on the ``*_test`` database (ADR-0012).

Real corpus import, real Phase 2 analysis, the real deterministic engine (``make recommend``)
as the ONLY plan producer, then the real LangGraph supervisor over the database-backed
``DatabaseAgentBackend`` / ``DatabaseAgentRunStore`` inside a real ``agent`` job run. The
model is the deterministic ``MockModelProvider`` with recorded critic outputs.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

from ken_agents.runner import AgentRunResult, BatchJob, build_config_snapshot, run_agent_batch
from ken_contracts.agents import GRAPH_VERSION, AgentNode, CriticAssessment
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail
from ken_core.providers.model.mock import MockModelProvider
from ken_services.agents.backend import DatabaseAgentBackend
from ken_services.agents.policy import load_configured_agent_policy
from ken_services.agents.store import DatabaseAgentRunStore
from ken_services.jobs.runner import run_job
from ken_services.recommendations.pipeline import load_policies
from tests.integration.recommendation_support import (
    SETTINGS,
    SM,
    provider,
    recommend,
    rows,
    scalar,
    seed_corpus,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

POLICY = load_configured_agent_policy(SETTINGS)
RECOMMENDATION, RANKING = load_policies(SETTINGS)


def _critic(verdict: str) -> MockModelProvider:
    model = provider()
    model.register(
        CriticAssessment,
        {"critiques": [{"ref": f"c{i}", "verdict": verdict} for i in range(10)]},
    )
    return model


async def _agents(
    sessionmaker: SM,
    model: MockModelProvider | None,
    *,
    sources: list[uuid.UUID] | None = None,
    results: list[AgentRunResult] | None = None,
) -> JobRunDetail:
    async def start_job(
        *,
        kind: JobKind,
        triggered_by: str,
        body: Callable[[BatchJob], Awaitable[RunStatus]],
        config_snapshot: Mapping[str, Any],
        counter_names: Iterable[str],
    ) -> JobRunDetail:
        return await run_job(
            kind=kind,
            sessionmaker=sessionmaker,
            triggered_by=triggered_by,
            body=body,
            config_snapshot=config_snapshot,
            counter_names=counter_names,
        )

    return await run_agent_batch(
        start_job=start_job,
        backend=DatabaseAgentBackend(sessionmaker=sessionmaker, settings=SETTINGS),
        store=DatabaseAgentRunStore(sessionmaker=sessionmaker),
        provider=model,
        policy=POLICY,
        config_snapshot=build_config_snapshot(
            policy=POLICY,
            recommendation_policy_version=RECOMMENDATION.version,
            ranking_policy_version=RANKING.version,
            provider_configured=model is not None,
        ),
        triggered_by="test",
        source_page_ids=sources,
        results=results,
    )


async def _seed(sessionmaker: SM, tmp_path: Path) -> int:
    await seed_corpus(sessionmaker, tmp_path)
    run = await recommend(sessionmaker)
    assert run.status is RunStatus.SUCCEEDED, run.counters
    proposed = await scalar(sessionmaker, "SELECT count(*) FROM link_plans WHERE status='proposed'")
    assert proposed > 0
    return int(proposed)


async def test_batch_reviews_and_hands_off_every_proposed_plan(
    sessionmaker: SM, tmp_path: Path
) -> None:
    proposed = await _seed(sessionmaker, tmp_path)
    results: list[AgentRunResult] = []
    detail = await _agents(sessionmaker, _critic("accept"), results=results)
    assert detail.kind is JobKind.AGENT
    assert detail.status is RunStatus.SUCCEEDED, detail.counters
    assert detail.config_snapshot["graph_version"] == GRAPH_VERSION
    assert detail.counters["runs_failed"] == 0
    assert detail.counters["plans_handed_off"] == detail.counters["plans_reviewed"] > 0

    runs = await rows(
        sessionmaker, "SELECT id, status, outcome, trace_id, correlation_id FROM agent_runs"
    )
    assert len(runs) == len(results) == detail.counters["sources_considered"]
    assert {(r.status, r.outcome) for r in runs} == {("awaiting_approval", "recommend")}
    assert all(r.trace_id == uuid.UUID(str(r.correlation_id)).hex for r in runs)
    steps = await rows(sessionmaker, "SELECT run_id, seq FROM agent_steps ORDER BY run_id, seq")
    assert len(steps) == len(runs) * len(AgentNode)
    reviewed = await scalar(sessionmaker, "SELECT count(*) FROM agent_run_plans")
    awaiting = await scalar(
        sessionmaker, "SELECT count(*) FROM link_plans WHERE status='awaiting_approval'"
    )
    assert reviewed == awaiting == detail.counters["plans_handed_off"]
    assert reviewed <= proposed
    transitions = await rows(
        sessionmaker,
        "SELECT DISTINCT to_status, actor FROM link_plan_transitions "
        "WHERE to_status IN ('critiqued','policy_checked','awaiting_approval')",
    )
    assert {(t.to_status, t.actor) for t in transitions} == {
        ("critiqued", "agent:critic@graph-v1"),
        ("policy_checked", "system:agent-policy-gate"),
        ("awaiting_approval", "system:agent-policy-gate"),
    }
    approved = "SELECT count(*) FROM link_plans WHERE status='approved'"
    assert await scalar(sessionmaker, approved) == 0

    # A re-run never re-reviews a plan (and spends no model call on it).
    again = _critic("accept")
    second = await _agents(sessionmaker, again)
    assert second.counters["plans_reviewed"] == 0
    assert await scalar(sessionmaker, "SELECT count(*) FROM agent_run_plans") == reviewed


async def test_rejected_plans_stay_proposed_and_are_not_reviewed_again(
    sessionmaker: SM, tmp_path: Path
) -> None:
    proposed = await _seed(sessionmaker, tmp_path)
    detail = await _agents(sessionmaker, _critic("reject"))
    assert detail.status is RunStatus.SUCCEEDED, detail.counters
    assert detail.counters["plans_handed_off"] == 0
    outcomes = await rows(sessionmaker, "SELECT DISTINCT status, outcome FROM agent_runs")
    assert {(o.status, o.outcome) for o in outcomes} == {("completed", "no_action")}
    still = await scalar(sessionmaker, "SELECT count(*) FROM link_plans WHERE status='proposed'")
    assert still == proposed
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM agent_run_plans")
        == detail.counters["plans_reviewed"]
        == proposed
    )
    # The next batch has nothing left to review for those plans.
    assert (await _agents(sessionmaker, _critic("accept"))).counters["plans_reviewed"] == 0


async def test_no_model_uses_the_recorded_deterministic_rule(
    sessionmaker: SM, tmp_path: Path
) -> None:
    await _seed(sessionmaker, tmp_path)
    detail = await _agents(sessionmaker, provider())  # nothing registered: schema violation
    assert detail.status is RunStatus.SUCCEEDED, detail.counters
    assert detail.counters["fallbacks"] == detail.counters["sources_considered"]
    reviews = await rows(
        sessionmaker, "SELECT fallback_used, fallback_reason, model FROM agent_run_plans"
    )
    assert reviews
    assert {(r.fallback_used, r.fallback_reason, r.model) for r in reviews} == {
        (True, "schema_violation", None)
    }


async def test_manual_trigger_for_one_named_source(sessionmaker: SM, tmp_path: Path) -> None:
    await _seed(sessionmaker, tmp_path)
    source = await scalar(
        sessionmaker,
        "SELECT source_page_id FROM link_plans WHERE status='proposed' ORDER BY source_url LIMIT 1",
    )
    detail = await _agents(sessionmaker, None, sources=[uuid.UUID(str(source))])
    assert detail.counters["sources_considered"] == 1
    runs = await rows(sessionmaker, "SELECT trigger, source_page_id FROM agent_runs")
    assert [(r.trigger, str(r.source_page_id)) for r in runs] == [("manual", str(source))]
