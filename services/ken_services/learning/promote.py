"""Promotion: the ONE path that records a stage change by a human (ADR-0016 15.7, 16.1, 17).

:class:`DatabasePromotionService.advance` is the only code that moves a proposal into a human
stage. It takes a :class:`~ken_contracts.auth.Principal` and NEVER an actor string: the actor is
``principal.actor``, derived from the authenticated session.

**With ``Settings.learning_demonstration_mode`` at its default (``True``) every promotion is
refused** with ``PromotionRefusal.DEMONSTRATION_MODE``, before any other check and before any
database access. That is the expected Phase 8 behaviour, not a defect: the corpus is synthetic
and ADR-0016 16.1 makes "Phase 8 promotes nothing" a mechanism rather than a promise.

For a promotion with demonstration mode off the service checks, IN THIS ORDER (protocol.py):

1. the gate policy's ``required_stages`` for the proposal's kind are all in its transition
   history (and, when ``shadow`` is required, the ``shadow_min_runs`` / ``shadow_min_plans``
   floors are met);
2. the evaluation run exists, belongs to this proposal (or its skill), is an ``offline`` run
   with ``gate_result = pass``, was run against the proposal's CURRENT fingerprint, its dataset
   manifests are unchanged on disk, it is newer than ``promotion_max_eval_age_days``, no
   decision class in it is below the sample floor and no behavioural block is recorded.

Then, in ONE transaction: ``set_config('ken.learning_promoter', 'on', true)``, the transition
INSERT, the proposal UPDATE (and, for a skill, the registry row), and ``policy.promoted`` /
``skill.promoted`` carrying ``promoted_by`` and ``evaluation_ref``. The setting is
transaction-local, so it cannot leak to the next transaction on a pooled connection.

**No policy file is written here.** :func:`write_promoted_policy` (``make policy-promote``) is
the only file write of the phase; it refuses unless a human ``promoted`` transition with an
evaluation reference already exists, and it never creates one.

**What a replay is.** ``operation_id`` is validated but not persisted (migration 0009 has no
column for it). A request that repeats the proposal's latest transition exactly is reported as
``replayed``; any other request for a stage the proposal is already in is refused with
``REPLAYED_WITH_DIFFERENT_BODY``.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.auth import Permission, Principal, has_permission
from ken_contracts.events import EventEnvelope, EventName, PromotionAuditPayload
from ken_contracts.learning import (
    LEARNING_EMITTED_BY,
    LEARNING_PROMOTER_SETTING,
    PROMOTER_ACTOR_PATTERN,
    PROPOSAL_TRANSITIONS,
    AdvanceProposalRequest,
    AdvanceProposalResponse,
    EvaluationMetrics,
    EvaluationMode,
    GateReasonCode,
    GateResult,
    LearningError,
    LearningPolicy,
    PolicyProposalBody,
    PromotionRefusal,
    PromotionRefusedError,
    ProposalKind,
    ProposalNotFoundError,
    ProposalRejectionReason,
    ProposalStatus,
    SkillStatus,
    TargetKind,
    may_advance_proposal,
)
from ken_contracts.recommendations import RankingPolicy, RecommendationPolicy, actor_class
from ken_core.config import REPO_ROOT, Settings
from ken_core.db.models.learning import (
    LearningProposal,
    LearningProposalTransition,
    PolicyEvaluation,
)
from ken_core.db.repositories import learning as repo
from ken_core.db.repositories.events import append_event
from ken_core.logging import get_logger
from ken_services.jobs.runner import utc_now
from ken_services.learning.datasets import DatasetError, load_suite_datasets, manifest_shas
from ken_services.learning.dedupe import canonical_sha256
from ken_services.learning.lifecycle import (
    SYSTEM_STAGES,
    check_stage_gate,
    policy_key,
    shadow_requirements_met,
    stages_reached,
)
from ken_services.learning.policy import load_configured_learning_policy
from ken_services.learning.protocol import required_stages_met
from ken_services.recommendations.policy import load_recommendation_policy

__all__ = [
    "DatabasePromotionService",
    "PolicyWriteRefusedError",
    "PromotedFiles",
    "plan_promoted_files",
    "record_stage",
    "write_promoted_policy",
]

logger = get_logger(__name__)

_PROMOTER_ACTOR: Final = re.compile(PROMOTER_ACTOR_PATTERN)
_HUMAN_CLASS: Final = "user"


def _refuse(refusal: PromotionRefusal) -> PromotionRefusedError:
    return PromotionRefusedError(refusal)


class DatabasePromotionService:
    """:class:`ken_services.learning.protocol.PromotionService` over PostgreSQL."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        policy: LearningPolicy | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._policy = policy
        self._clock = clock

    def _learning_policy(self) -> LearningPolicy:
        return self._policy or load_configured_learning_policy(self._settings)

    async def advance(
        self, *, principal: Principal, proposal_id: uuid.UUID, request: AdvanceProposalRequest
    ) -> AdvanceProposalResponse:
        target = request.to_status
        # 1. Demonstration mode is checked FIRST, before the database is touched (16.1).
        if target is ProposalStatus.PROMOTED and self._settings.learning_demonstration_mode:
            raise _refuse(PromotionRefusal.DEMONSTRATION_MODE)
        if not has_permission(principal.role, Permission.PROMOTE):
            raise _refuse(PromotionRefusal.PERMISSION_DENIED)
        actor = principal.actor
        if actor_class(actor) != _HUMAN_CLASS or _PROMOTER_ACTOR.fullmatch(actor) is None:
            raise _refuse(PromotionRefusal.NOT_A_HUMAN)
        policy = self._learning_policy()
        now = self._clock()

        from ken_services.learning.reader import build_proposal_detail, transition_view

        async with self._sessionmaker() as session, session.begin():
            proposal = await repo.lock_proposal(session, proposal_id)
            if proposal is None:
                raise ProposalNotFoundError
            history = await repo.proposal_transitions(session, proposal.id)
            current = ProposalStatus(proposal.status)
            if current is target:
                last = history[-1] if history else None
                same = (
                    last is not None
                    and last.to_status == target.value
                    and last.actor == actor
                    and last.evaluation_run_id == request.evaluation_run_id
                    and last.reason == (None if request.reason is None else request.reason.value)
                    and (last.note or None) == (request.note or None)
                )
                if not same or last is None:
                    raise _refuse(PromotionRefusal.REPLAYED_WITH_DIFFERENT_BODY)
                detail = await build_proposal_detail(
                    session, settings=self._settings, policy=policy, proposal_id=proposal.id
                )
                return AdvanceProposalResponse(
                    proposal=detail, transition=transition_view(last), replayed=True
                )
            if target not in PROPOSAL_TRANSITIONS[current]:
                raise _refuse(PromotionRefusal.TRANSITION_NOT_ALLOWED)
            if target in SYSTEM_STAGES:
                await check_stage_gate(session, proposal, target)
            promoted = target is ProposalStatus.PROMOTED
            if promoted:
                await self._check_promotion(
                    session,
                    proposal=proposal,
                    history=history,
                    policy=policy,
                    request=request,
                    now=now,
                )
                await session.execute(
                    text("SELECT set_config(:name, 'on', true)"),
                    {"name": LEARNING_PROMOTER_SETTING},
                )
            row = await record_stage(
                session,
                proposal=proposal,
                to_status=target,
                actor=actor,
                now=now,
                reason=request.reason,
                note=request.note,
                evaluation_run_id=request.evaluation_run_id,
                promoter_established=promoted,
            )
            if promoted:
                await self._emit_promoted(
                    session,
                    proposal=proposal,
                    actor=actor,
                    now=now,
                    evaluation_run_id=request.evaluation_run_id,
                )
            detail = await build_proposal_detail(
                session, settings=self._settings, policy=policy, proposal_id=proposal.id
            )
            view = transition_view(row)
        logger.info(
            "learning_proposal_advanced",
            proposal_id=str(proposal_id),
            to_status=target.value,
            actor_class=_HUMAN_CLASS,
        )
        return AdvanceProposalResponse(proposal=detail, transition=view, replayed=False)

    # ------------------------------------------------------------------ gates
    async def _check_promotion(
        self,
        session: AsyncSession,
        *,
        proposal: LearningProposal,
        history: list[Any],
        policy: LearningPolicy,
        request: AdvanceProposalRequest,
        now: datetime,
    ) -> None:
        key = policy_key(proposal)
        reached = stages_reached(history)
        if not required_stages_met(policy=policy, key=key, reached=reached):
            raise _refuse(PromotionRefusal.REQUIRED_STAGE_MISSING)
        skill = await repo.skill_for_proposal(session, proposal.id)
        if ProposalStatus.SHADOW in policy.gates.required_stages.get(key, ()):
            shadows = await repo.evaluations_for_proposal(
                session, proposal.id, mode=EvaluationMode.SHADOW
            )
            shadow_objects = [
                dict(run.metrics.get("shadow") or {}) for run in shadows if run.metrics
            ]
            if not shadow_requirements_met(
                policy=policy, fingerprint=proposal.fingerprint, shadow_metrics=shadow_objects
            ):
                raise _refuse(PromotionRefusal.REQUIRED_STAGE_MISSING)
        if request.evaluation_run_id is None:
            raise _refuse(PromotionRefusal.EVALUATION_MISSING)
        evaluation = await repo.evaluation_by_id(session, request.evaluation_run_id)
        if evaluation is None:
            raise _refuse(PromotionRefusal.EVALUATION_NOT_FOUND)
        belongs = evaluation.proposal_id == proposal.id or (
            skill is not None and evaluation.skill_id == skill.id
        )
        if not belongs:
            raise _refuse(PromotionRefusal.EVALUATION_FOREIGN)
        if (
            evaluation.mode != EvaluationMode.OFFLINE.value
            or evaluation.gate_result != GateResult.PASS.value
        ):
            raise _refuse(PromotionRefusal.EVALUATION_NOT_PASSED)
        if evaluation.proposal_fingerprint != proposal.fingerprint:
            raise _refuse(PromotionRefusal.FINGERPRINT_MISMATCH)
        await self._check_candidate_integrity(session, proposal)
        self._check_datasets(evaluation)
        moment = evaluation.finished_at or evaluation.started_at
        if now - moment > timedelta(days=policy.gates.promotion_max_eval_age_days):
            raise _refuse(PromotionRefusal.EVALUATION_TOO_OLD)
        self._check_metrics(evaluation)

    async def _check_candidate_integrity(
        self, session: AsyncSession, proposal: LearningProposal
    ) -> None:
        if proposal.kind != ProposalKind.POLICY.value:
            return
        try:
            PolicyProposalBody.model_validate(proposal.body)
        except ValueError:
            raise _refuse(PromotionRefusal.FROZEN_FIELD) from None
        candidate = await repo.candidate_for_proposal(session, proposal.id)
        if candidate is None or canonical_sha256(candidate.document) != candidate.document_sha256:
            raise _refuse(PromotionRefusal.FINGERPRINT_MISMATCH)

    @staticmethod
    def _check_datasets(evaluation: PolicyEvaluation) -> None:
        stored = dict(evaluation.dataset_manifests or {})
        if not stored:
            raise _refuse(PromotionRefusal.DATASET_CHANGED)
        try:
            from ken_contracts.learning import EvalSuite

            suites = [EvalSuite(name) for name in evaluation.suites]
            current = manifest_shas(load_suite_datasets(suites))
        except (DatasetError, ValueError):
            raise _refuse(PromotionRefusal.DATASET_CHANGED) from None
        if current != stored:
            raise _refuse(PromotionRefusal.DATASET_CHANGED)

    @staticmethod
    def _check_metrics(evaluation: PolicyEvaluation) -> None:
        try:
            metrics = EvaluationMetrics.model_validate(
                {k: v for k, v in evaluation.metrics.items() if k != "shadow"}
            )
        except ValueError:
            raise _refuse(PromotionRefusal.EVALUATION_NOT_PASSED) from None
        if any(not row.sufficient for row in metrics.by_decision_class.values()):
            raise _refuse(PromotionRefusal.SAMPLE_TOO_SMALL)
        codes = {str(reason.get("code")) for reason in evaluation.gate_reasons or ()}
        if GateReasonCode.BEHAVIOURAL_SIGNAL_NEGATIVE.value in codes:
            raise _refuse(PromotionRefusal.BEHAVIOURAL_BLOCK)

    # ----------------------------------------------------------------- events
    async def _emit_promoted(
        self,
        session: AsyncSession,
        *,
        proposal: LearningProposal,
        actor: str,
        now: datetime,
        evaluation_run_id: uuid.UUID | None,
    ) -> None:
        if evaluation_run_id is None:  # pragma: no cover - the CHECK and the request forbid it
            raise _refuse(PromotionRefusal.EVALUATION_MISSING)
        if proposal.kind == ProposalKind.SKILL.value:
            skill = await repo.skill_for_proposal(session, proposal.id)
            if skill is None:  # pragma: no cover - record_stage already required it
                raise _refuse(PromotionRefusal.TRANSITION_NOT_ALLOWED)
            name, version = skill.name, str(skill.version)
            artifact: Literal["skill", "policy"] = "skill"
            earlier = await repo.previous_skill_version(
                session, name=skill.name, below=skill.version
            )
            previous = None if earlier is None else str(earlier)
            name_event = EventName.SKILL_PROMOTED
        else:
            candidate = await repo.candidate_for_proposal(session, proposal.id)
            if candidate is None:  # pragma: no cover - checked in _check_candidate_integrity
                raise _refuse(PromotionRefusal.FINGERPRINT_MISMATCH)
            name, version = str(proposal.target_kind), candidate.candidate_version
            previous, artifact = proposal.base_version, "policy"
            name_event = EventName.POLICY_PROMOTED
        payload = PromotionAuditPayload(
            artifact_kind=artifact,
            name=name,
            version=version,
            previous_version=previous,
            promoted_by=actor,
            evaluation_ref=str(evaluation_run_id),
        )
        await append_event(
            session,
            EventEnvelope(
                event_name=name_event,
                occurred_at=now,
                emitted_by=LEARNING_EMITTED_BY,
                subject_type=artifact,
                subject_id=name,
                payload=payload.model_dump(mode="json"),
                correlation_id=proposal.id,
            ),
        )


# --------------------------------------------------------------------------- #
# The one writer of learning_proposal_transitions
# --------------------------------------------------------------------------- #
async def record_stage(
    session: AsyncSession,
    *,
    proposal: LearningProposal,
    to_status: ProposalStatus,
    actor: str,
    now: datetime,
    reason: ProposalRejectionReason | None = None,
    note: str | None = None,
    evaluation_run_id: uuid.UUID | None = None,
    promoter_established: bool = False,
) -> LearningProposalTransition:
    """Move a LOCKED proposal one step and write its audit row in the same transaction.

    The caller holds the proposal ``FOR UPDATE`` (:func:`repo.lock_proposal`) and owns the
    transaction. Checks, in order: the pair is in ``PROPOSAL_TRANSITIONS``; the actor's CLASS
    may reach ``to_status`` (:func:`may_advance_proposal`); ``promoted`` is only recorded by a
    caller that has already established the promoter setting.

    Raises:
        PromotionRefusedError: ``TRANSITION_NOT_ALLOWED``, ``NOT_A_HUMAN`` or
            ``PROMOTER_GUARD_MISSING``.
    """
    current = ProposalStatus(proposal.status)
    if to_status not in PROPOSAL_TRANSITIONS[current]:
        raise _refuse(PromotionRefusal.TRANSITION_NOT_ALLOWED)
    if not may_advance_proposal(actor, to_status):
        raise _refuse(PromotionRefusal.NOT_A_HUMAN)
    if to_status is ProposalStatus.PROMOTED and not promoter_established:
        raise _refuse(PromotionRefusal.PROMOTER_GUARD_MISSING)
    row = await repo.insert_transition(
        session,
        proposal_id=proposal.id,
        from_status=current,
        to_status=to_status,
        actor=actor,
        reason=None if reason is None else reason.value,
        note=note,
        evaluation_run_id=evaluation_run_id,
    )
    await repo.set_proposal_status(session, proposal_id=proposal.id, status=to_status, now=now)
    await _sync_skill(
        session,
        proposal=proposal,
        to_status=to_status,
        actor=actor,
        now=now,
        evaluation_run_id=evaluation_run_id,
    )
    return row


async def _sync_skill(
    session: AsyncSession,
    *,
    proposal: LearningProposal,
    to_status: ProposalStatus,
    actor: str,
    now: datetime,
    evaluation_run_id: uuid.UUID | None,
) -> None:
    """Keep the skill registry row in step with its proposal (skills have the SHORT ladder).

    Nothing consumes a skill at runtime in Phase 8, so this changes a status and nothing else.
    A previously promoted version of the same name is retired in the same transaction because
    at most one version per name may be ``promoted`` (a partial unique index).
    """
    if proposal.kind != ProposalKind.SKILL.value:
        return
    skill = await repo.skill_for_proposal(session, proposal.id)
    if skill is None:
        if to_status is ProposalStatus.PROMOTED:
            raise _refuse(PromotionRefusal.TRANSITION_NOT_ALLOWED)
        return
    current = SkillStatus(skill.status)
    if to_status is ProposalStatus.OFFLINE_TEST and current is SkillStatus.DRAFT:
        await repo.update_skill_status(session, skill_id=skill.id, status=SkillStatus.OFFLINE_TEST)
    elif to_status in (ProposalStatus.REJECTED, ProposalStatus.WITHDRAWN) and current in (
        SkillStatus.DRAFT,
        SkillStatus.OFFLINE_TEST,
    ):
        await repo.update_skill_status(session, skill_id=skill.id, status=SkillStatus.REJECTED)
    elif to_status is ProposalStatus.ROLLED_BACK and current is SkillStatus.PROMOTED:
        await repo.update_skill_status(session, skill_id=skill.id, status=SkillStatus.RETIRED)
    elif to_status is ProposalStatus.PROMOTED:
        if current is not SkillStatus.OFFLINE_TEST or evaluation_run_id is None:
            raise _refuse(PromotionRefusal.TRANSITION_NOT_ALLOWED)
        live = await repo.skill_by_name_status(
            session, name=skill.name, status=SkillStatus.PROMOTED
        )
        if live is not None:
            await repo.update_skill_status(session, skill_id=live.id, status=SkillStatus.RETIRED)
        run = await repo.evaluation_by_id(session, evaluation_run_id)
        total = int(run.metrics.get("cases_total", 0)) if run is not None else 0
        passed = int(run.metrics.get("cases_passed", 0)) if run is not None else 0
        await repo.update_skill_status(
            session,
            skill_id=skill.id,
            status=SkillStatus.PROMOTED,
            promoted_by=actor,
            promoted_at=now,
            evaluation_run_id=evaluation_run_id,
            evaluation_score=None if total == 0 else passed / total,
        )


# --------------------------------------------------------------------------- #
# The policy file write (make policy-promote) -- the only file write of the phase
# --------------------------------------------------------------------------- #
class PolicyWriteRefusedError(LearningError):
    """The writer refused. Carries a fixed code, never a path or exception text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


NO_PROMOTED_TRANSITION: Final = "no_human_promoted_transition"
NOT_A_POLICY: Final = "not_a_policy_proposal"
ROLLED_BACK: Final = "no_longer_promoted"
FILE_EXISTS: Final = "target_file_exists_with_different_content"
CANDIDATE_MISSING: Final = "candidate_missing"
WRITE_FAILED: Final = "write_failed"


@dataclass(frozen=True, slots=True)
class PromotedFiles:
    """What a promotion would write: repo-relative path -> YAML text, in WRITE ORDER."""

    files: tuple[tuple[str, str], ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(path for path, _ in self.files)


_NAMES: Final = {
    TargetKind.RANKING.value: ("ranking", "ranking_weights"),
    TargetKind.RECOMMENDATION.value: ("recommendation", "recommendation"),
    TargetKind.AGENTS.value: ("agents", "agents"),
}


def _relative(kind: str, version: int) -> str:
    folder, stem = _NAMES[kind]
    return f"policies/{folder}/{stem}.v{version}.yaml"


def _dump(document: dict[str, Any]) -> str:
    return str(
        yaml.safe_dump(document, sort_keys=False, default_flow_style=False, allow_unicode=True)
    )


def plan_promoted_files(
    *,
    target_kind: str,
    candidate_document: dict[str, Any],
    proposal_id: uuid.UUID,
    active_recommendation: RecommendationPolicy | None = None,
) -> PromotedFiles:
    """The files a promotion writes. For ``ranking`` that is TWO, weights first and the
    recommendation document whose ``ranking_policy:`` pointer names them second, because the
    ACTIVE ranking policy is resolved from that pointer and not from ``Settings`` (15.7.8)."""
    version = candidate_document.get("version")
    if not isinstance(version, int):
        raise PolicyWriteRefusedError(CANDIDATE_MISSING)
    document = {**candidate_document, "status": "promoted-candidate"}
    document["source"] = f"learning proposal {proposal_id}"
    if target_kind == TargetKind.RANKING.value:
        if active_recommendation is None:
            raise PolicyWriteRefusedError(CANDIDATE_MISSING)
        RankingPolicy.model_validate(document)
        weights_path = _relative(TargetKind.RANKING.value, version)
        recommendation = active_recommendation.model_dump(mode="json")
        recommendation["version"] = int(active_recommendation.version) + 1
        recommendation["ranking_policy"] = weights_path
        recommendation["status"] = "promoted-candidate"
        recommendation["source"] = f"learning proposal {proposal_id}"
        RecommendationPolicy.model_validate(recommendation)
        return PromotedFiles(
            files=(
                (weights_path, _dump(document)),
                (
                    _relative(TargetKind.RECOMMENDATION.value, recommendation["version"]),
                    _dump(recommendation),
                ),
            )
        )
    if target_kind == TargetKind.RECOMMENDATION.value:
        RecommendationPolicy.model_validate(document)
    return PromotedFiles(files=((_relative(target_kind, version), _dump(document)),))


async def write_promoted_policy(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    proposal_id: uuid.UUID,
    dry_run: bool = True,
    root: Path | None = None,
) -> tuple[str, ...]:
    """``PolicyFileWriter``: write the promoted policy file(s). Returns repo-relative paths.

    Refuses unless the proposal is ``promoted`` AND a transition into ``promoted`` exists whose
    actor has the human shape and which cites an evaluation. It reads those rows and writes
    nothing to the database: it can never create the transition it depends on.

    The write is atomic for the two-file ranking case: both files are staged next to their
    destinations and validated by parsing them back through the real policy models, then linked
    into place without overwriting anything; if the second link fails the first is removed. A
    previously released file is never edited or deleted -- an existing target is only accepted
    when its bytes are identical (an idempotent re-run).

    ``dry_run`` (the default) stops after planning. ``root`` exists for tests.
    """
    base = (root or REPO_ROOT).resolve()
    async with sessionmaker() as session:
        proposal = await repo.proposal_by_id(session, proposal_id)
        if proposal is None:
            raise ProposalNotFoundError
        if proposal.kind != ProposalKind.POLICY.value or proposal.target_kind is None:
            raise PolicyWriteRefusedError(NOT_A_POLICY)
        history = await repo.proposal_transitions(session, proposal.id)
        promoted = [
            row
            for row in history
            if row.to_status == ProposalStatus.PROMOTED.value
            and _PROMOTER_ACTOR.fullmatch(row.actor) is not None
            and row.evaluation_run_id is not None
        ]
        if not promoted:
            raise PolicyWriteRefusedError(NO_PROMOTED_TRANSITION)
        if proposal.status != ProposalStatus.PROMOTED.value:
            raise PolicyWriteRefusedError(ROLLED_BACK)
        candidate = await repo.candidate_for_proposal(session, proposal.id)
        if candidate is None or canonical_sha256(candidate.document) != candidate.document_sha256:
            raise PolicyWriteRefusedError(CANDIDATE_MISSING)
        document = dict(candidate.document)
        target_kind = str(proposal.target_kind)
        await session.rollback()
    active = None
    if target_kind == TargetKind.RANKING.value:
        active = load_recommendation_policy(settings.recommendation_policy_path)
    plan = plan_promoted_files(
        target_kind=target_kind,
        candidate_document=document,
        proposal_id=proposal_id,
        active_recommendation=active,
    )
    if dry_run:
        return plan.paths
    return _write_atomically(base, plan)


def _write_atomically(base: Path, plan: PromotedFiles) -> tuple[str, ...]:
    staged: list[tuple[Path, Path]] = []
    created: list[Path] = []
    try:
        for relative, content in plan.files:
            final = (base / relative).resolve()
            if not final.is_relative_to(base):
                raise PolicyWriteRefusedError(WRITE_FAILED)
            if final.exists():
                if final.read_text(encoding="utf-8") != content:
                    raise PolicyWriteRefusedError(FILE_EXISTS)
                continue
            final.parent.mkdir(parents=True, exist_ok=True)
            temporary = final.with_name(f".{final.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(content, encoding="utf-8")
            staged.append((temporary, final))
        for temporary, _final in staged:
            parsed: Any = yaml.safe_load(temporary.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise PolicyWriteRefusedError(WRITE_FAILED)
        for temporary, final in staged:
            os.link(temporary, final)  # fails if it exists: never an overwrite
            created.append(final)
    except PolicyWriteRefusedError:
        _cleanup(staged, created)
        raise
    except OSError:
        _cleanup(staged, created)
        raise PolicyWriteRefusedError(WRITE_FAILED) from None
    for temporary, _ in staged:
        temporary.unlink(missing_ok=True)
    return plan.paths


def _cleanup(staged: list[tuple[Path, Path]], created: list[Path]) -> None:
    for path in created:
        path.unlink(missing_ok=True)
    for temporary, _ in staged:
        temporary.unlink(missing_ok=True)
