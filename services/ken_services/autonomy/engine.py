"""The autonomy engine: :class:`DatabaseAutonomyEngine` (ADR-0018 s1, s2, s6).

It DECIDES and RECORDS. It never writes to the CMS; :mod:`ken_services.autonomy.publish`
does that, and only for a decision this engine already took.

How one decision is reached, in this order and no other::

    0. the critic declined          -> NO_ACTION: not evaluated at all (see below)
    1. the deterministic blocks      evaluate_blocks(facts)   -> any code means BLOCK
    2. the critic outcome            agent_run_plans.verdict  -> reject means BLOCK
    3. the calibrated confidence     policy_evaluations       -> LOWER only; version or None
    4. the risk tier                 stored signals           -> RAISE only
    5. the mode                      shadow / guarded / autonomous
    6. decision_for(...)             the ONE place the mapping lives (frozen protocol)
    7. the mode's floors             confidence and calibration -> may only BLOCK
    8. rollback availability         no rollback, no publish

Step 1 runs first and short-circuits. Steps 2-8 can only make the outcome MORE conservative:
a critic can decline, a calibration can lower a confidence, a signal can raise a tier, a
floor can block. **Nothing in steps 2-8 can clear a block** -- the fired codes are passed to
:func:`~ken_services.autonomy.protocol.decision_for`, which returns ``BLOCK`` on a non-empty
sequence before it looks at anything else, so a blocked plan stays blocked even when every
other input says publish. ``tests/unit/autonomy/test_block_determinism.py`` drives exactly
that case with a model provider that answers ``accept``.

**Where NO_ACTION went.** ADR-0018 s1 calls the critic's ``no_action`` "a separate, EARLIER
outcome" that "never becomes a publish", and the frozen
:class:`~ken_contracts.autonomy.AutonomyEvaluation` validator refuses to pair
``critic_outcome=no_action`` with ``decision=block``. Those two rules together say a
``no_action`` plan is not evaluated at all, so :meth:`evaluate` raises
:class:`CriticDeclinedError` for one and a sweep counts it as declined. Writing a
``no_action`` row would require giving it a publish decision, which is the one thing the ADR
forbids (the database now forbids it too: ``ck_autonomy_decisions_no_action_never_decided``).

An ``uncertain`` critic review is ``no_action`` as well, ALWAYS (:func:`critic_outcome_of`):
it never raised a tier or demanded extra verification and then published. That mapping was
fail-open for the legacy rows that could still carry it and was corrected in the ADR-0019
follow-up.

**The three decisions.**

``BLOCK``
    a deterministic code fired; or the critic rejected; or the mode is ``shadow``; or the
    mode is ``guarded`` and the plan is not low-risk, high-confidence and CALIBRATED; or the
    confidence is under the mode's floor; or no rollback is available.
``AUTO_PUBLISH``
    everything above passed, with the configured default verification profile.
``AUTO_PUBLISH_WITH_EXTRA_VERIFICATION``
    the SAME safety rules and the strict verification profile. Reached for ``high`` risk, an
    uncalibrated confidence, or ``strict`` configured -- cases where more checking after the
    write is worth its cost. (An ``uncertain`` critic is NOT one of them: it is declined, see
    :func:`critic_outcome_of`.) It is never a weaker gate: it comes out of the same
    :func:`decision_for` call and the same floors.

**Shadow mode** records exactly one thing: an ``autonomy_decisions`` row whose ``decision``
is ``block`` and whose ``would_have_decision`` carries what ``autonomous`` mode would have
done. No ``deployments`` row, no ``publish_attempts`` row, no plan transition -- the DEFERRED
``ken_autonomy_decision_guard()`` refuses a shadow row that moved its plan, so the database
enforces it too.

**The status ladder.** The engine drives ``proposed`` / ``critiqued`` -> ``policy_checked``
-> ``autonomy_decided`` (the deterministic block evaluation IS the policy check) or
``-> blocked``, in ONE transaction with the decision row, as migration 0010's guard requires.
A ``guarded``-mode deferral is a ``BLOCK`` with no fired codes: the guard's status mapping is
fixed (``block`` -> ``blocked``), so the deferral queue is the blocked-actions screen plus an
operator override, not ``awaiting_approval``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.agents import CriticVerdict
from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyEvaluation,
    AutonomyMode,
    AutonomyPolicy,
    BlockCode,
    CriticOutcome,
    RiskTier,
    VerificationProfile,
)
from ken_contracts.intelligence import TaxonomyPolicy
from ken_contracts.recommendations import (
    LinkPlanStatus,
    PlanNotFoundError,
    RecommendationPolicy,
)
from ken_core.config import Settings
from ken_core.db.models.agents import AgentRunPlan
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow
from ken_core.db.repositories import autonomy as repo
from ken_core.logging import get_logger
from ken_services.autonomy.blocks import DeterministicBlocks, evaluate_blocks
from ken_services.autonomy.calibration import EvaluationCalibrationReader
from ken_services.autonomy.facts import PlanIdentity, load_plan_facts, snapshot
from ken_services.autonomy.protocol import (
    AUTONOMY_ACTOR,
    ENGINE_VERSION,
    CalibratedConfidence,
    PlanFacts,
    decision_for,
    profile_for,
    target_status_for,
)
from ken_services.autonomy.reasons import MAX_NOTE_CHARS, block_note
from ken_services.autonomy.risk import DeterministicRiskClassifier
from ken_services.jobs.runner import utc_now
from ken_services.recommendations.transitions import transition
from ken_services.recommendations.validity import lock_source_plans

__all__ = [
    "DECIDABLE_STATUSES",
    "MODE_ORDER",
    "SHADOW_ONLY_STATUSES",
    "CriticDeclinedError",
    "DatabaseAutonomyEngine",
    "EvaluatedPlan",
    "ShadowOnlyStatusError",
    "critic_outcome_of",
]

logger = get_logger(__name__)

_S = LinkPlanStatus

DECIDABLE_STATUSES: Final[tuple[LinkPlanStatus, ...]] = (
    _S.PROPOSED,
    _S.CRITIQUED,
    _S.POLICY_CHECKED,
    _S.AUTONOMY_DECIDED,
)
"""Statuses an evaluation may be taken on.

``blocked`` is absent: re-entering the autonomous path from ``blocked`` is an OPERATOR act
(:class:`~ken_services.autonomy.override.DatabaseOverrideService`), never an engine sweep.
``approved`` is absent too, and deliberately: ``ALLOWED_TRANSITIONS`` has no
``approved -> autonomy_decided`` edge, because an approved plan is an exception a human
already took responsibility for and goes to the ordinary deployment service.
``autonomy_decided`` IS here, so a plan whose facts changed after its decision is blocked by
the next pass instead of being published on a stale decision."""

SHADOW_ONLY_STATUSES: Final[tuple[LinkPlanStatus, ...]] = (_S.AWAITING_APPROVAL,)
"""Statuses a SHADOW pass may evaluate but no publishing pass may touch.

``awaiting_approval`` is the pre-ADR-0018 hand-off state (and still the operator-exception
state). ``ALLOWED_TRANSITIONS`` (and the database guard) has no edge from it onto the
autonomous ladder, so the engine can neither advance nor decide such a plan. A shadow pass writes
a decision row and moves nothing, so it can say what WOULD happen without touching the plan's
status or history. Going further is an operator act (override / approval), or the explicit,
audited ``legacy-reconcile`` command (``ken_services.autonomy.legacy``)."""

_TO_POLICY_CHECKED: Final[frozenset[LinkPlanStatus]] = frozenset({_S.PROPOSED, _S.CRITIQUED})
"""Statuses the engine first advances to ``policy_checked``: the deterministic policy check
it just performed IS that step of the ladder (ADR-0018 s3)."""

MODE_ORDER: Final[tuple[AutonomyMode, ...]] = (
    AutonomyMode.SHADOW,
    AutonomyMode.GUARDED,
    AutonomyMode.AUTONOMOUS,
)
"""Least to most permissive. A run may move DOWN this list, never up."""


class ShadowOnlyStatusError(Exception):
    """A publishing pass reached a plan whose status only a shadow pass may evaluate."""

    def __init__(self, plan_id: uuid.UUID) -> None:
        super().__init__(f"plan {plan_id} is in a shadow-evaluation-only status")
        self.plan_id = plan_id


class CriticDeclinedError(Exception):
    """The critic's outcome is ``no_action``: there is nothing to decide (see the module
    docstring). Not an error in the plan -- an earlier outcome than the decision."""

    def __init__(self, plan_id: uuid.UUID) -> None:
        self.plan_id = plan_id
        super().__init__("the critic declined to act on this plan")


@dataclass(frozen=True, slots=True)
class EvaluatedPlan:
    """An evaluation plus the inputs the publisher needs, so it need not recompute them."""

    evaluation: AutonomyEvaluation
    facts: PlanFacts
    identity: PlanIdentity
    decision_row_id: int | None = None
    """``None`` when nothing was recorded: the plan was already in the status the decision
    puts it in, so there was no transition for the deferred guard to match."""


def critic_outcome_of(verdict: CriticVerdict | None, *, handed_off: bool | None) -> CriticOutcome:
    """The autonomy-level view of the critic step, read back from a STORED review.

    **FAIL CLOSED.** An ``uncertain``, failed or unparseable review must NEVER map to accept
    (ADR-0019 and the critic workstream's non-negotiable rule). This function agrees with the
    pure reference mapping :func:`ken_agents.critic.critic_outcome` and may only be STRICTER
    than it, never more permissive; ``tests/unit/agents/test_critic.py`` pins that over every
    verdict and both ``handed_off`` values.

    * ``accept`` -> :attr:`CriticOutcome.ACCEPT`, unless the run did NOT hand the plan on
      (``handed_off is False``): the critic found no defect and the run still declined to pass
      it along, which is "nothing worth doing" -> :attr:`CriticOutcome.NO_ACTION`.
    * ``reject`` -> :attr:`CriticOutcome.REJECT`.
    * ``uncertain`` -> :attr:`CriticOutcome.NO_ACTION`, ALWAYS, whatever ``handed_off`` says.
      A doubt is not a licence. Since the critic change an uncertain review is recorded
      ``handed_off = false`` and the plan stays ``proposed``, so only a row written before that
      change can carry ``uncertain`` with ``handed_off = true`` -- a LEGACY row. It is declined
      exactly like a fresh one: the engine raises :class:`CriticDeclinedError`, the sweep
      counts the plan as declined, and no ``autonomy_decisions`` row is written (the database
      forbids one: ``ck_autonomy_decisions_no_action_never_decided``). It does NOT raise a risk
      tier or demand extra verification and publish anyway; that was a fail-open mapping.
    * any other verdict value -> :attr:`CriticOutcome.NO_ACTION` (an unknown value is never
      permissive).
    * no critic row at all -> :attr:`CriticOutcome.ACCEPT`. The critic is an optional step of
      the ladder; what makes a plan safe is the deterministic layer, not the critic.
    """
    if verdict is None:
        return CriticOutcome.ACCEPT
    if verdict is CriticVerdict.REJECT:
        return CriticOutcome.REJECT
    if verdict is CriticVerdict.ACCEPT and handed_off is not False:
        return CriticOutcome.ACCEPT
    return CriticOutcome.NO_ACTION


@dataclass(frozen=True, slots=True)
class _Inputs:
    """Everything one decision is computed from, gathered once."""

    facts: PlanFacts
    identity: PlanIdentity
    blocks: tuple[BlockCode, ...]
    critic: CriticOutcome
    verdict: CriticVerdict | None
    calibrated: CalibratedConfidence
    tier: RiskTier
    extra_verification: bool


class DatabaseAutonomyEngine:
    """:class:`~ken_services.autonomy.protocol.AutonomyEngine` over PostgreSQL."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        policy: RecommendationPolicy,
        taxonomy: TaxonomyPolicy,
        autonomy_policy: AutonomyPolicy,
        blocks: DeterministicBlocks,
        risk: DeterministicRiskClassifier,
        calibration: EvaluationCalibrationReader,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._policy = policy
        self._taxonomy = taxonomy
        # The versioned autonomy policy (policies/autonomy/autonomy.v<N>.yaml): every floor
        # below comes from it, and its version is recorded on every decision row.
        self._autonomy_policy = autonomy_policy
        self._blocks = blocks
        self._risk = risk
        self._calibration = calibration
        self._clock = clock

    # ------------------------------------------------------------------ mode
    @property
    def configured_mode(self) -> AutonomyMode:
        return AutonomyMode(self._settings.autonomy_mode)

    def resolve_mode(self, requested: AutonomyMode | None) -> AutonomyMode:
        """The mode in force. A request may only make the pass MORE conservative.

        ``make autonomy-shadow`` can force ``shadow`` while the configured mode publishes;
        the reverse is refused here, because a command-line argument must never turn a
        ``shadow`` installation into a publishing one. That takes the two configuration
        acknowledgements ADR-0018 s2 requires, which ``Settings`` checks at startup.
        """
        configured = self.configured_mode
        if requested is None or requested is configured:
            return configured
        if MODE_ORDER.index(requested) > MODE_ORDER.index(configured):
            raise ValueError(
                "a run cannot select a more permissive autonomy mode than KEN_AUTONOMY_MODE"
            )
        return requested

    # --------------------------------------------------------------- blocks
    async def deterministic_blocks(self, *, plan_id: uuid.UUID) -> tuple[BlockCode, ...]:
        """The frozen contract's entry point: facts, then the pure evaluator. No inference."""
        return evaluate_blocks(await self._blocks.facts(plan_id=plan_id))

    # ------------------------------------------------------------- evaluate
    async def evaluate(
        self, *, plan_id: uuid.UUID, mode: AutonomyMode | None = None
    ) -> AutonomyEvaluation:
        """Decide without recording. Used by the API, the evals and a dry run.

        Raises:
            PlanNotFoundError: no such plan.
            CriticDeclinedError: the critic's outcome is ``no_action``.
        """
        return (await self.evaluated(plan_id=plan_id, mode=mode)).evaluation

    async def evaluated(
        self, *, plan_id: uuid.UUID, mode: AutonomyMode | None = None
    ) -> EvaluatedPlan:
        resolved = self.resolve_mode(mode)
        inputs = await self._gather(plan_id=plan_id)
        return self._assemble(inputs, mode=resolved)

    async def _gather(self, *, plan_id: uuid.UUID) -> _Inputs:
        async with self._sessionmaker() as session:
            await snapshot(session)  # ONE snapshot for the whole decision
            facts, identity = await load_plan_facts(
                session,
                plan_id=plan_id,
                policy=self._policy,
                taxonomy=self._taxonomy,
                now=self._clock(),
                scope_hosts=self._blocks.scope_hosts(),
                max_redirect_hops=self._autonomy_policy.blocks.max_redirect_hops,
            )
            verdict, handed_off = await self._critique(session, plan_id=plan_id)
            await session.rollback()  # the evaluation itself writes nothing

        # 1. THE DETERMINISTIC LAYER, FIRST. Nothing below can clear a code.
        blocks = evaluate_blocks(facts)

        # 2. The critic. `no_action` is an EARLIER outcome, not an input to weigh.
        critic = critic_outcome_of(verdict, handed_off=handed_off)
        if critic is CriticOutcome.NO_ACTION:
            raise CriticDeclinedError(plan_id)

        # 3. Calibration: lower only; a None version means RAW.
        calibrated = await self._calibration.calibrate_for(facts=facts, identity=identity)

        # 4. Risk: raise only. It is computed AFTER calibration, because "this decision class
        # has no calibration" is itself a risk signal and the plan's own stored field cannot
        # answer that (a Phase 3 plan always carries a NULL calibration version).
        tier = await self._risk.classify_with(facts=facts, calibrated=calibrated)

        return _Inputs(
            facts=facts,
            identity=identity,
            blocks=blocks,
            critic=critic,
            verdict=verdict,
            calibrated=calibrated,
            tier=tier,
            # More checking AFTER the write, for the cases where it is worth its cost. It
            # changes the verification profile and NOTHING else (ADR-0018 s4).
            extra_verification=not calibrated.is_calibrated,
        )

    def _decide(self, inputs: _Inputs, *, mode: AutonomyMode) -> AutonomyDecision:
        """The decision these inputs imply under ``mode``. Pure; blocks win first."""
        decision = decision_for(
            blocks=inputs.blocks,
            mode=mode,
            risk_tier=inputs.tier,
            extra_verification=inputs.extra_verification,
        )
        if decision is AutonomyDecision.BLOCK:
            return decision
        if inputs.critic is CriticOutcome.REJECT:
            return AutonomyDecision.BLOCK
        # Nothing is published that cannot be undone. Three independent statements of one
        # rule: the MISSING_ROLLBACK_METADATA block code, this line, and the
        # ``no_publish_without_rollback`` CHECK on the table.
        if not inputs.facts.rollback_metadata_present:
            return AutonomyDecision.BLOCK
        return self._apply_floors(decision, mode=mode, inputs=inputs)

    def _assemble(self, inputs: _Inputs, *, mode: AutonomyMode) -> EvaluatedPlan:
        decision = self._decide(inputs, mode=mode)
        counterfactual: AutonomyDecision | None = None
        if mode is AutonomyMode.SHADOW:
            # What `autonomous` would have done, on the same facts and the same rules.
            counterfactual = self._decide(inputs, mode=AutonomyMode.AUTONOMOUS)
            decision = AutonomyDecision.BLOCK
        profile = profile_for(decision, default=self._default_profile())
        evaluation = AutonomyEvaluation(
            plan_id=inputs.facts.plan_id,
            decision=decision,
            mode=mode,
            risk_tier=inputs.tier,
            confidence=inputs.calibrated.value,
            calibration_version=inputs.calibrated.calibration_version,
            critic_outcome=inputs.critic,
            critic_verdict=inputs.verdict,
            policy_version=self._autonomy_policy.version,
            blocks=inputs.blocks,
            rollback_available=inputs.facts.rollback_metadata_present,
            verification_profile=profile,
            would_have_decision=counterfactual,
            evaluated_at=self._clock(),
            engine_version=ENGINE_VERSION,
        )
        return EvaluatedPlan(evaluation=evaluation, facts=inputs.facts, identity=inputs.identity)

    def _apply_floors(
        self, decision: AutonomyDecision, *, mode: AutonomyMode, inputs: _Inputs
    ) -> AutonomyDecision:
        """The mode's confidence and calibration floors. They BLOCK or pass; never upgrade."""
        calibrated = inputs.calibrated
        if mode is AutonomyMode.GUARDED:
            # ADR-0011: no threshold may treat raw as calibrated, so guarded refuses on a
            # None version outright -- which keeps Phase 9 calibration on the critical path
            # instead of making it optional.
            if not calibrated.is_calibrated:
                return AutonomyDecision.BLOCK
            floor = self._autonomy_policy.confidence.guarded_min_confidence
            if inputs.tier is not RiskTier.LOW or calibrated.value < floor:
                return AutonomyDecision.BLOCK
            return decision
        floor = self._autonomy_policy.confidence.autonomous_min_confidence
        if mode is AutonomyMode.AUTONOMOUS and calibrated.value < floor:
            return AutonomyDecision.BLOCK
        return decision

    def _default_profile(self) -> VerificationProfile:
        return VerificationProfile(self._settings.autonomy_verification_profile)

    @staticmethod
    async def _critique(
        session: AsyncSession, *, plan_id: uuid.UUID
    ) -> tuple[CriticVerdict | None, bool | None]:
        row = (
            await session.execute(
                select(AgentRunPlan.verdict, AgentRunPlan.handed_off).where(
                    AgentRunPlan.plan_id == plan_id
                )
            )
        ).first()
        if row is None:
            return None, None
        return CriticVerdict(row.verdict), bool(row.handed_off)

    # --------------------------------------------------------------- record
    async def evaluate_and_record(
        self, *, plan_id: uuid.UUID, mode: AutonomyMode | None = None
    ) -> AutonomyEvaluation:
        """Evaluate, then write the decision row and (outside shadow) move the plan.

        The transitions and the decision row are written by the SAME transaction, which
        migration 0010's deferred guard requires; in shadow mode the guard requires the
        opposite -- no transition at all -- so the two paths are physically distinct and
        neither can be reached by accident.
        """
        return (await self.record(await self.evaluated(plan_id=plan_id, mode=mode))).evaluation

    async def record(
        self, evaluated: EvaluatedPlan, *, job_run_id: uuid.UUID | None = None
    ) -> EvaluatedPlan:
        """Persist one evaluation. Returns it with ``decision_row_id`` set, or unchanged when
        the plan was already in the status the decision puts it in (an idempotent re-run)."""
        evaluation = evaluated.evaluation
        row_id: int | None = None
        async with self._sessionmaker() as session, session.begin():
            await lock_source_plans(session, source_page_id=evaluated.facts.source_page_id)
            row = await session.get(LinkPlanRow, evaluation.plan_id)
            if row is None:
                raise PlanNotFoundError
            status = LinkPlanStatus(row.status)
            if status in SHADOW_ONLY_STATUSES and evaluation.mode is not AutonomyMode.SHADOW:
                raise ShadowOnlyStatusError(evaluation.plan_id)
            moved = evaluation.mode is AutonomyMode.SHADOW or await self._advance(
                session, evaluation=evaluation, status=status
            )
            if moved:
                row_id = await repo.insert_decision(
                    session, evaluation=evaluation, actor=AUTONOMY_ACTOR, job_run_id=job_run_id
                )
        if row_id is None:
            # The plan is already in the status this decision puts it in, so there is no
            # transition for the deferred guard to match and nothing new to say.
            logger.info(
                "autonomy_decision_unchanged",
                plan_id=str(evaluation.plan_id),
                status=status.value,
            )
            return evaluated
        logger.info(
            "autonomy_decision_recorded",
            plan_id=str(evaluation.plan_id),
            decision=evaluation.decision.value,
            mode=evaluation.mode.value,
            risk_tier=evaluation.risk_tier.value,
            blocks=",".join(code.value for code in evaluation.blocks),
            calibrated=evaluation.calibration_version is not None,
        )
        return EvaluatedPlan(
            evaluation=evaluation,
            facts=evaluated.facts,
            identity=evaluated.identity,
            decision_row_id=row_id,
        )

    async def _advance(
        self, session: AsyncSession, *, evaluation: AutonomyEvaluation, status: LinkPlanStatus
    ) -> bool:
        """Move the plan onto ADR-0018 s3's ladder. False = it is already there.

        ``proposed`` / ``critiqued`` first become ``policy_checked``, because the
        deterministic block evaluation that just ran IS the policy check; then the decision's
        own status follows in the same transaction, which is the transition the deferred
        guard matches against the decision row.
        """
        target = target_status_for(
            decision=evaluation.decision, mode=evaluation.mode, blocks=evaluation.blocks
        )
        if target is None or status is target:
            return False
        note = self._note(evaluation)
        if target is _S.AUTONOMY_DECIDED and status in _TO_POLICY_CHECKED:
            await transition(
                session,
                plan_id=evaluation.plan_id,
                to_status=_S.POLICY_CHECKED,
                actor=AUTONOMY_ACTOR,
                note=(
                    "policy checked by the autonomy engine "
                    f"(autonomy policy v{evaluation.policy_version})"
                ),
                expected_from=status,
                clock=self._clock,
            )
            status = _S.POLICY_CHECKED
        await transition(
            session,
            plan_id=evaluation.plan_id,
            to_status=target,
            actor=AUTONOMY_ACTOR,
            note=note,
            expected_from=status,
            clock=self._clock,
        )
        return True

    def _note(self, evaluation: AutonomyEvaluation) -> str:
        """The sentence written on the plan's transition row, in the same transaction as the
        decision row (which has no column for it). A block or a deferral carries its reasons
        in words, so the investigation view never shows "blocked" with an empty list."""
        if evaluation.decision is AutonomyDecision.BLOCK:
            return block_note(evaluation, self._autonomy_policy)
        return (
            f"autonomy {evaluation.decision.value} [policy v{self._autonomy_policy.version}, "
            f"mode {evaluation.mode.value}, risk {evaluation.risk_tier.value}]"
        )[:MAX_NOTE_CHARS]

    # --------------------------------------------------------------- sweeps
    async def evaluate_source(
        self, *, source_page_id: uuid.UUID, mode: AutonomyMode | None = None
    ) -> list[AutonomyEvaluation]:
        """Every decidable plan of one source, serialised per source like the review service."""
        plan_ids = await self.decidable_plans(source_page_ids=[source_page_id])
        out: list[AutonomyEvaluation] = []
        for plan_id in plan_ids:
            try:
                evaluated = await self.evaluated(plan_id=plan_id, mode=mode)
            except CriticDeclinedError:
                continue
            out.append((await self.record(evaluated)).evaluation)
        return out

    async def decidable_plans(
        self,
        *,
        source_page_ids: Sequence[uuid.UUID] | None = None,
        include_shadow_only: bool = False,
    ) -> list[uuid.UUID]:
        """Plan ids a pass should evaluate, oldest first (a stable, reproducible order).

        ``include_shadow_only`` adds :data:`SHADOW_ONLY_STATUSES`; callers pass it only for a
        SHADOW pass."""
        statuses = DECIDABLE_STATUSES + (SHADOW_ONLY_STATUSES if include_shadow_only else ())
        async with self._sessionmaker() as session:
            statement = (
                select(LinkPlanRow.id)
                .where(LinkPlanRow.status.in_([status.value for status in statuses]))
                .order_by(LinkPlanRow.created_at, LinkPlanRow.id)
            )
            if source_page_ids:
                statement = statement.where(LinkPlanRow.source_page_id.in_(list(source_page_ids)))
            ids = list((await session.scalars(statement)).all())
            await session.rollback()
        return ids
