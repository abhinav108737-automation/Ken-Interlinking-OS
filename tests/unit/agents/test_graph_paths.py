"""Every supervisor path (ADR-0012 section 2), against the in-memory fakes."""

from __future__ import annotations

import uuid

import pytest

from ken_agents.runner import AgentRunResult
from ken_agents.state import RiskLevel
from ken_contracts.agents import (
    AgentErrorCode,
    AgentNode,
    AgentOutcome,
    AgentRunStatus,
    CriticAssessment,
    CriticVerdict,
    FallbackReason,
    StepStatus,
    TriageDecision,
)
from ken_core.providers.model.mock import MockModelProvider
from ken_services.agents.protocol import PlanCheck
from tests.unit.agents._support import (
    SOURCE_ID,
    FakeBackend,
    FakeStore,
    eligible,
    make_page,
    make_plan,
    make_policy,
    run_source,
)


def _nodes(result: AgentRunResult) -> list[AgentNode]:
    return [s.node for s in result.steps]


def _critic(provider: MockModelProvider, *verdicts: tuple[str, str]) -> None:
    provider.register(
        CriticAssessment,
        {"critiques": [{"ref": ref, "verdict": v, "reasons": []} for ref, v in verdicts]},
    )


async def test_missing_page_fails_with_page_not_found() -> None:
    backend = FakeBackend.ready([make_plan(1)])
    store = FakeStore()
    # The runner reads the page for the run's source_url; the page vanishes before load_page.
    original = backend.get_page
    calls = 0

    async def vanishing(*, page_id: uuid.UUID):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return await original(page_id=page_id) if calls == 1 else None

    backend.get_page = vanishing  # type: ignore[method-assign]
    result = await run_source(backend, store, MockModelProvider(dimensions=8), make_policy())
    assert _nodes(result) == [AgentNode.LOAD_PAGE, AgentNode.FINALIZE]
    assert result.status is AgentRunStatus.FAILED
    assert result.outcome is AgentOutcome.FAILED
    assert result.error_code is AgentErrorCode.PAGE_NOT_FOUND
    assert result.steps[0].status is StepStatus.FAILED
    assert result.steps[0].error_code is AgentErrorCode.PAGE_NOT_FOUND
    assert store.finished[result.run_id]["error_code"] is AgentErrorCode.PAGE_NOT_FOUND


async def test_unknown_source_starts_no_run() -> None:
    from ken_agents.runner import run_one_source
    from tests.unit.agents._support import snapshot

    store = FakeStore()
    policy = make_policy()
    result = await run_one_source(
        source_page_id=uuid.uuid4(),
        job_run_id=uuid.uuid4(),
        trigger="manual",  # type: ignore[arg-type]
        backend=FakeBackend.ready(),
        store=store,
        provider=None,
        policy=policy,
        config_snapshot=snapshot(policy),
    )
    assert result is None
    assert store.runs == {}


async def test_ineligible_source_is_no_action() -> None:
    backend = FakeBackend.ready([make_plan(1)])
    backend.eligibility[SOURCE_ID] = eligible(as_source=False)
    store = FakeStore()
    result = await run_source(backend, store, MockModelProvider(dimensions=8), make_policy())
    assert _nodes(result) == [AgentNode.LOAD_PAGE, AgentNode.TRIAGE, AgentNode.FINALIZE]
    assert result.outcome is AgentOutcome.NO_ACTION
    assert result.status is AgentRunStatus.COMPLETED
    assert store.finished[result.run_id]["triage_decision"] is TriageDecision.NO_ACTION
    assert any("not eligible" in n for n in store.finished[result.run_id]["review_notes"])
    assert backend.handoffs == []


async def test_gone_source_is_no_action() -> None:
    backend = FakeBackend.ready([make_plan(1)])
    backend.pages[SOURCE_ID] = make_page(gone=True)
    result = await run_source(backend, FakeStore(), None, make_policy())
    assert result.outcome is AgentOutcome.NO_ACTION
    assert result.state is not None
    assert result.state.triage_decision is TriageDecision.NO_ACTION


async def test_missing_analysis_is_reindex_and_ends() -> None:
    backend = FakeBackend.ready([make_plan(1)])
    del backend.analyses[SOURCE_ID]
    store = FakeStore()
    result = await run_source(backend, store, MockModelProvider(dimensions=8), make_policy())
    assert _nodes(result) == [AgentNode.LOAD_PAGE, AgentNode.TRIAGE, AgentNode.FINALIZE]
    assert result.outcome is AgentOutcome.NO_ACTION
    assert store.finished[result.run_id]["triage_decision"] is TriageDecision.REINDEX
    assert "list_proposed_plans" not in backend.calls


async def test_no_unreviewed_plans_is_no_action() -> None:
    backend = FakeBackend.ready([])
    result = await run_source(backend, FakeStore(), MockModelProvider(dimensions=8), make_policy())
    assert _nodes(result) == [AgentNode.LOAD_PAGE, AgentNode.TRIAGE, AgentNode.FINALIZE]
    assert result.outcome is AgentOutcome.NO_ACTION
    assert result.state is not None
    assert result.state.triage_decision is TriageDecision.NO_ACTION


async def test_all_accepted_recommends_and_hands_off() -> None:
    plans = [make_plan(i, confidence=0.9 - i * 0.1) for i in range(3)]
    backend = FakeBackend.ready(plans)
    provider = MockModelProvider(dimensions=8)
    _critic(provider, ("c0", "accept"), ("c1", "accept"), ("c2", "accept"))
    store = FakeStore()
    result = await run_source(backend, store, provider, make_policy())
    assert _nodes(result) == list(AgentNode)
    assert result.outcome is AgentOutcome.RECOMMEND
    assert result.status is AgentRunStatus.AWAITING_APPROVAL
    assert result.plans_handed_off == 3
    assert result.plans_reviewed == 3
    assert all(backend.status[p.id] == "awaiting_approval" for p in plans)
    assert len(backend.handoffs) == 1, "ONE hand-off call"
    assert result.state is not None
    assert result.state.risk_level is RiskLevel.LOW
    assert result.state.approval_required is True
    assert result.state.approval_reason == "AUTO_APPROVE disabled (TRD §20)"
    reviews = backend.handoffs[0][1]
    assert all(r.model == "mock-reasoning" and r.fallback_reason is None for r in reviews)


async def test_only_uncertain_needs_human_review() -> None:
    plans = [make_plan(i) for i in range(2)]
    backend = FakeBackend.ready(plans)
    provider = MockModelProvider(dimensions=8)
    _critic(provider, ("c0", "uncertain"), ("c1", "uncertain"))
    result = await run_source(backend, FakeStore(), provider, make_policy())
    assert result.outcome is AgentOutcome.NEEDS_HUMAN_REVIEW
    assert result.status is AgentRunStatus.AWAITING_APPROVAL
    assert result.plans_handed_off == 2
    assert result.state is not None
    assert result.state.risk_level is RiskLevel.MEDIUM


async def test_rejected_plan_is_recorded_not_handed_off() -> None:
    plans = [make_plan(i) for i in range(2)]
    backend = FakeBackend.ready(plans)
    provider = MockModelProvider(dimensions=8)
    _critic(provider, ("c0", "accept"), ("c1", "reject"))
    result = await run_source(backend, FakeStore(), provider, make_policy())
    assert result.outcome is AgentOutcome.RECOMMEND
    sent = backend.handoffs[0][1]
    assert {r.plan_id for r in sent} == {plans[0].id, plans[1].id}, "rejects are recorded"
    assert backend.status[plans[1].id] == "proposed", "a rejected plan stays proposed"
    assert result.state is not None
    assert result.state.link_plans == [plans[0].id]
    assert result.plans_reviewed == 2


async def test_all_rejected_is_no_action() -> None:
    backend = FakeBackend.ready([make_plan(0)])
    provider = MockModelProvider(dimensions=8)
    _critic(provider, ("c0", "reject"))
    result = await run_source(backend, FakeStore(), provider, make_policy())
    assert result.outcome is AgentOutcome.NO_ACTION
    assert result.status is AgentRunStatus.COMPLETED
    assert result.plans_reviewed == 1


async def test_invariant_failure_is_not_handed_off() -> None:
    plans = [make_plan(i) for i in range(2)]
    backend = FakeBackend.ready(plans)
    backend.checks[plans[1].id] = PlanCheck(ok=False, problems=("anchor_not_verbatim",))
    provider = MockModelProvider(dimensions=8)
    _critic(provider, ("c0", "accept"), ("c1", "accept"))
    store = FakeStore()
    result = await run_source(backend, store, provider, make_policy())
    sent = backend.handoffs[0][1]
    assert [r.plan_id for r in sent] == [plans[0].id], "failing plans are NOT sent"
    assert result.outcome is AgentOutcome.RECOMMEND
    assert result.state is not None
    assert result.state.risk_level is RiskLevel.HIGH
    notes = store.finished[result.run_id]["review_notes"]
    assert any("anchor_not_verbatim" in n for n in notes)


async def test_every_plan_failing_invariants_needs_human_review_without_hand_off() -> None:
    plan = make_plan(0)
    backend = FakeBackend.ready([plan])
    backend.checks[plan.id] = PlanCheck(ok=False, problems=("source_version_changed",))
    result = await run_source(backend, FakeStore(), None, make_policy(critic=False))
    assert backend.handoffs == []
    gate = next(s for s in result.steps if s.node is AgentNode.POLICY_GATE)
    assert gate.status is StepStatus.SKIPPED
    assert result.outcome is AgentOutcome.NEEDS_HUMAN_REVIEW
    assert result.status is AgentRunStatus.COMPLETED
    assert result.error_code is None


async def test_handoff_conflict_with_nothing_handed_off() -> None:
    plan = make_plan(0, confidence=0.9)
    backend = FakeBackend.ready([plan])
    backend.concurrent_expiry.add(plan.id)
    store = FakeStore()
    result = await run_source(backend, store, None, make_policy(critic=False))
    assert result.outcome is AgentOutcome.NEEDS_HUMAN_REVIEW
    assert result.status is AgentRunStatus.COMPLETED
    assert result.error_code is AgentErrorCode.HANDOFF_CONFLICT
    assert store.finished[result.run_id]["error_code"] is AgentErrorCode.HANDOFF_CONFLICT
    assert any("concurrently" in n for n in store.finished[result.run_id]["review_notes"])


async def test_partial_conflict_still_recommends() -> None:
    plans = [make_plan(0, confidence=0.9), make_plan(1, confidence=0.8)]
    backend = FakeBackend.ready(plans)
    backend.concurrent_expiry.add(plans[1].id)
    result = await run_source(backend, FakeStore(), None, make_policy(critic=False))
    assert result.outcome is AgentOutcome.RECOMMEND
    assert result.error_code is None
    assert result.state is not None
    assert result.state.conflicts == {plans[1].id: "not_proposed"}


async def test_max_plans_per_run_bounds_the_review() -> None:
    plans = [make_plan(i, confidence=0.9 - i * 0.05) for i in range(6)]
    backend = FakeBackend.ready(plans)
    result = await run_source(backend, FakeStore(), None, make_policy(critic=False, max_plans=2))
    assert result.state is not None
    assert result.state.ranked_candidates == [plans[0].id, plans[1].id], "engine order"
    assert len(backend.handoffs[0][1]) == 2


async def test_rerun_never_re_reviews_a_plan() -> None:
    plans = [make_plan(0, confidence=0.9), make_plan(1, confidence=0.1)]
    backend = FakeBackend.ready(plans)
    provider = MockModelProvider(dimensions=8)
    _critic(provider, ("c0", "accept"), ("c1", "reject"))
    await run_source(backend, FakeStore(), provider, make_policy())
    provider.reset_calls()
    second = await run_source(backend, FakeStore(), provider, make_policy())
    assert second.outcome is AgentOutcome.NO_ACTION
    assert [c for c in provider.calls if c.method == "complete_structured"] == []


@pytest.mark.parametrize("failing", ["get_analysis", "list_proposed_plans"])
async def test_read_tool_failure_after_retries_fails_the_run(failing: str) -> None:
    backend = FakeBackend.ready([make_plan(0)])
    backend.failures[failing] = 5
    store = FakeStore()
    result = await run_source(backend, store, None, make_policy(retries=1))
    assert result.status is AgentRunStatus.FAILED
    assert result.error_code is AgentErrorCode.TOOL_FAILED
    assert result.steps[-1].node is AgentNode.FINALIZE
    failed = next(s for s in result.steps if s.status is StepStatus.FAILED)
    assert [c.attempt for c in failed.tool_calls if not c.ok] == [1, 2]
    assert "postgres" not in failed.model_dump_json(), "error text never recorded"


async def test_handoff_failure_is_not_retried_and_fails_the_run() -> None:
    backend = FakeBackend.ready([make_plan(0)])
    backend.failures["hand_off"] = 1
    result = await run_source(backend, FakeStore(), None, make_policy(critic=False, retries=3))
    assert backend.calls.count("hand_off") == 1
    assert result.status is AgentRunStatus.FAILED
    assert result.error_code is AgentErrorCode.TOOL_FAILED


async def test_deterministic_critic_rule() -> None:
    plans = [make_plan(0, confidence=0.5), make_plan(1, confidence=0.49)]
    backend = FakeBackend.ready(plans)
    result = await run_source(backend, FakeStore(), None, make_policy(critic=False))
    reviews = {r.plan_id: r for r in backend.handoffs[0][1]}
    assert reviews[plans[0].id].verdict is CriticVerdict.ACCEPT, "at the threshold accepts"
    assert reviews[plans[1].id].verdict is CriticVerdict.UNCERTAIN
    assert all(r.fallback_reason is FallbackReason.MODEL_DISABLED for r in reviews.values())
    assert all(r.model is None for r in reviews.values())
    assert result.outcome is AgentOutcome.RECOMMEND
