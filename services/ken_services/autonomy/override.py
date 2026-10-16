"""The operator override: :class:`DatabaseOverrideService` (ADR-0018 s5, directive s20/s22).

The exception path, and only the exception path. Normal publishing never comes through here;
ADR-0018 removed the obligation to approve every link, and what it left humans is monitoring,
policy administration, investigating blocks and rollbacks, and intervening on a specific
case. This is the intervention.

Two actions, one plan at a time, both audited:

``publish``
    put a plan the engine did not publish back on the autonomous path (``blocked`` ->
    ``autonomy_decided``). It does **NOT** waive anything: the deterministic blocks are
    RE-EVALUATED here, and a publish override is REFUSED while any code still fires. An
    operator who wants the link must first fix the fact that blocks it -- unprotect the page,
    publish the destination, repair the canonical -- which is the only honest meaning of
    "override" for a safety rule computed from stored facts.
``block``
    stop a plan the engine would publish (anything decidable -> ``blocked``). Always allowed:
    a human may always say no.

**Why a Principal and not an actor string.** The database can check that an actor LOOKS like
``user:<name>`` (``ck_autonomy_decisions_actor_shape``); only the session proves it IS one.
The service therefore takes a :class:`~ken_contracts.auth.Principal`, re-asserts
``Permission.DEPLOY`` (defence in depth behind the route's dependency), and derives the actor
from it -- never from the request body. This is the ADR-0016 15.7.3 rule applied to overrides.

**Idempotency and version-awareness.** ``operation_id`` makes a replay return the stored
result rather than acting twice, and ``expected_status`` is checked against the plan's live
status under its row lock, so an override racing an engine pass is refused instead of
applying to a plan that has moved on.

**What is recorded.** One ``autonomy_decisions`` row with a ``user:<name>`` actor and the
operator's reason -- the table's ``override_records_a_reason`` CHECK makes the reason
mandatory for a ``user:`` actor -- plus the ordinary ``link_plan_transitions`` row, in the
same transaction. The override does not itself publish: it puts the plan where the next
``make autonomy-run`` will pick it up, so the publish still goes through the whole
VALIDATE -> apply -> verify path with every check intact.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.auth import Permission, Principal, has_permission
from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyEvaluation,
    AutonomyMode,
    BlockCode,
    CriticOutcome,
    OperatorOverrideRequest,
    OperatorOverrideResponse,
    OverrideAction,
    RiskTier,
    VerificationProfile,
)
from ken_contracts.recommendations import LinkPlanStatus, PlanNotFoundError
from ken_core.config import Settings
from ken_core.db.models.autonomy import AutonomyDecisionRow
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow
from ken_core.db.repositories import autonomy as repo
from ken_core.db.repositories import recommendations as rec_repo
from ken_core.logging import get_logger
from ken_services.autonomy.policy import load_configured_autonomy_policy
from ken_services.autonomy.protocol import ENGINE_VERSION
from ken_services.jobs.runner import utc_now
from ken_services.recommendations.transitions import transition
from ken_services.recommendations.validity import lock_source_plans

__all__ = [
    "OVERRIDE_BLOCKED_BY_CODE",
    "OVERRIDE_NOT_OVERRIDABLE",
    "OVERRIDE_STATUS_MOVED",
    "DatabaseOverrideService",
    "OverridePermissionError",
]

logger = get_logger(__name__)

_S = LinkPlanStatus

OVERRIDE_BLOCKED_BY_CODE: Final = "a deterministic block still fires"
OVERRIDE_STATUS_MOVED: Final = "the plan is no longer in the expected status"
OVERRIDE_NOT_OVERRIDABLE: Final = "the plan's status cannot be overridden"

_PUBLISHABLE_FROM: Final[frozenset[LinkPlanStatus]] = frozenset({_S.BLOCKED})
"""A publish override puts a BLOCKED plan back on the ladder. ``blocked -> autonomy_decided``
is the only edge ``ALLOWED_TRANSITIONS`` offers out of ``blocked``, and that is deliberate:
there is nowhere else for an override to go."""
_BLOCKABLE_FROM: Final[frozenset[LinkPlanStatus]] = frozenset(
    {_S.PROPOSED, _S.CRITIQUED, _S.POLICY_CHECKED, _S.AUTONOMY_DECIDED, _S.AWAITING_APPROVAL}
)
"""A human may stop anything that has not yet been written to the CMS. A ``validated`` or
``deployed`` plan is past this point: that is a ROLLBACK, not a block."""


class OverridePermissionError(PermissionError):
    """The principal may not override. The message never names the principal."""

    def __init__(self) -> None:
        super().__init__("this role may not override an autonomy decision")


class DatabaseOverrideService:
    """:class:`~ken_services.autonomy.protocol.OperatorOverrideService` over PostgreSQL."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._clock = clock

    @staticmethod
    def _actor(principal: Principal) -> str:
        if not has_permission(principal.role, Permission.DEPLOY):
            raise OverridePermissionError
        return principal.actor

    async def override(
        self,
        *,
        principal: Principal,
        plan_id: uuid.UUID,
        request: OperatorOverrideRequest,
    ) -> OperatorOverrideResponse:
        """Publish or block ONE plan, with a recorded actor and reason.

        Raises:
            OverridePermissionError: the role lacks ``deploy``.
            PlanNotFoundError: no such plan.
        """
        actor = self._actor(principal)
        action = OverrideAction(request.action)
        replay = await self._replay(plan_id=plan_id, request=request, actor=actor)
        if replay is not None:
            return replay

        blocks = await self._blocks(plan_id=plan_id)
        async with self._sessionmaker() as session, session.begin():
            row = await session.get(LinkPlanRow, plan_id)
            if row is None:
                raise PlanNotFoundError
            await lock_source_plans(session, source_page_id=row.source_page_id)
            locked = await rec_repo.lock_plan(session, plan_id=plan_id)
            if locked is None:  # pragma: no cover - plans are never deleted
                raise PlanNotFoundError
            status = LinkPlanStatus(locked.status)
            refusal = self._refusal(action, status=status, request=request, blocks=blocks)
            if refusal is not None:
                return OperatorOverrideResponse(
                    plan_id=plan_id,
                    action=action,
                    applied=False,
                    plan_status=status,
                    actor=actor,
                    blocks=blocks if action is OverrideAction.PUBLISH else (),
                    refusal=refusal,
                    occurred_at=self._clock(),
                )
            target = _S.AUTONOMY_DECIDED if action is OverrideAction.PUBLISH else _S.BLOCKED
            decision = (
                AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION
                if action is OverrideAction.PUBLISH
                else AutonomyDecision.BLOCK
            )
            now = self._clock()
            await transition(
                session,
                plan_id=plan_id,
                to_status=target,
                actor=actor,
                note=f"operator override {action.value}: {request.reason}"[:500],
                expected_from=status,
                clock=self._clock,
            )
            evaluation = AutonomyEvaluation(
                plan_id=plan_id,
                decision=decision,
                # An override is recorded under the mode in force, so the audit trail shows
                # the regime the human acted inside -- except in shadow, where nothing may
                # record a publish: an override there is a GUARDED-mode act by definition,
                # since the operator is deliberately stepping outside the shadow regime.
                mode=self._override_mode(decision),
                risk_tier=RiskTier.HIGH,
                confidence=locked.confidence,
                calibration_version=locked.calibration_version,
                critic_outcome=CriticOutcome.ACCEPT,
                critic_verdict=None,
                policy_version=load_configured_autonomy_policy(self._settings).version,
                blocks=(),
                rollback_available=True,
                verification_profile=(
                    VerificationProfile.STRICT
                    if decision is AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION
                    else VerificationProfile.STANDARD
                ),
                would_have_decision=None,
                evaluated_at=now,
                engine_version=ENGINE_VERSION,
            )
            await repo.insert_decision(
                session,
                evaluation=evaluation,
                actor=actor,
                override_reason=request.reason,
            )
            logger.info(
                "autonomy_override_applied",
                plan_id=str(plan_id),
                action=action.value,
                from_status=status.value,
                to_status=target.value,
            )
            return OperatorOverrideResponse(
                plan_id=plan_id,
                action=action,
                applied=True,
                plan_status=target,
                actor=actor,
                blocks=(),
                refusal=None,
                occurred_at=now,
            )

    def _override_mode(self, decision: AutonomyDecision) -> AutonomyMode:
        """The mode an override row is recorded under.

        An override is NEVER recorded as ``shadow``, in either direction, and the database
        says so twice: ``ken_autonomy_decision_guard()`` refuses a shadow decision that MOVED
        its plan (and an override always moves one), and ``shadow_never_publishes`` refuses a
        shadow row with a publish decision. Both are right -- a human acting on one specific
        plan is by definition stepping outside the shadow regime, which is exactly what
        ``guarded`` means. On a ``guarded`` or ``autonomous`` installation the configured mode
        is recorded as it is, so the audit trail shows the regime the human acted inside.
        """
        del decision
        configured = AutonomyMode(self._settings.autonomy_mode)
        return AutonomyMode.GUARDED if configured is AutonomyMode.SHADOW else configured

    def _refusal(
        self,
        action: OverrideAction,
        *,
        status: LinkPlanStatus,
        request: OperatorOverrideRequest,
        blocks: tuple[BlockCode, ...],
    ) -> str | None:
        if status is not LinkPlanStatus(request.expected_status):
            return OVERRIDE_STATUS_MOVED
        allowed = _PUBLISHABLE_FROM if action is OverrideAction.PUBLISH else _BLOCKABLE_FROM
        if status not in allowed:
            return OVERRIDE_NOT_OVERRIDABLE
        if action is OverrideAction.PUBLISH and blocks:
            # A deterministic block is NEVER overridable. Fix the fact, not the decision.
            return OVERRIDE_BLOCKED_BY_CODE
        return None

    async def _blocks(self, *, plan_id: uuid.UUID) -> tuple[BlockCode, ...]:
        """Re-evaluate the deterministic blocks for this plan, now.

        Built here rather than injected so the API's override route needs no composition
        root; it is the same :class:`~ken_services.autonomy.blocks.DeterministicBlocks` the
        engine uses, so the answer cannot drift between the two.
        """
        from ken_services.autonomy.compose import build_block_evaluator

        evaluator = build_block_evaluator(
            sessionmaker=self._sessionmaker, settings=self._settings, clock=self._clock
        )
        return evaluator.evaluate(await evaluator.facts(plan_id=plan_id))

    # -------------------------------------------------------- idempotency
    async def _replay(
        self, *, plan_id: uuid.UUID, request: OperatorOverrideRequest, actor: str
    ) -> OperatorOverrideResponse | None:
        """Return the stored result when this override has ALREADY been applied.

        ``autonomy_decisions`` is append-only and has no operation column -- adding one needs
        a migration this workstream does not own, and the ``override_reason`` column cannot
        carry the id without truncating a 500-character reason. So the replay is detected from
        STATE, which is the thing a caller actually cares about: the newest ``user:`` override
        of this plan has the same action and the same reason, and the plan is already in the
        status that action puts it in. A retry of the same request therefore returns the same
        answer, and a different request against a plan an override already moved is refused by
        the ``expected_status`` check instead of acting twice.
        """
        del actor
        action = OverrideAction(request.action)
        target = _S.AUTONOMY_DECIDED if action is OverrideAction.PUBLISH else _S.BLOCKED
        async with self._sessionmaker() as session:
            plan = await session.get(LinkPlanRow, plan_id)
            if plan is None:
                await session.rollback()
                raise PlanNotFoundError
            status = LinkPlanStatus(plan.status)
            row = (
                await session.scalars(
                    select(AutonomyDecisionRow)
                    .where(
                        AutonomyDecisionRow.plan_id == plan_id,
                        AutonomyDecisionRow.override_reason.is_not(None),
                    )
                    .order_by(AutonomyDecisionRow.id.desc())
                    .limit(1)
                )
            ).first()
            # Read every column INSIDE the session: a rollback detaches the instance and the
            # next attribute access would try to refresh against a closed session.
            stored = (
                None
                if row is None
                else (
                    AutonomyDecision(row.decision),
                    row.override_reason,
                    row.actor,
                    row.evaluated_at,
                )
            )
            await session.rollback()
        if stored is None or status is not target:
            return None
        decision, reason, actor_recorded, when = stored
        stored_action = (
            OverrideAction.BLOCK if decision is AutonomyDecision.BLOCK else OverrideAction.PUBLISH
        )
        if stored_action is not action or reason != request.reason:
            return None
        return OperatorOverrideResponse(
            plan_id=plan_id,
            action=action,
            applied=True,
            plan_status=status,
            actor=actor_recorded,
            blocks=(),
            refusal=None,
            occurred_at=when,
        )
