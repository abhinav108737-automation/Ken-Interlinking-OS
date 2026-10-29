"""Support for the Phase 8 lifecycle, shadow, promotion and experiment tests (workstream D).

Everything is built through the REAL repositories and services where the behaviour under test
depends on them; raw SQL appears only for rows a test must forge to prove a refusal (an
evaluation of a chosen age, a proposal whose body breaks its contract).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.auth import Principal, Role
from ken_contracts.learning import (
    EvalSuite,
    EvaluationMetrics,
    EvidenceWindow,
    LearningPolicy,
    PolicyChange,
    ProposalEvidence,
    ProposalOption,
    ProposalStatus,
    SuiteMetrics,
    TargetKind,
)
from ken_core.db.models.learning import PolicyCandidate, PolicyEvaluation
from ken_core.db.repositories import learning as repo
from ken_services.learning.datasets import (
    available_suites,
    load_suite_datasets,
    manifest_shas,
)
from ken_services.learning.dedupe import canonical_sha256
from ken_services.learning.policy import load_learning_policy
from ken_services.learning.promote import record_stage
from ken_services.learning.proposals import insert_policy_proposal, prepare_policy_proposal
from ken_services.learning.reflect import _base_documents
from tests.conftest import make_settings
from tests.integration.review_support import principal

SM = async_sessionmaker[AsyncSession]
NOW: Final = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
SYSTEM: Final = "system:learning"
LEARNING_POLICY_PATH: Final = "policies/learning/learning.v1.yaml"
SHA: Final = "a" * 64


def learning_policy(**gates: Any) -> LearningPolicy:
    """The real policy, optionally with gate numbers overridden (for small test corpora)."""
    base = load_learning_policy(LEARNING_POLICY_PATH)
    if not gates:
        return base
    return base.model_copy(update={"gates": base.gates.model_copy(update=gates)})


def settings(**overrides: Any) -> Any:
    return make_settings(**overrides)


def evidence() -> ProposalEvidence:
    return ProposalEvidence(
        window=EvidenceWindow(start=date(2026, 7, 1), end=date(2026, 9, 28)),
        generated_at=NOW,
        aggregate_version="1",
        observations=(),
        options=(ProposalOption(index=0, label="baseline option", changes=()),),
        chosen_index=0,
        sample_count_total=0,
        policy_versions={"ranking": "1"},
        engine_version="engine-1",
        analysis_version="analysis-1",
        fallback_used=True,
    )


async def job_run(sm: SM, kind: str = "reflection") -> uuid.UUID:
    run_id = uuid.uuid4()
    async with sm() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO job_runs (id, kind, status, triggered_by) "  # noqa: S608
                f"VALUES (:id, '{kind}', 'running', 'test')"
            ),
            {"id": run_id},
        )
    return run_id


class Seeded:
    """A policy proposal and its candidate."""

    def __init__(self, proposal_id: uuid.UUID, candidate_id: uuid.UUID, fingerprint: str) -> None:
        self.proposal_id = proposal_id
        self.candidate_id = candidate_id
        self.fingerprint = fingerprint


def weight_changes() -> list[PolicyChange]:
    return [
        PolicyChange(path="ranking.weights.semantic", from_value=0.30, to_value=0.35),
        PolicyChange(path="ranking.weights.taxonomy", from_value=0.20, to_value=0.15),
    ]


async def seed_proposal(
    sm: SM,
    *,
    target: TargetKind = TargetKind.RANKING,
    changes: Sequence[PolicyChange] | None = None,
) -> Seeded:
    documents, cross = _base_documents(settings())
    prepared = prepare_policy_proposal(
        target_kind=target,
        base_document=documents[target],
        changes=list(changes or weight_changes()),
        cross_check=cross,
    )
    run_id = await job_run(sm)
    async with sm() as session, session.begin():
        proposal_id = await insert_policy_proposal(
            session,
            prepared=prepared,
            evidence=evidence(),
            title="Shift weight towards taxonomy",
            rationale="Reviewers edited anchors on semantic-only picks.",
            job_run_id=run_id,
            now=NOW,
        )
        assert proposal_id is not None
        candidate = await repo.candidate_for_proposal(session, proposal_id)
        assert candidate is not None
    return Seeded(proposal_id, candidate.id, prepared.fingerprint)


async def force_stage(sm: SM, proposal_id: uuid.UUID, *stages: ProposalStatus) -> None:
    """Walk a proposal through system stages, recording a real transition row each time."""
    for stage in stages:
        async with sm() as session, session.begin():
            proposal = await repo.lock_proposal(session, proposal_id)
            assert proposal is not None
            await record_stage(session, proposal=proposal, to_status=stage, actor=SYSTEM, now=NOW)


def real_suite() -> EvalSuite:
    suites = available_suites()
    assert suites, "no eval dataset exists under evals/datasets"
    return suites[0]


def metrics_json(*, sufficient: bool = True) -> dict[str, Any]:
    suite = SuiteMetrics(
        suite=real_suite(), cases=10, passed=10, failed=0, must_not_violations=0, pass_rate=1.0
    )
    metrics = EvaluationMetrics(
        cases_total=10, cases_passed=10, by_suite=(suite,), by_decision_class={}
    )
    dumped = metrics.model_dump(mode="json")
    if not sufficient:
        dumped["by_decision_class"] = {
            "same_market|target_topic|semantic": {
                "decision_class": "same_market|target_topic|semantic",
                "sample_count": 3,
                "sufficient": False,
            }
        }
    return dumped


async def insert_evaluation(
    sm: SM,
    *,
    proposal_id: uuid.UUID | None,
    fingerprint: str | None,
    gate: str = "pass",
    mode: str = "offline",
    manifests: dict[str, str] | None = None,
    age: timedelta = timedelta(hours=1),
    candidate_id: uuid.UUID | None = None,
    skill_id: uuid.UUID | None = None,
    metrics: dict[str, Any] | None = None,
    gate_reasons: list[dict[str, Any]] | None = None,
) -> uuid.UUID:
    suite = real_suite()
    current = manifest_shas(load_suite_datasets([suite]))
    run_id = await job_run(sm, kind="evaluation")
    evaluation_id = uuid.uuid4()
    started = NOW - age
    async with sm() as session, session.begin():
        await session.execute(
            insert(PolicyEvaluation).values(
                id=evaluation_id,
                job_run_id=run_id,
                mode=mode,
                proposal_id=proposal_id,
                candidate_id=candidate_id,
                skill_id=skill_id,
                proposal_fingerprint=fingerprint,
                suites=[suite.value],
                dataset_manifests=current if manifests is None else manifests,
                metrics=metrics or metrics_json(),
                gate_result=gate,
                gate_reasons=gate_reasons or [],
                runner_version="test-1",
                model_provider="mock",
                model_role_models={},
                seed=1,
                started_at=started,
                finished_at=started + timedelta(minutes=1),
            )
        )
    return evaluation_id


async def insert_shadow_rows(
    sm: SM, seeded: Seeded, *, runs: int, compared_pairs: int
) -> list[uuid.UUID]:
    """Shadow evaluations bound to the proposal's fingerprint (what ``shadow_pass`` writes)."""
    ids: list[uuid.UUID] = []
    for _ in range(runs):
        run_id = await job_run(sm, kind="recommendation")
        evaluation_id = uuid.uuid4()
        ids.append(evaluation_id)
        async with sm() as session, session.begin():
            await repo.insert_shadow_evaluation(
                session,
                evaluation_id=evaluation_id,
                job_run_id=run_id,
                proposal_id=seeded.proposal_id,
                candidate_id=seeded.candidate_id,
                proposal_fingerprint=seeded.fingerprint,
                suites=["policy-regression"],
                metrics={
                    **metrics_json(),
                    "shadow": {
                        "fingerprint": seeded.fingerprint,
                        "compared_pairs": compared_pairs,
                    },
                },
                gate_reasons=[],
                runner_version="shadow-1",
                started_at=NOW,
                finished_at=NOW,
            )
    return ids


async def add_identity_candidate(
    sm: SM, proposal_id: uuid.UUID, document: dict[str, Any]
) -> uuid.UUID:
    """A second candidate row holding exactly ``document`` (append-only table, new version)."""
    candidate_id = uuid.uuid4()
    async with sm() as session, session.begin():
        await session.execute(
            insert(PolicyCandidate).values(
                id=candidate_id,
                proposal_id=proposal_id,
                policy_kind=str(document["policy"]),
                base_version=str(document["version"]),
                candidate_version=f"test-{candidate_id.hex[:12]}",
                document=document,
                document_sha256=canonical_sha256(document),
                created_at=NOW,
            )
        )
    return candidate_id


async def operator(sm: SM, username: str = "otto") -> Principal:
    return await principal(sm, username, role=Role.OPERATOR_ADMIN)


async def reviewer(sm: SM, username: str = "rita") -> Principal:
    return await principal(sm, username, role=Role.REVIEWER)


async def count(sm: SM, table: str) -> int:
    async with sm() as session:
        return int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)
