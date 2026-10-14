"""In-memory fakes of the frozen agent protocols, plan factories and a run helper."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from ken_agents.runner import AgentRunResult, build_config_snapshot, run_one_source
from ken_contracts.agents import (
    MAX_REVIEW_NOTE_CHARS,
    MAX_REVIEW_NOTES,
    MAX_STEP_OUTPUT_BYTES,
    AgentErrorCode,
    AgentOutcome,
    AgentPolicy,
    AgentRunStatus,
    AgentStepView,
    AgentTrigger,
    CriticPolicy,
    CriticVerdict,
    ModelUsePolicy,
    TriageDecision,
)
from ken_contracts.intelligence import Intent, LinkEligibility, PageAnalysis
from ken_contracts.model import ModelProvider
from ken_contracts.recommendations import (
    ENGINE_VERSION,
    LinkPlan,
    LinkPlanStatus,
    PlacementLocator,
    PlacementPreview,
    RetrievalChannel,
    ScoreBreakdown,
    ScoreComponent,
)
from ken_services.agents.protocol import (
    HandOffResult,
    NewAgentRun,
    PageSnapshot,
    PlanCheck,
    PlanReviewInput,
)

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
SOURCE_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
SOURCE_URL = "https://site.test/reports/ev"
BLOCK = "Vehicle makers depend on imported lithium-ion batteries today."


class Clock:
    """Deterministic clock: each call advances one millisecond."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        self.now += timedelta(milliseconds=1)
        return self.now


def make_policy(
    *,
    planner: bool = False,
    critic: bool = True,
    model_timeout: float = 5.0,
    node_timeout: float = 5.0,
    max_plans: int = 3,
    threshold: float = 0.5,
    retries: int = 1,
) -> AgentPolicy:
    return AgentPolicy(
        version=1,
        planner=ModelUsePolicy(use_model=planner, timeout_seconds=model_timeout),
        critic=CriticPolicy(
            use_model=critic, timeout_seconds=model_timeout, accept_min_confidence=threshold
        ),
        max_plans_per_run=max_plans,
        node_timeout_seconds=node_timeout,
        tool_retries=retries,
    )


def make_page(page_id: uuid.UUID = SOURCE_ID, *, gone: bool = False) -> PageSnapshot:
    return PageSnapshot(
        id=page_id,
        canonical_url=SOURCE_URL,
        title="Electric Vehicle Market",
        current_version=2,
        publication_state="published",
        gone=gone,
        word_count=1200,
        internal_link_count=4,
    )


def make_analysis(page_id: uuid.UUID = SOURCE_ID) -> PageAnalysis:
    return PageAnalysis(
        page_id=page_id,
        page_version=2,
        analysis_version="tax1-alg1",
        page_type="report",
        intent=Intent.COMMERCIAL_RESEARCH,
        commercial_importance=2,
        industry_id="automotive",
        market_id="ev",
    )


def eligible(as_source: bool = True) -> LinkEligibility:
    return LinkEligibility(
        as_source=as_source,
        as_target=True,
        source_reasons=[] if as_source else ["protected"],
        target_reasons=[],
    )


def make_plan(
    i: int,
    *,
    confidence: float = 0.6,
    source_id: uuid.UUID = SOURCE_ID,
    title: str | None = None,
    preview_before: str | None = None,
) -> LinkPlan:
    start = BLOCK.index("lithium-ion batteries")
    end = start + len("lithium-ion batteries")
    return LinkPlan(
        id=uuid.UUID(int=0xA0000000_0000_4000_8000_000000000000 + i),
        run_id=None,
        source_page_id=source_id,
        source_page_version=2,
        source_url=SOURCE_URL,
        target_page_id=uuid.UUID(int=0xB0000000_0000_4000_8000_000000000000 + i),
        target_page_version=1,
        target_url=f"https://site.test/reports/target-{i}",
        target_title=title or f"Target Market {i}",
        relationship_type="supply_chain_upstream",
        channels=(RetrievalChannel.SEMANTIC,),
        anchor_text="lithium-ion batteries",
        anchor_source="target_topic",
        locator=PlacementLocator(
            block_id="b2",
            block_kind="p",
            occurrence_index=0,
            start=start,
            end=end,
            context_hash=sha256(BLOCK.encode()).hexdigest(),
        ),
        preview=PlacementPreview(
            before=preview_before if preview_before is not None else BLOCK[:start],
            anchor="lithium-ion batteries",
            after=BLOCK[end:],
        ),
        score=ScoreBreakdown(
            components=(
                ScoreComponent(
                    name="semantic", value=confidence, weight=1.0, contribution=confidence
                ),
            ),
            relevance=confidence,
            factors={},
            total=confidence,
            policy_name="ranking",
            policy_version=1,
        ),
        confidence=confidence,
        reason_codes=("semantically_similar",),
        rationale="Deterministic rationale.",
        engine_version=ENGINE_VERSION,
        policy_version=1,
        ranking_policy_version=1,
        analysis_version="tax1-alg1",
        status=LinkPlanStatus.PROPOSED,
        created_at=NOW,
        updated_at=NOW,
    )


@dataclass
class FakeBackend:
    """An in-memory AgentBackend with a plan lifecycle that mirrors the service rules."""

    pages: dict[uuid.UUID, PageSnapshot] = field(default_factory=dict)
    analyses: dict[uuid.UUID, PageAnalysis] = field(default_factory=dict)
    eligibility: dict[uuid.UUID, LinkEligibility] = field(default_factory=dict)
    plans: list[LinkPlan] = field(default_factory=list)
    status: dict[uuid.UUID, str] = field(default_factory=dict)
    reviewed: set[uuid.UUID] = field(default_factory=set)
    checks: dict[uuid.UUID, PlanCheck] = field(default_factory=dict)
    concurrent_expiry: set[uuid.UUID] = field(default_factory=set)
    failures: dict[str, int] = field(default_factory=dict)
    """method name -> how many next calls raise."""
    slow: dict[str, float] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    handoffs: list[tuple[uuid.UUID, tuple[PlanReviewInput, ...]]] = field(default_factory=list)

    @classmethod
    def ready(cls, plans: Sequence[LinkPlan] = ()) -> FakeBackend:
        backend = cls(
            pages={SOURCE_ID: make_page()},
            analyses={SOURCE_ID: make_analysis()},
            eligibility={SOURCE_ID: eligible()},
        )
        for p in plans:
            backend.add_plan(p)
        return backend

    def add_plan(self, plan: LinkPlan) -> None:
        self.plans.append(plan)
        self.status[plan.id] = "proposed"

    async def _enter(self, name: str) -> None:
        self.calls.append(name)
        if name in self.slow:
            await asyncio.sleep(self.slow[name])
        if self.failures.get(name, 0) > 0:
            self.failures[name] -= 1
            raise RuntimeError("secret-looking backend failure text postgres://u:p@h/db")

    async def get_page(self, *, page_id: uuid.UUID) -> PageSnapshot | None:
        await self._enter("get_page")
        return self.pages.get(page_id)

    async def get_analysis(self, *, page_id: uuid.UUID) -> PageAnalysis | None:
        await self._enter("get_analysis")
        return self.analyses.get(page_id)

    async def get_eligibility(self, *, page_id: uuid.UUID) -> LinkEligibility | None:
        await self._enter("get_eligibility")
        return self.eligibility.get(page_id)

    async def list_proposed_plans(self, *, source_page_id: uuid.UUID, limit: int) -> list[LinkPlan]:
        await self._enter("list_proposed_plans")
        open_ = [
            p
            for p in self.plans
            if p.source_page_id == source_page_id
            and self.status[p.id] == "proposed"
            and p.id not in self.reviewed
        ]
        open_.sort(key=lambda p: (-p.score.total, p.created_at, p.id))
        return open_[:limit]

    async def check_plan(self, *, plan_id: uuid.UUID) -> PlanCheck:
        await self._enter("check_plan")
        return self.checks.get(plan_id, PlanCheck(ok=True))

    async def hand_off(
        self,
        *,
        agent_run_id: uuid.UUID,
        reviews: Sequence[PlanReviewInput],
        reviewed_at: datetime,
    ) -> HandOffResult:
        await self._enter("hand_off")
        self.handoffs.append((agent_run_id, tuple(reviews)))
        handed: list[uuid.UUID] = []
        recorded: list[uuid.UUID] = []
        conflicts: dict[uuid.UUID, str] = {}
        for review in reviews:
            if review.plan_id in self.concurrent_expiry:
                self.status[review.plan_id] = "expired"
            if self.status[review.plan_id] != "proposed":
                conflicts[review.plan_id] = "not_proposed"
                continue
            recorded.append(review.plan_id)
            self.reviewed.add(review.plan_id)
            if review.verdict in (CriticVerdict.ACCEPT, CriticVerdict.UNCERTAIN):
                self.status[review.plan_id] = "awaiting_approval"
                handed.append(review.plan_id)
        return HandOffResult(
            handed_off=tuple(handed), recorded=tuple(recorded), conflicts=conflicts
        )


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(", ", ": ")).encode())


@dataclass
class FakeStore:
    """An in-memory AgentRunStore enforcing the store's documented rules."""

    sources: list[uuid.UUID] = field(default_factory=list)
    runs: dict[uuid.UUID, NewAgentRun] = field(default_factory=dict)
    running: set[uuid.UUID] = field(default_factory=set)
    steps: list[AgentStepView] = field(default_factory=list)
    finished: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)
    reaped: int = 0
    reap_calls: int = 0
    refuse_start: bool = False
    fail_finish: bool = False
    sources_limit: int | None = None

    async def sources_to_review(self, *, limit: int) -> list[uuid.UUID]:
        self.sources_limit = limit
        return self.sources[:limit]

    async def reap_orphans(self, *, now: datetime) -> int:
        self.reap_calls += 1
        return self.reaped

    async def start_run(self, *, run: NewAgentRun) -> None:
        if self.refuse_start or run.source_page_id in {
            r.source_page_id for r in self.runs.values() if r.id in self.running
        }:
            raise RuntimeError("already running")
        self.runs[run.id] = run
        self.running.add(run.id)

    async def append_step(self, *, step: AgentStepView) -> None:
        assert step.run_id in self.running, "steps only while running"
        assert (step.run_id, step.seq) not in {(s.run_id, s.seq) for s in self.steps}
        assert step.trace_id == self.runs[step.run_id].trace_id
        dumped = step.model_dump(mode="json")
        for name in ("output", "tool_calls"):
            assert _json_bytes(dumped[name]) <= MAX_STEP_OUTPUT_BYTES, name
        self.steps.append(step)

    async def finish_run(
        self,
        *,
        run_id: uuid.UUID,
        status: AgentRunStatus,
        outcome: AgentOutcome,
        finished_at: datetime,
        triage_decision: TriageDecision | None = None,
        source_page_version: int | None = None,
        review_notes: Sequence[str] = (),
        error_code: AgentErrorCode | None = None,
    ) -> None:
        assert run_id not in self.finished, "finish_run exactly once"
        if self.fail_finish:
            raise RuntimeError("finish failed")
        assert status is not AgentRunStatus.RUNNING
        assert (status is AgentRunStatus.FAILED) == (outcome is AgentOutcome.FAILED)
        assert status is not AgentRunStatus.FAILED or error_code is not None
        if outcome is AgentOutcome.RECOMMEND:
            assert status is AgentRunStatus.AWAITING_APPROVAL
        if outcome is AgentOutcome.NO_ACTION:
            assert status is AgentRunStatus.COMPLETED
        assert len(review_notes) <= MAX_REVIEW_NOTES
        assert all(len(n) <= MAX_REVIEW_NOTE_CHARS for n in review_notes)
        self.running.discard(run_id)
        self.finished[run_id] = {
            "status": status,
            "outcome": outcome,
            "finished_at": finished_at,
            "triage_decision": triage_decision,
            "source_page_version": source_page_version,
            "review_notes": list(review_notes),
            "error_code": error_code,
        }


def snapshot(policy: AgentPolicy) -> Mapping[str, object]:
    return build_config_snapshot(
        policy=policy,
        recommendation_policy_version=1,
        ranking_policy_version=1,
        provider_configured=True,
    )


async def run_source(
    backend: FakeBackend,
    store: FakeStore,
    provider: ModelProvider | None,
    policy: AgentPolicy,
    *,
    source: uuid.UUID = SOURCE_ID,
) -> AgentRunResult:
    result = await run_one_source(
        source_page_id=source,
        job_run_id=uuid.uuid4(),
        trigger=AgentTrigger.MANUAL,
        backend=backend,
        store=store,
        provider=provider,
        policy=policy,
        config_snapshot=snapshot(policy),
        clock=Clock(),
    )
    assert result is not None
    return result
