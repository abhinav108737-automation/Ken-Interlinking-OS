"""Clearly labelled SYNTHETIC fixtures that stand in for history a fresh install does not have.

Two, and only two:

* a **calibration fixture** (``runner_version = 'demo-synthetic-calibration'``): a passing,
  fresh, sufficiently sampled ``policy_evaluations`` row for one decision class, bound to a
  system-authored proposal. It is what lets the demo show plain ``AUTO_PUBLISH``;
* a **review history**: 34 reviewed plans of one decision class dated June 2026, so reflection
  has cleared the 30-sample floor and produces a proposal.

Both are written through the tables the real pipeline writes, never around a guard, and both
say "SYNTHETIC" in every field a human can read. They are evidence about nothing.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, date, datetime
from typing import Any, Final

from sqlalchemy import insert, text

from ken_contracts.auth import Role
from ken_contracts.intelligence import RelationshipType
from ken_contracts.jobs import JobKind
from ken_contracts.learning import (
    LEARNING_ACTOR,
    DecisionClassMetrics,
    EvalSuite,
    EvaluationMetrics,
    EvidenceWindow,
    ProposalEvidence,
    ProposalOption,
    SuiteMetrics,
    TargetKind,
)
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
from ken_core.db.models.learning import PolicyEvaluation
from ken_core.db.repositories import auth as auth_repo
from ken_core.db.repositories import learning as learning_repo
from ken_core.db.repositories import recommendations as rec_repo
from ken_services.auth.passwords import hash_password
from ken_services.demo.context import Ctx, fetchall, need
from ken_services.inventory.hashing import sha256_hex
from ken_services.learning.policy import load_configured_learning_policy
from ken_services.learning.proposals import (
    insert_policy_proposal,
    policy_change,
    prepare_policy_proposal,
)
from ken_services.learning.reflect import _base_documents

__all__ = [
    "CALIBRATION_RUNNER",
    "REVIEW_WINDOW",
    "SYNTHETIC_CLASS",
    "seed_calibration_fixture",
    "seed_review_history",
]

CALIBRATION_RUNNER: Final = "demo-synthetic-calibration"
REVIEW_WINDOW: Final = EvidenceWindow(start=date(2026, 6, 1), end=date(2026, 6, 30))
REVIEW_AT: Final = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
SYNTHETIC_CLASS: Final = "same_market|target_topic|semantic"
SYNTHETIC_PLANS: Final = 34
LABEL: Final = "SYNTHETIC - demo fixture, not evidence about any real site"


async def _job_run(ctx: Ctx, kind: JobKind) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with ctx.sessionmaker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO job_runs (id, kind, status, triggered_by, started_at, completed_at, "
                "counters, config_snapshot) VALUES (:id, :kind, 'succeeded', 'demo-synthetic', "
                "now(), now(), '{}'::jsonb, CAST(:snap AS jsonb))"
            ),
            {"id": run_id, "kind": kind.value, "snap": json.dumps({"synthetic": True})},
        )
    return run_id


def _evidence(now: datetime) -> ProposalEvidence:
    return ProposalEvidence(
        window=REVIEW_WINDOW,
        generated_at=now,
        aggregate_version="1",
        observations=(),
        options=(ProposalOption(index=0, label="SYNTHETIC calibration anchor", changes=()),),
        chosen_index=0,
        sample_count_total=0,
        policy_versions={"ranking": "1"},
        engine_version="demo-synthetic",
        analysis_version="demo-synthetic",
        fallback_used=True,
    )


async def seed_calibration_fixture(ctx: Ctx, *, decision_class: str) -> dict[str, Any]:
    """Seed the passing evaluation that calibrates ``decision_class`` (module docstring)."""
    now = datetime.now(UTC)
    documents, cross = _base_documents(ctx.settings)
    policy = load_configured_learning_policy(ctx.settings)
    ranking = documents[TargetKind.RANKING]
    prepared = prepare_policy_proposal(
        target_kind=TargetKind.RANKING,
        base_document=ranking,
        changes=[
            policy_change(
                path="ranking.weights.historical",
                base_document=ranking,
                to_value=round(float(ranking["weights"]["historical"]) + 0.01, 6),
                target_kind=TargetKind.RANKING,
                policy=policy,
            ),
            policy_change(
                path="ranking.weights.geography",
                base_document=ranking,
                to_value=round(float(ranking["weights"]["geography"]) - 0.01, 6),
                target_kind=TargetKind.RANKING,
                policy=policy,
            ),
        ],
        cross_check=cross,
    )
    reflection_run = await _job_run(ctx, JobKind.REFLECTION)
    async with ctx.sessionmaker() as session, session.begin():
        proposal_id = await insert_policy_proposal(
            session,
            prepared=prepared,
            evidence=_evidence(now),
            title="SYNTHETIC CALIBRATION FIXTURE (demo): anchor for a calibration row",
            rationale=f"{LABEL}. It exists because policy_evaluations needs exactly one subject.",
            job_run_id=reflection_run,
            now=now,
        )
        proposal_id = need(proposal_id)
        candidate = await learning_repo.candidate_for_proposal(session, proposal_id)
        candidate = need(candidate)
        candidate_id = candidate.id
    evaluation_run = await _job_run(ctx, JobKind.EVALUATION)
    samples, calibrated, positive = 40, 0.95, 0.60
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
            decision_class: DecisionClassMetrics(
                decision_class=decision_class,
                sample_count=samples,
                calibrated_confidence=calibrated,
                rollback_rate=0.0,
                positive_outcome_rate=positive,
                sufficient=True,
            )
        },
    )
    evaluation_id = uuid.uuid4()
    async with ctx.sessionmaker() as session, session.begin():
        await session.execute(
            insert(PolicyEvaluation).values(
                id=evaluation_id,
                job_run_id=evaluation_run,
                mode="offline",
                proposal_id=proposal_id,
                candidate_id=candidate_id,
                skill_id=None,
                proposal_fingerprint=prepared.fingerprint,
                suites=[EvalSuite.PROTECTED_CONTENT.value],
                dataset_manifests={},
                metrics=metrics.model_dump(mode="json"),
                gate_result="pass",
                gate_reasons=[],
                runner_version=CALIBRATION_RUNNER,
                model_provider="mock",
                model_role_models={},
                seed=0,
                started_at=now,
                finished_at=now,
            )
        )
    return {
        "evaluation_id": evaluation_id,
        "proposal_id": proposal_id,
        "proposed_by": LEARNING_ACTOR,
        "samples": samples,
        "calibrated": calibrated,
        "positive": positive,
        "runner_version": CALIBRATION_RUNNER,
    }


def _plan(
    source: uuid.UUID,
    target: uuid.UUID,
    source_url: str,
    target_url: str,
    status: LinkPlanStatus,
    n: int,
) -> LinkPlan:
    block = f"SYNTHETIC history {n}: electric vehicles are mentioned here."
    anchor = "electric vehicles"
    start = block.index(anchor)
    component = ScoreComponent(name="semantic", value=0.5, weight=1.0, contribution=0.5)
    return LinkPlan(
        id=uuid.uuid4(),
        run_id=None,
        source_page_id=source,
        source_page_version=1,
        source_url=source_url,
        target_page_id=target,
        target_page_version=1,
        target_url=target_url,
        target_title="SYNTHETIC",
        relationship_type=RelationshipType.SAME_MARKET,
        channels=(RetrievalChannel.SEMANTIC,),
        anchor_text=anchor,
        anchor_source="target_topic",
        locator=PlacementLocator(
            block_id=f"synthetic-{n}",
            block_kind="p",
            occurrence_index=0,
            start=start,
            end=start + len(anchor),
            context_hash=sha256_hex(block),
        ),
        preview=PlacementPreview(
            before=block[:start], anchor=anchor, after=block[start + len(anchor) :]
        ),
        score=ScoreBreakdown(
            components=(component,),
            relevance=0.5,
            factors={},
            total=0.5,
            policy_name="ranking",
            policy_version=1,
        ),
        confidence=0.5,
        reason_codes=(),
        rationale=LABEL,
        engine_version=ENGINE_VERSION,
        policy_version=1,
        ranking_policy_version=1,
        analysis_version="demo-synthetic",
        status=status,
        created_at=REVIEW_AT,
        updated_at=REVIEW_AT,
    )


async def seed_review_history(ctx: Ctx) -> dict[str, int]:
    """34 reviewed plans of one decision class, dated June 2026, with destination-heavy
    rejections (every other plan). Terminal plan statuses only, so nothing occupies a pair."""
    pages = await fetchall(
        ctx,
        "SELECT id, canonical_url FROM pages WHERE canonical_url LIKE '%/industry-reports/%' "
        "ORDER BY canonical_url LIMIT 2",
    )
    source, target = pages[0], pages[1]
    reviewer_id = uuid.uuid4()
    secret = secrets.token_urlsafe(48)
    async with ctx.sessionmaker() as session, session.begin():
        await auth_repo.insert_user(
            session,
            user_id=reviewer_id,
            username="demo-synthetic-reviewer",
            display_name="SYNTHETIC reviewer (no login)",
            role=Role.REVIEWER,
            password_hash=hash_password(secret),
            now=datetime.now(UTC),
        )
    del secret
    rejected = approved = 0
    for n in range(SYNTHETIC_PLANS):
        reject = n % 2 == 1
        plan = _plan(
            source.id,
            target.id,
            source.canonical_url,
            target.canonical_url,
            LinkPlanStatus.REJECTED if reject else LinkPlanStatus.VERIFIED,
            n,
        )
        async with ctx.sessionmaker() as session, session.begin():
            await rec_repo.insert_plan(session, plan=plan)
            await rec_repo.insert_transition(
                session,
                plan_id=plan.id,
                from_status=None,
                to_status=plan.status,
                actor="system:demo-synthetic",
                note=LABEL,
                occurred_at=REVIEW_AT,
            )
            await session.execute(
                text(
                    "INSERT INTO review_actions (operation_id, request_fingerprint, action, actor, "
                    "user_id, plan_id, new_plan_id, new_plan_status, from_status, to_status, "
                    "reasons, note, anchor_before, anchor_after, target_before, target_after, "
                    "result, created_at) VALUES (:op, :fp, :action, "
                    "'user:demo-synthetic-reviewer', "
                    ":user, :plan, NULL, NULL, 'awaiting_approval', :to_status, "
                    "CAST(:reasons AS jsonb), '', 'electric vehicles', NULL, :target, NULL, "
                    "'{}'::jsonb, :at)"
                ),
                {
                    "op": uuid.uuid4(),
                    "fp": sha256_hex(f"synthetic:{plan.id}"),
                    "action": "reject" if reject else "approve",
                    "user": reviewer_id,
                    "plan": plan.id,
                    "to_status": "rejected" if reject else "approved",
                    "reasons": json.dumps(
                        ["irrelevant_destination", "better_destination_exists"] if reject else []
                    ),
                    "target": target.id,
                    "at": REVIEW_AT,
                },
            )
        rejected += int(reject)
        approved += int(not reject)
    return {"plans": SYNTHETIC_PLANS, "rejected": rejected, "approved": approved}
