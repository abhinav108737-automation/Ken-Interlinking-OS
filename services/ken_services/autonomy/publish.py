"""The autonomous publish path: :class:`AutonomousPublisher` (directive s14, s15; ADR-0018).

One plan, one pass, in this exact order::

    budget    the per-(source, target) retry budget and cool-down, UNDER THE PLAN'S LOCK
    VALIDATE  the deployment service's dry run (every ADR-0014 deterministic check)
    policy    the autonomy decision's policy version is re-asserted by re-evaluating
    decision  the autonomy engine's recorded decision, re-taken on CURRENT facts
    dry-run   the mutation is spliced against the CMS's live HTML, all-or-nothing
    version   the stored expected_version is checked by the CMS on apply (409 -> expired)
    apply     ONE CMS call, with the durable intent already committed (two transactions)
    verify    the decision's verification profile (STANDARD or STRICT)
    audit     link.deployed / link.verified / link.failed / link.rolled_back + an attempt row
    rollback  automatic, when verification failed and it is SAFE

Nothing here re-implements ADR-0014. The two-transaction durable intent, the per-source
advisory lock, the idempotency fingerprint and the splice-only mutation all belong to
:class:`~ken_services.deployment.service.DatabaseDeploymentService`, which this module calls
with exactly two differences from the operator path: the plans must be ``autonomy_decided``
rather than ``approved``, and the verification profile comes from the decision.

**The decision is re-taken immediately before the publish**, not read from the stored row. A
decision can be minutes old; a page can have been protected, unpublished or edited since.
Re-evaluating costs one snapshot read and removes a whole class of "it was safe when we
looked" bugs. The stored row is still what the audit trail shows, and a re-evaluation that
now BLOCKS moves the plan to ``blocked`` instead of publishing.

**Automatic rollback, and when it is unsafe.** On a verification failure the publisher rolls
the deployment back through ``CMSProvider.rollback`` -- the inverse splice ADR-0014 defines,
on the CMS's CURRENT HTML, so it is safe after unrelated later edits. It does NOT roll back
when:

* the deployment never reached the page (``failed`` / ``conflict``): there is nothing to
  remove, and the service answers ``nothing_to_roll_back``;
* the CMS refuses with ``rollback_conflict``: our link was itself changed on the page, so
  removing it would destroy somebody's edit. The failure is recorded, the attempt is counted,
  the cool-down starts, and a human is left to look -- which is the point of the
  blocked-actions screen.

Either way the attempt is recorded in ``publish_attempts`` with a ``cooldown_until``, so the
pair is not retried in a loop. Once ``attempts >= KEN_AUTONOMY_PUBLISH_MAX_ATTEMPTS`` the
pair is never published again, whatever a later decision says.

**An identity for the audit trail.** ``deployments.actor`` carries a ``^user:`` CHECK and
``user_id`` is a NOT NULL foreign key to ``users`` (migration 0007, unchanged by 0010), so a
deployment row cannot name a ``system:`` actor. The publisher therefore runs AS a named
operator account, which ``make autonomy-run AS_USER=<username>`` supplies and the CLI resolves
from the database. That is not an approval -- nobody is asked about any individual link -- it
is the accountable identity the CMS audit trail requires, chosen once per run. Plan
transitions and ``autonomy_decisions`` rows still carry ``system:`` actors, exactly as
ADR-0018 s3 says.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.auth import Principal
from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyEvaluation,
    AutonomyMode,
    VerificationProfile,
)
from ken_contracts.deployment import (
    DeploymentDetail,
    DeploymentError,
    DeploymentStatus,
    DeployRequest,
    RollbackReason,
    RollbackRequest,
)
from ken_contracts.recommendations import LinkPlanStatus
from ken_core.config import Settings
from ken_core.db.repositories import autonomy as autonomy_repo
from ken_core.logging import get_logger
from ken_services.autonomy.budget import BudgetRaceError, DatabaseRetryBudgetGate
from ken_services.autonomy.engine import (
    DECIDABLE_STATUSES,
    CriticDeclinedError,
    DatabaseAutonomyEngine,
    EvaluatedPlan,
)
from ken_services.autonomy.facts import plan_identity
from ken_services.autonomy.gate import PublishingNotEnabledError, require_publishing
from ken_services.autonomy.protocol import AUTONOMY_ACTOR
from ken_services.deployment.service import DatabaseDeploymentService
from ken_services.recommendations.validity import lock_source_plans

__all__ = [
    "PUBLISH_COUNTERS",
    "AutonomousPublisher",
    "PublishOutcome",
    "PublishResult",
]

logger = get_logger(__name__)

_S = LinkPlanStatus
_D = DeploymentStatus


class PublishOutcome(StrEnum):
    """What one plan's publish pass did. Stable codes: counters and logs use them."""

    PUBLISHED = "published"
    """Applied and verified."""
    PUBLISHED_UNVERIFIED = "published_unverified"
    """Applied; verification was inconclusive (transient). Not a failure, not a success."""
    ROLLED_BACK = "rolled_back"
    """Applied, verification FAILED, and the automatic rollback succeeded."""
    ROLLBACK_REFUSED = "rollback_refused"
    """Applied, verification failed, and rolling back was not safe. Recorded for a human."""
    BLOCKED = "blocked"
    """The re-taken decision blocks the plan. Nothing was sent to the CMS."""
    DEFERRED = "deferred"
    """Not published, and not the plan's fault: the budget, a cool-down, the mode, a lock, or
    a CMS that could not be reached."""
    REFUSED = "refused"
    """The deployment service refused at VALIDATE (a deterministic deploy-time check)."""
    DECLINED = "declined"
    """The critic's ``no_action``: an earlier outcome than a decision (ADR-0018 s1)."""


PUBLISH_COUNTERS: Final[tuple[str, ...]] = (
    "plans_considered",
    *(f"outcome_{outcome.value}" for outcome in PublishOutcome),
    "errors",
)


@dataclass(frozen=True, slots=True)
class PublishResult:
    plan_id: uuid.UUID
    outcome: PublishOutcome
    evaluation: AutonomyEvaluation | None = None
    deployment_id: uuid.UUID | None = None
    reasons: tuple[str, ...] = field(default=())
    """Stable codes: budget/verdict reasons, deployment issue codes. Never prose."""


class AutonomousPublisher:
    """Evaluates, publishes, verifies and rolls back -- without asking anyone."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        engine: DatabaseAutonomyEngine,
        deployments: DatabaseDeploymentService,
        budget: DatabaseRetryBudgetGate,
        principal: Principal,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._engine = engine
        self._deployments = deployments
        self._budget = budget
        self._principal = principal

    # ------------------------------------------------------------------ one
    async def publish_plan(
        self,
        *,
        plan_id: uuid.UUID,
        mode: AutonomyMode | None = None,
        job_run_id: uuid.UUID | None = None,
    ) -> PublishResult:
        """One plan, end to end. Never raises for a plan-level problem; see
        :class:`PublishOutcome`.

        Raises:
            PublishingNotEnabledError: the configured CMS and acknowledgements do not permit a
                publish in this mode. Raised BEFORE anything is evaluated, recorded or sent,
                so a misconfiguration costs zero CMS calls and writes no decision row. This is
                the runtime gate (:mod:`ken_services.autonomy.gate`) asked a second time after
                the `Settings` validator, because settings can be built without validation.
        """
        resolved = self._engine.resolve_mode(mode)
        if resolved is not AutonomyMode.SHADOW:
            require_publishing(self._settings, resolved)

        # Two cheap guards before anything is evaluated or recorded.
        #
        # 1. The plan's status. A plan that is already published, rolled back, rejected or
        #    expired is not the engine's to decide: a sweep never offers one (it reads
        #    DECIDABLE_STATUSES), and a direct call for one must answer rather than raise an
        #    illegal-transition error from three layers down.
        # 2. The pair's retry budget. Evaluating a pair that may never be published again
        #    would write a decision row per sweep for ever; the authoritative check is still
        #    the one under the plan's lock in :meth:`_apply`, immediately before the publish.
        async with self._sessionmaker() as session:
            identity = await plan_identity(session, plan_id=plan_id)
            await session.rollback()
        status = LinkPlanStatus(identity.status)
        if status not in DECIDABLE_STATUSES:
            return PublishResult(
                plan_id=plan_id,
                outcome=PublishOutcome.DEFERRED,
                reasons=("plan_not_decidable",),
            )
        if resolved is not AutonomyMode.SHADOW:
            early = await self._budget.check(
                source_page_id=identity.source_page_id,
                target_page_id=identity.target_page_id,
            )
            if not early.allowed:
                return PublishResult(
                    plan_id=plan_id,
                    outcome=PublishOutcome.DEFERRED,
                    reasons=early.reasons,
                )

        try:
            evaluated = await self._engine.evaluated(plan_id=plan_id, mode=resolved)
        except CriticDeclinedError:
            return PublishResult(plan_id=plan_id, outcome=PublishOutcome.DECLINED)
        overridden = await self._operator_override(plan_id=plan_id)
        if overridden and not evaluated.evaluation.blocks:
            # An operator override publishes a plan the engine declined. It waives the MODE
            # FLOORS -- the confidence, calibration and risk gates that exist to decide
            # WITHOUT a human -- and nothing else: the deterministic blocks were just
            # re-evaluated above and are empty, the whole VALIDATE -> apply -> verify path
            # still runs, and the profile is forced to STRICT. A plan whose blocks are NOT
            # empty falls through to the ordinary BLOCK below, so an override can never clear
            # a deterministic code even if one appeared after the override was recorded.
            evaluated = self._as_override(evaluated, mode=resolved)
        recorded = await self._engine.record(evaluated, job_run_id=job_run_id)
        evaluation = recorded.evaluation

        if resolved is AutonomyMode.SHADOW:
            # Shadow writes the counterfactual and NOTHING else: no deployment, no attempt,
            # no plan transition. The database guard enforces the last of those.
            return PublishResult(
                plan_id=plan_id,
                outcome=PublishOutcome.DEFERRED,
                evaluation=evaluation,
                reasons=("shadow_mode",),
            )
        if evaluation.decision is AutonomyDecision.BLOCK:
            return PublishResult(
                plan_id=plan_id,
                outcome=PublishOutcome.BLOCKED,
                evaluation=evaluation,
                reasons=tuple(code.value for code in evaluation.blocks) or ("policy_refused",),
            )
        return await self._apply(recorded, mode=resolved)

    @staticmethod
    def _as_override(evaluated: EvaluatedPlan, *, mode: AutonomyMode) -> EvaluatedPlan:
        """The same evaluation, re-decided as an operator-sanctioned publish."""
        if mode is AutonomyMode.SHADOW:
            return evaluated  # shadow publishes nothing, override or not
        if not evaluated.facts.rollback_metadata_present:
            return evaluated  # nothing is published that cannot be undone
        evaluation = evaluated.evaluation.model_copy(
            update={
                "decision": AutonomyDecision.AUTO_PUBLISH_WITH_EXTRA_VERIFICATION,
                "verification_profile": VerificationProfile.STRICT,
                "would_have_decision": None,
            }
        )
        return EvaluatedPlan(
            evaluation=evaluation, facts=evaluated.facts, identity=evaluated.identity
        )

    async def _operator_override(self, *, plan_id: uuid.UUID) -> bool:
        """Has an operator overridden this plan to publish, and not been superseded since?

        The newest decision row with an ``override_reason`` is the operator's; a later
        ``system:`` row means the engine has decided again and the override is spent.
        """
        async with self._sessionmaker() as session:
            row = await autonomy_repo.latest_decision(session, plan_id=plan_id)
            # Read the two columns INSIDE the session: ``expire_on_commit=False`` keeps the
            # instance usable, but a rollback detaches it and the next attribute access would
            # try to refresh against a closed session.
            found = None if row is None else (row.override_reason, AutonomyDecision(row.decision))
            await session.rollback()
        return found is not None and found[0] is not None and found[1] is not AutonomyDecision.BLOCK

    async def _apply(self, recorded: EvaluatedPlan, *, mode: AutonomyMode) -> PublishResult:
        evaluation = recorded.evaluation
        facts = recorded.facts
        plan_id = facts.plan_id
        # Asked once more, immediately before the deployment service (and so the CMS) is
        # reachable: a future caller of `_apply` gets the gate for free.
        require_publishing(self._settings, mode)

        # The budget, under the plan-writer lock, immediately before the publish. A decision
        # can be minutes old and the pair may have failed since.
        #
        # DEVIATION, stated plainly. The protocol docstring asks for the attempt to be
        # APPENDED in the same transaction as the deployment intent, so a crash between them
        # cannot lose an attempt and reopen the budget. It is not: `deploy()` owns its own two
        # transactions (ADR-0014's durable intent) and nothing can inject a write into tx1
        # without changing its signature, which is not this workstream's to change. What
        # closes the race instead:
        #
        # * `deploy()` holds a session-level advisory lock PER SOURCE across tx1, the CMS call
        #   and tx2, so two publishes of one source -- and therefore of one (source, target)
        #   pair -- cannot overlap; the second gets `deployment_in_progress`;
        # * the plan's `autonomy_decided -> validated` transition carries `expected_from`, so a
        #   second publisher of the same plan is refused by the transition service;
        # * `publish_attempts` has a UNIQUE (source, target, attempt) index, so even if both
        #   got through, the second INSERT is REFUSED rather than merged (`BudgetRaceError`).
        #
        # What remains is the crash window: a process killed between the CMS call and the
        # attempt row loses ONE attempt from the budget. `make deployments-reconcile` settles
        # the deployment, and the cool-down is the thing that actually prevents a hot retry
        # loop, so the residual risk is a budget one short -- not an unbounded retry.
        async with self._sessionmaker() as session, session.begin():
            await lock_source_plans(session, source_page_id=facts.source_page_id)
            verdict = await self._budget.check_in(
                session,
                source_page_id=facts.source_page_id,
                target_page_id=facts.target_page_id,
            )
        if not verdict.allowed:
            logger.info(
                "autonomy_publish_budget_refused",
                plan_id=str(plan_id),
                reasons=",".join(verdict.reasons),
                attempts=verdict.state.attempts,
            )
            return PublishResult(
                plan_id=plan_id,
                outcome=PublishOutcome.DEFERRED,
                evaluation=evaluation,
                reasons=verdict.reasons,
            )

        operation_id = uuid.uuid4()
        # The AUDIT LINK between the two records. `deployments.actor` must be a `user:`
        # account (CHECK), so a deployment row alone cannot say whether a human or the engine
        # chose to publish. The note names the `autonomy_decisions` row that sanctioned it, and
        # `publish_attempts` (actor system:autonomy-engine, deployment_id set) joins the two
        # from the other side; a human deploy has neither.
        async with self._sessionmaker() as session:
            decision_row = await autonomy_repo.latest_decision(session, plan_id=plan_id)
            decision_id = None if decision_row is None else int(decision_row.id)
            await session.rollback()
        request = DeployRequest(
            operation_id=operation_id,
            plan_ids=[plan_id],
            note=f"autonomous publish ({evaluation.decision.value}) decision#{decision_id}",
        )
        profile = VerificationProfile(evaluation.verification_profile)
        try:
            detail = await self._deployments.deploy(
                request=request,
                principal=self._principal,
                expected_status=_S.AUTONOMY_DECIDED,
                profile=profile,
            )
        except DeploymentError as exc:
            codes = tuple(sorted({str(issue.code) for issue in getattr(exc, "issues", ())}))
            await self._record_attempt(
                recorded,
                mode=mode,
                profile=profile,
                succeeded=False,
                failure_code=(codes[0] if codes else "deployment_refused")[:64],
                rolled_back=False,
                deployment_id=None,
            )
            logger.info("autonomy_publish_refused", plan_id=str(plan_id), codes=",".join(codes))
            return PublishResult(
                plan_id=plan_id,
                outcome=PublishOutcome.REFUSED,
                evaluation=evaluation,
                reasons=codes or ("deployment_refused",),
            )

        return await self._settle(recorded, detail, mode=mode, profile=profile)

    # -------------------------------------------------- verify and roll back
    async def _settle(
        self,
        recorded: EvaluatedPlan,
        detail: DeploymentDetail,
        *,
        mode: AutonomyMode,
        profile: VerificationProfile,
    ) -> PublishResult:
        """Turn the deployment's post-verification state into an outcome, rolling back when
        verification FAILED and it is safe to do so."""
        evaluation = recorded.evaluation
        plan_id = recorded.facts.plan_id
        status = DeploymentStatus(detail.status)

        if status is _D.VERIFIED:
            await self._record_attempt(
                recorded,
                mode=mode,
                profile=profile,
                succeeded=True,
                failure_code=None,
                rolled_back=False,
                deployment_id=detail.id,
            )
            return PublishResult(
                plan_id=plan_id,
                outcome=PublishOutcome.PUBLISHED,
                evaluation=evaluation,
                deployment_id=detail.id,
            )

        if status is _D.APPLIED:
            # Applied, verification inconclusive (publish lag, an unreachable target, a
            # transient 5xx). NOT a failure: nothing is rolled back on a check that could not
            # be made, and no cool-down is started. The verification sweep settles it.
            await self._record_attempt(
                recorded,
                mode=mode,
                profile=profile,
                succeeded=True,
                failure_code=None,
                rolled_back=False,
                deployment_id=detail.id,
            )
            return PublishResult(
                plan_id=plan_id,
                outcome=PublishOutcome.PUBLISHED_UNVERIFIED,
                evaluation=evaluation,
                deployment_id=detail.id,
                reasons=("verification_inconclusive",),
            )

        if status in {_D.FAILED, _D.CONFLICT}:
            # The change never reached the page: there is nothing to roll back.
            code = "cms_version_conflict" if status is _D.CONFLICT else "cms_refused"
            await self._record_attempt(
                recorded,
                mode=mode,
                profile=profile,
                succeeded=False,
                failure_code=code,
                rolled_back=False,
                deployment_id=detail.id,
            )
            return PublishResult(
                plan_id=plan_id,
                outcome=PublishOutcome.REFUSED,
                evaluation=evaluation,
                deployment_id=detail.id,
                reasons=(code,),
            )

        # verification_failed: the link is on the page and it is wrong. Roll back.
        return await self._rollback(recorded, detail, mode=mode, profile=profile)

    async def _rollback(
        self,
        recorded: EvaluatedPlan,
        detail: DeploymentDetail,
        *,
        mode: AutonomyMode,
        profile: VerificationProfile,
    ) -> PublishResult:
        plan_id = recorded.facts.plan_id
        reasons: tuple[str, ...] = ("verification_failed",)
        rolled_back = False
        try:
            after = await self._deployments.rollback(
                deployment_id=detail.id,
                request=RollbackRequest(
                    operation_id=uuid.uuid4(),
                    reason=RollbackReason.VERIFICATION_FAILED,
                    note="automatic rollback: verification failed",
                ),
                principal=self._principal,
            )
            rolled_back = DeploymentStatus(after.status) is _D.ROLLED_BACK
        except DeploymentError as exc:
            codes = tuple(sorted({str(issue.code) for issue in getattr(exc, "issues", ())}))
            reasons = ("verification_failed", *codes) if codes else reasons
            logger.warning("autonomy_rollback_refused", plan_id=str(plan_id), codes=",".join(codes))
        await self._record_attempt(
            recorded,
            mode=mode,
            profile=profile,
            succeeded=False,
            failure_code="verification_failed",
            rolled_back=rolled_back,
            deployment_id=detail.id,
        )
        return PublishResult(
            plan_id=plan_id,
            outcome=PublishOutcome.ROLLED_BACK if rolled_back else PublishOutcome.ROLLBACK_REFUSED,
            evaluation=recorded.evaluation,
            deployment_id=detail.id,
            reasons=reasons,
        )

    async def _record_attempt(
        self,
        recorded: EvaluatedPlan,
        *,
        mode: AutonomyMode,
        profile: VerificationProfile,
        succeeded: bool,
        failure_code: str | None,
        rolled_back: bool,
        deployment_id: uuid.UUID | None,
    ) -> None:
        """Append the attempt. A lost attempt row would reopen the budget, so a failure to
        write one is logged loudly and never swallowed into a success."""
        try:
            state = await self._budget.record(
                plan_id=recorded.facts.plan_id,
                source_page_id=recorded.facts.source_page_id,
                target_page_id=recorded.facts.target_page_id,
                decision=recorded.evaluation.decision,
                mode=mode,
                profile=profile,
                succeeded=succeeded,
                failure_code=failure_code,
                rolled_back=rolled_back,
                actor=AUTONOMY_ACTOR,
                deployment_id=deployment_id,
            )
        except BudgetRaceError:
            logger.warning("autonomy_attempt_race", plan_id=str(recorded.facts.plan_id))
            return
        logger.info(
            "autonomy_publish_attempt",
            plan_id=str(recorded.facts.plan_id),
            succeeded=succeeded,
            rolled_back=rolled_back,
            attempts=state.attempts,
            exhausted=state.exhausted,
            failure_code=failure_code or "",
        )

    # ----------------------------------------------------------------- many
    async def publish_plans(
        self,
        *,
        plan_ids: Sequence[uuid.UUID],
        mode: AutonomyMode | None = None,
        job_run_id: uuid.UUID | None = None,
    ) -> list[PublishResult]:
        """Every plan in order, isolated: one plan's failure never stops the pass."""
        out: list[PublishResult] = []
        for plan_id in plan_ids:
            try:
                out.append(
                    await self.publish_plan(plan_id=plan_id, mode=mode, job_run_id=job_run_id)
                )
            except PublishingNotEnabledError:
                raise  # a misconfiguration aborts the pass; it is not a per-plan problem
            except Exception as exc:  # the CLASS only: never exception text in a log
                logger.error(
                    "autonomy_publish_failed",
                    plan_id=str(plan_id),
                    error_class=type(exc).__name__,
                )
                out.append(
                    PublishResult(
                        plan_id=plan_id,
                        outcome=PublishOutcome.DEFERRED,
                        reasons=("unexpected_error",),
                    )
                )
        return out
