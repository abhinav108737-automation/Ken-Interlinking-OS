"""Confidence calibration: :class:`EvaluationCalibrationReader` (directive s20, Phase 9).

A raw score from the ranking engine is NOT a probability, and ADR-0011 forbids any threshold
from treating it as one. This module is what turns a plan's raw confidence into a calibrated
one, and -- far more importantly -- what makes the absence of a calibration visible instead of
invisible.

**Where the numbers come from.** Phase 8 already records them: every ``policy_evaluations``
row carries ``metrics.by_decision_class``, keyed by the canonical
:class:`~ken_contracts.learning.DecisionClass` (relationship type x anchor source x channel
set), with ``sample_count``, ``sufficient``, ``calibrated_confidence``, ``rollback_rate`` and
``positive_outcome_rate``. Phase 9 reads that; it does not invent a second store. Rows whose
``gate_result`` failed are ignored, and so are rows older than the learning policy's
``promotion_max_eval_age_days``: a calibration nobody could promote is not one a publisher
may lean on.

**Degrading honestly.** When a plan's decision class has no row, or the row says
``sufficient=False``, or ``calibrated_confidence`` is absent, the reader returns the RAW
confidence with ``calibration_version=None``. That is what the whole chain keys off:
``guarded`` mode refuses to auto-publish on a ``None`` version, and the risk classifier
raises the tier. An uncalibrated class therefore gets the conservative outcome, never the
permissive one.

**Calibration may only LOWER.** The returned value is ``min(raw, calibrated)``, and
behavioural evidence can lower it again. A calibration that is itself wrong can therefore
only make the system publish LESS, never more -- which matters because the calibration is
computed from data the system produced about itself, and because ``positive_outcome_rate`` is
behavioural and first-party counts are spoofable (ADR-0015 s12): it may block and may never
pass.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.intelligence import RelationshipType
from ken_contracts.learning import (
    DecisionClass,
    DecisionClassMetrics,
    GateResult,
    LearningPolicy,
)
from ken_contracts.recommendations import RetrievalChannel
from ken_core.db.models.learning import PolicyEvaluation
from ken_services.autonomy.facts import PlanIdentity
from ken_services.autonomy.protocol import CalibratedConfidence, PlanFacts

__all__ = [
    "CALIBRATION_RECIPE",
    "ClassCalibration",
    "EvaluationCalibrationReader",
    "blend_confidence",
    "calibration_version_of",
    "decision_class_of",
]

CALIBRATION_RECIPE: Final = "autonomy-calib-1"
"""The recipe, not the data. The stored version is ``<recipe>:<evaluation id>``, so a
decision is attributable to the exact evaluation run that calibrated it."""

_ANCHOR_SOURCES: Final[frozenset[str]] = frozenset(
    {"target_topic", "target_title", "target_keyword", "edited"}
)
"""``ken_contracts.learning.AnchorSource`` is a ``Literal`` alias, not an enum, so the
membership test is spelled out here rather than faked with a constructor call."""


def decision_class_of(identity: PlanIdentity) -> DecisionClass | None:
    """The plan's canonical decision class, or ``None`` when it cannot be formed.

    ``None`` is not an error: a plan with no recognised retrieval channel has no class, so it
    has no calibration, so it gets the conservative treatment. Returning ``None`` rather than
    guessing a class is the point -- a guessed class would borrow another class's numbers.
    """
    channels: list[RetrievalChannel] = []
    for value in identity.channels:
        try:
            channels.append(RetrievalChannel(value))
        except ValueError:
            return None
    if not channels:
        return None
    if identity.anchor_source not in _ANCHOR_SOURCES:
        return None
    relationship: RelationshipType | None = None
    if identity.relationship_type is not None:
        try:
            relationship = RelationshipType(identity.relationship_type)
        except ValueError:
            return None
    try:
        return DecisionClass(
            relationship_type=relationship,
            anchor_source=identity.anchor_source,
            channels=tuple(channels),
        )
    except ValueError:
        return None


def calibration_version_of(evaluation_id: object) -> str:
    return f"{CALIBRATION_RECIPE}:{evaluation_id}"


@dataclass(frozen=True, slots=True)
class ClassCalibration:
    """What one evaluation run says about one decision class."""

    version: str
    metrics: DecisionClassMetrics


def blend_confidence(
    *, raw: float, metrics: DecisionClassMetrics, min_positive_outcome_rate: float
) -> float:
    """What a class's recorded metrics do to a RAW confidence. Pure; LOWERS only.

    The calibrated value never exceeds the raw score, a measured rollback rate caps it at
    ``1 - rate``, and a measured positive-outcome rate under ``min_positive_outcome_rate``
    caps it at that rate. A generous calibration therefore confirms a confident plan and can
    never lift an unconfident one, and a wrong calibration costs deferrals, never publishes.
    A high positive-outcome rate changes nothing: it is behavioural, and first-party counts
    are spoofable (ADR-0015 s12).
    """
    value = min(raw, metrics.calibrated_confidence or 0.0)
    if metrics.rollback_rate is not None:
        value = min(value, 1.0 - metrics.rollback_rate)
    if (
        metrics.positive_outcome_rate is not None
        and metrics.positive_outcome_rate < min_positive_outcome_rate
    ):
        value = min(value, metrics.positive_outcome_rate)
    return max(0.0, min(1.0, value))


class EvaluationCalibrationReader:
    """:class:`~ken_services.autonomy.protocol.CalibrationReader` over ``policy_evaluations``.

    Takes no model provider: a calibration is arithmetic over recorded outcomes.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        policy: LearningPolicy | None,
        clock: Callable[[], datetime],
        min_positive_outcome_rate: float,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._policy = policy
        self._clock = clock
        # From the versioned autonomy policy (confidence.min_positive_outcome_rate).
        self._min_positive_outcome_rate = min_positive_outcome_rate

    def _max_age(self, policy: LearningPolicy) -> timedelta:
        return timedelta(days=policy.gates.promotion_max_eval_age_days)

    async def lookup(self, *, decision_class: DecisionClass) -> ClassCalibration | None:
        """The newest usable calibration for one class, or ``None``.

        Usable means: the run PASSED its gate, it finished inside the policy's maximum
        evaluation age, it carries a row for this class, the row is ``sufficient`` AND meets
        the policy's ``min_sample_count``, and it has a ``calibrated_confidence``. Any one of
        those missing means no calibration, not a weaker one.
        """
        key = decision_class.canonical
        policy = self._policy
        if policy is None:
            # No learning policy means no sample floor and no maximum evaluation age to hold a
            # calibration to. The honest answer is NO calibration -- not a calibration against
            # invented defaults, which would be more permissive than the real floor.
            return None
        cutoff = self._clock() - self._max_age(policy)
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(PolicyEvaluation.id, PolicyEvaluation.metrics)
                    .where(
                        PolicyEvaluation.gate_result == GateResult.PASS.value,
                        PolicyEvaluation.finished_at.is_not(None),
                        PolicyEvaluation.finished_at >= cutoff,
                        PolicyEvaluation.metrics["by_decision_class"].has_key(key),
                    )
                    .order_by(PolicyEvaluation.finished_at.desc(), PolicyEvaluation.id.desc())
                    .limit(1)
                )
            ).first()
            await session.rollback()
        if rows is None:
            return None
        raw = (rows.metrics or {}).get("by_decision_class", {}).get(key)
        if not isinstance(raw, dict):
            return None
        try:
            metrics = DecisionClassMetrics.model_validate(raw)
        except ValueError:
            return None  # a malformed row calibrates nothing
        if not metrics.sufficient or metrics.sample_count < policy.gates.min_sample_count:
            return None
        if metrics.calibrated_confidence is None:
            return None
        return ClassCalibration(version=calibration_version_of(rows.id), metrics=metrics)

    async def calibrate(self, *, facts: PlanFacts) -> CalibratedConfidence:
        """The protocol entry point. Without a :class:`PlanIdentity` the class cannot be
        formed, so this reads the plan itself; :meth:`calibrate_for` is the cheaper call when
        the caller already has the identity."""
        from ken_services.autonomy.facts import plan_identity

        async with self._sessionmaker() as session:
            identity = await plan_identity(session, plan_id=facts.plan_id)
            await session.rollback()
        return await self.calibrate_for(facts=facts, identity=identity)

    async def calibrate_for(
        self, *, facts: PlanFacts, identity: PlanIdentity
    ) -> CalibratedConfidence:
        decision_class = decision_class_of(identity)
        if decision_class is None:
            return CalibratedConfidence(value=facts.confidence, calibration_version=None)
        found = await self.lookup(decision_class=decision_class)
        if found is None:
            return CalibratedConfidence(value=facts.confidence, calibration_version=None)
        value = blend_confidence(
            raw=facts.confidence,
            metrics=found.metrics,
            min_positive_outcome_rate=self._min_positive_outcome_rate,
        )
        return CalibratedConfidence(value=value, calibration_version=found.version)
