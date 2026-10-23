"""Phase 9 integration support: the autonomy engine and the autonomous publisher over the
Phase 6 :class:`~tests.integration.deployment_support.World`.

The plans are REAL engine plans, the CMS is the in-memory mock over the inventory's own pages
with the REAL deterministic HTML library on both sides, and the deployment service is the real
one. Nothing about the safety machinery is stubbed; what the tests vary is the autonomy MODE,
the stored facts and the CMS's behaviour.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

from ken_contracts.autonomy import AutonomyMode, AutonomyPolicy
from ken_contracts.deployment import (
    VerificationCheck,
    VerificationCheckName,
    VerificationOutcome,
)
from ken_contracts.intelligence import ProtectionSource
from ken_contracts.jobs import JobKind
from ken_contracts.learning import (
    DecisionClassMetrics,
    EvalSuite,
    EvaluationMetrics,
    EvaluationMode,
    GateResult,
    SkillSource,
    SkillStatus,
    SuiteMetrics,
)
from ken_contracts.model import ModelRole
from ken_contracts.recommendations import LinkPlanStatus
from ken_core.config import Settings
from ken_core.db.models.intelligence import JobRun, PageProtection
from ken_core.db.models.learning import PolicyEvaluation, Skill
from ken_services.autonomy.blocks import DeterministicBlocks
from ken_services.autonomy.budget import DatabaseRetryBudgetGate
from ken_services.autonomy.calibration import EvaluationCalibrationReader, decision_class_of
from ken_services.autonomy.engine import DatabaseAutonomyEngine
from ken_services.autonomy.facts import plan_identity
from ken_services.autonomy.policy import effective_retry, load_autonomy_policy
from ken_services.autonomy.publish import AutonomousPublisher
from ken_services.autonomy.risk import DeterministicRiskClassifier
from ken_services.deployment.mutation import HtmlMutator
from ken_services.deployment.service import DatabaseDeploymentService
from ken_services.deployment.verification import HtmlLinkVerifier
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.learning.policy import load_learning_policy
from ken_services.recommendations.policy import load_recommendation_policy
from ken_services.recommendations.transitions import transition
from tests.integration.deployment_support import World, world
from tests.integration.recommendation_support import SETTINGS, SM, rows

T0 = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
S = LinkPlanStatus


class Tick:
    """A strictly increasing clock whose current value a test can read and advance."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def _no_sleep(_seconds: float) -> None:
    return None


class FailingVerifier:
    """A :class:`~ken_services.deployment.protocol.LinkVerifier` that FAILS deterministically
    while leaving the CMS page untouched.

    Needed to test the SAFE automatic rollback. Every other way of making verification fail
    (an editor removing the link, a template adding a duplicate) also moves the CMS page's
    version, and the mock provider's ``rollback`` refuses to restore a snapshot of a page that
    has moved -- correctly, since that would destroy somebody's edit. So the only way to reach
    "verification failed AND rolling back is safe" is to fault the VERIFIER, which is the same
    kind of instrumentation as ``FaultyCMS`` around the CMS call: the deterministic mutation
    itself is never replaced.
    """

    def __init__(self, *, detail: str = "link_missing") -> None:
        self.detail = detail
        self.calls = 0

    def verify_links(
        self, html: str, insertions: Any, *, target_reachable: Any = None
    ) -> list[VerificationCheck]:
        del html, target_reachable
        self.calls += 1
        return [
            VerificationCheck(
                plan_id=item.plan_id,
                check=name,
                outcome=VerificationOutcome.FAILED,
                detail=self.detail,
            )
            for item in insertions
            for name in (
                VerificationCheckName.LINK_PRESENT_ONCE,
                VerificationCheckName.HREF_MATCHES,
            )
        ]


def autonomy_settings(**overrides: Any) -> Settings:
    """``SETTINGS`` with the autonomy knobs a test wants. The validators are NOT bypassed:
    ``autonomous`` still needs the acknowledgement, which the tests pass explicitly."""
    base = {"deploy_verify_retry_delay_s": 0.0, "autonomy_strict_verify_delay_s": 0.0}
    built: Settings = SETTINGS.model_copy(update={**base, **overrides})
    return built


@dataclass
class Autonomy:
    """The autonomy stack over one :class:`World`.

    ``policy`` is the RELEASED autonomy policy by default. :meth:`with_policy` swaps in a copy
    with chosen values for a test that exercises a path the shipped thresholds make
    unreachable on the demo corpus (``guarded`` publishing anything), via ``model_copy`` --
    which skips validation ON PURPOSE, because the loader would (rightly) refuse a floor below
    the contract's. That is test scaffolding only; the loader's refusal is tested separately in
    ``tests/unit/autonomy/test_policy.py``.
    """

    world: World
    settings: Settings
    clock: Tick
    policy: AutonomyPolicy = field(default_factory=load_autonomy_policy)

    @property
    def sessionmaker(self) -> SM:
        return self.world.sessionmaker

    def blocks(self) -> DeterministicBlocks:
        return DeterministicBlocks(
            sessionmaker=self.sessionmaker,
            settings=self.settings,
            policy=load_recommendation_policy(self.settings.recommendation_policy_path),
            taxonomy=load_taxonomy(self.settings.taxonomy_path),
            autonomy_policy=self.policy,
            clock=self.clock,
        )

    def engine(self) -> DatabaseAutonomyEngine:
        taxonomy = load_taxonomy(self.settings.taxonomy_path)
        return DatabaseAutonomyEngine(
            sessionmaker=self.sessionmaker,
            settings=self.settings,
            policy=load_recommendation_policy(self.settings.recommendation_policy_path),
            taxonomy=taxonomy,
            autonomy_policy=self.policy,
            blocks=self.blocks(),
            risk=DeterministicRiskClassifier(
                sessionmaker=self.sessionmaker,
                taxonomy=taxonomy,
                clock=self.clock,
                policy=self.policy.risk,
            ),
            calibration=EvaluationCalibrationReader(
                sessionmaker=self.sessionmaker,
                policy=load_learning_policy(self.settings.learning_policy_path),
                clock=self.clock,
                min_positive_outcome_rate=self.policy.confidence.min_positive_outcome_rate,
            ),
            clock=self.clock,
        )

    def deployments(self, *, probe: Any = None, verifier: Any = None) -> DatabaseDeploymentService:
        return DatabaseDeploymentService(
            sessionmaker=self.sessionmaker,
            settings=self.settings,
            cms=self.world.cms,
            mutator=HtmlMutator(),
            verifier=verifier or HtmlLinkVerifier(),
            probe=probe,
            clock=self.clock,
            sleep=_no_sleep,
        )

    def budget(self) -> DatabaseRetryBudgetGate:
        return DatabaseRetryBudgetGate(
            sessionmaker=self.sessionmaker,
            retry=effective_retry(self.policy, self.settings),
            clock=self.clock,
        )

    def publisher(
        self,
        *,
        engine: DatabaseAutonomyEngine | None = None,
        deployments: DatabaseDeploymentService | None = None,
        probe: Any = None,
        verifier: Any = None,
    ) -> AutonomousPublisher:
        return AutonomousPublisher(
            sessionmaker=self.sessionmaker,
            settings=self.settings,
            engine=engine or self.engine(),
            deployments=deployments or self.deployments(probe=probe, verifier=verifier),
            budget=self.budget(),
            principal=self.world.operator,
        )

    def with_mode(self, mode: str, **overrides: Any) -> Autonomy:
        return Autonomy(
            world=self.world,
            settings=autonomy_settings(autonomy_mode=mode, **overrides),
            clock=self.clock,
            policy=self.policy,
        )

    def with_policy(
        self, *, confidence: dict[str, float] | None = None, retry: dict[str, Any] | None = None
    ) -> Autonomy:
        """The same stack under a policy copy with the named values (NOT validated: see the
        class docstring)."""
        policy = self.policy.model_copy(
            update={
                "confidence": self.policy.confidence.model_copy(update=confidence or {}),
                "retry": self.policy.retry.model_copy(update=retry or {}),
            }
        )
        return Autonomy(world=self.world, settings=self.settings, clock=self.clock, policy=policy)


async def autonomy(sessionmaker: SM, directory: Path, **overrides: Any) -> Autonomy:
    built = await world(sessionmaker, directory)
    return Autonomy(world=built, settings=autonomy_settings(**overrides), clock=Tick())


async def to_policy_checked(sessionmaker: SM, plan_id: uuid.UUID) -> None:
    """Advance a proposed plan to ``policy_checked`` as the agent ladder would."""
    async with sessionmaker() as session, session.begin():
        await transition(
            session,
            plan_id=plan_id,
            to_status=S.POLICY_CHECKED,
            actor="agent:policy-gate@graph-v1",
            expected_from=S.PROPOSED,
        )


async def record_critic_review(
    sessionmaker: SM,
    plan_id: uuid.UUID,
    *,
    source_page_id: uuid.UUID,
    source_url: str,
    verdict: str,
    handed_off: bool,
    at: datetime = T0,
) -> None:
    """A STORED critic review (``agent_runs`` + ``agent_run_plans``) with ``handed_off`` under
    the test's control. The shared ``record_agent_review`` helper hard-codes ``handed_off``
    true, which the schema forbids for a ``reject`` (``reject_not_handed_off``) and which a
    fresh ``uncertain`` review is not."""
    run_id = uuid.uuid4()
    async with sessionmaker() as session, session.begin():
        job_run_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO job_runs (id, kind, status, triggered_by, started_at, "
                "completed_at, counters, config_snapshot) VALUES (:id, 'agent', "
                "'succeeded', 'test', :at, :at, '{}'::jsonb, '{}'::jsonb)"
            ),
            {"id": job_run_id, "at": at},
        )
        await session.execute(
            text(
                "INSERT INTO agent_runs (id, job_run_id, source_page_id, source_page_version, "
                "source_url, trigger, status, outcome, correlation_id, trace_id, "
                "graph_version, config_snapshot, started_at, finished_at, created_at, "
                "updated_at) "
                "VALUES (:id, :job, :source, 1, :url, 'batch', 'awaiting_approval', "
                "'recommend', :id, :trace, 'graph-v1', '{}'::jsonb, :at, :at, :at, :at)"
            ),
            {
                "id": run_id,
                "job": job_run_id,
                "source": source_page_id,
                "url": source_url,
                "trace": run_id.hex,
                "at": at,
            },
        )
        await session.execute(
            text(
                "INSERT INTO agent_run_plans (run_id, plan_id, verdict, reasons, explanation, "
                "handed_off, fallback_used, model, reviewed_at) VALUES (:run, :plan, :verdict, "
                "'[]'::jsonb, '', :handed, false, 'qwen3:8b', :at)"
            ),
            {"run": run_id, "plan": plan_id, "verdict": verdict, "handed": handed_off, "at": at},
        )


async def decisions(sessionmaker: SM, plan_id: uuid.UUID | None = None) -> list[Any]:
    where = "WHERE plan_id = :p " if plan_id is not None else ""
    return await rows(
        sessionmaker,
        f"SELECT * FROM autonomy_decisions {where}ORDER BY id",  # noqa: S608 - fixed fragment
        **({"p": plan_id} if plan_id is not None else {}),
    )


async def attempts(sessionmaker: SM, plan_id: uuid.UUID | None = None) -> list[Any]:
    where = "WHERE plan_id = :p " if plan_id is not None else ""
    return await rows(
        sessionmaker,
        f"SELECT * FROM publish_attempts {where}ORDER BY id",  # noqa: S608 - fixed fragment
        **({"p": plan_id} if plan_id is not None else {}),
    )


async def transitions(sessionmaker: SM, plan_id: uuid.UUID) -> list[str]:
    found = await rows(
        sessionmaker,
        "SELECT to_status FROM link_plan_transitions WHERE plan_id = :p ORDER BY id",
        p=plan_id,
    )
    return [r.to_status for r in found]


async def protect(sessionmaker: SM, page_id: uuid.UUID) -> None:
    """An ACTIVE protection on a page: the PROTECTED_PAGE hard block's fact."""
    async with sessionmaker() as session, session.begin():
        session.add(
            PageProtection(
                page_id=page_id,
                source=ProtectionSource.MANUAL.value,
                rule_id=None,
                reason="a test protected this page",
                active=True,
                created_by="user:otto",
                created_at=datetime.now(UTC),
            )
        )


async def seed_calibration(
    sessionmaker: SM,
    *,
    plan_id: uuid.UUID,
    calibrated_confidence: float = 0.95,
    sample_count: int = 500,
    rollback_rate: float | None = None,
    positive_outcome_rate: float | None = None,
    gate: GateResult = GateResult.PASS,
    finished_at: datetime | None = None,
) -> str:
    """A PASSING ``policy_evaluations`` row carrying a calibration for the plan's class.

    This is the Phase 8 record Phase 9 reads -- not a new store. Without one, the plan's class
    is UNCALIBRATED, which is exactly what the degradation tests assert.
    """
    async with sessionmaker() as session:
        identity = await plan_identity(session, plan_id=plan_id)
        await session.rollback()
    decision_class = decision_class_of(identity)
    assert decision_class is not None, identity
    metrics = EvaluationMetrics(
        cases_total=1,
        cases_passed=1,
        by_suite=(
            SuiteMetrics(
                suite=EvalSuite.PROTECTED_CONTENT,
                cases=1,
                passed=1,
                failed=0,
                must_not_violations=0,
                pass_rate=1.0,
            ),
        ),
        by_decision_class={
            decision_class.canonical: DecisionClassMetrics(
                decision_class=decision_class.canonical,
                sample_count=sample_count,
                calibrated_confidence=calibrated_confidence,
                rollback_rate=rollback_rate,
                positive_outcome_rate=positive_outcome_rate,
                sufficient=True,
            )
        },
    )
    when = finished_at or datetime.now(UTC)
    async with sessionmaker() as session, session.begin():
        run = JobRun(
            id=uuid.uuid4(),
            kind=JobKind.EVALUATION.value,
            status="succeeded",
            triggered_by="test",
            config_snapshot={},
            counters={},
            started_at=when,
            completed_at=when,
        )
        skill_id = uuid.uuid4()
        skill = Skill(
            id=skill_id,
            name=f"calibration_fixture_{skill_id.hex[:12]}",
            version=1,
            description="a calibration fixture",
            instructions="none",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            suites=[EvalSuite.PROTECTED_CONTENT.value],
            model_role=ModelRole.REASONING.value,
            source=SkillSource.HUMAN.value,
            status=SkillStatus.DRAFT.value,
            created_at=when,
        )
        session.add_all([run, skill])
        await session.flush()
        session.add(
            PolicyEvaluation(
                id=uuid.uuid4(),
                job_run_id=run.id,
                mode=EvaluationMode.OFFLINE.value,
                skill_id=skill_id,
                suites=[EvalSuite.PROTECTED_CONTENT.value],
                dataset_manifests={},
                metrics=metrics.model_dump(mode="json"),
                gate_result=GateResult(gate).value,
                gate_reasons=[],
                runner_version="eval-1",
                model_provider="mock",
                model_role_models={},
                started_at=when,
                finished_at=when,
            )
        )
    return decision_class.canonical


__all__ = [
    "Autonomy",
    "AutonomyMode",
    "FailingVerifier",
    "Tick",
    "attempts",
    "autonomy",
    "autonomy_settings",
    "decisions",
    "protect",
    "record_critic_review",
    "seed_calibration",
    "to_policy_checked",
    "transitions",
]
