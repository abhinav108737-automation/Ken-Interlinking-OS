"""Deterministic risk tiering: :class:`DeterministicRiskClassifier` (directive s20).

A tier GATES publishing in ``guarded`` mode, so it is computed from stored facts by code --
page type, commercial importance, inbound traffic, protection proximity and anchor
sensitivity -- and never by a model. It is reproducible, auditable and recorded on every
``autonomy_decisions`` row.

**The tier only ever rises.** Every rule below can move a plan from ``low`` to ``medium`` or
to ``high``; none can lower it. That asymmetry is the whole point: a signal that is wrong in
the cautious direction costs a deferral, and a signal that is wrong in the permissive
direction publishes a link nobody vetted.

**Behavioural analytics may only block, never justify** (ADR-0015 s12, ADR-0016 16.7):
first-party counts are spoofable, so a measured rollback rate or a negative outcome rate can
RAISE a tier and nothing in here can lower one. A page with excellent measured engagement is
treated exactly like a page with none.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.autonomy import RiskPolicy, RiskTier
from ken_contracts.intelligence import TaxonomyPolicy
from ken_core.db.models.analytics import LinkMeasurementRow
from ken_core.db.models.autonomy import PublishAttempt
from ken_core.db.models.intelligence import PageProtection
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow
from ken_core.db.repositories import recommendations as rec_repo
from ken_services.autonomy.protocol import CalibratedConfidence, PlanFacts

__all__ = [
    "RISK_ORDER",
    "DeterministicRiskClassifier",
    "RiskSignals",
    "tier_from_signals",
]

RISK_ORDER: Final[tuple[RiskTier, ...]] = (RiskTier.LOW, RiskTier.MEDIUM, RiskTier.HIGH)


@dataclass(frozen=True, slots=True)
class RiskSignals:
    """The stored signals a tier is computed from. All deterministic, all raise-only."""

    commercial_page: bool
    """Either side is a commercial page type."""
    hub_destination: bool
    """The destination already has many inbound internal links."""
    protection_nearby: bool
    """A protection exists on the source or destination page -- active or NOT.

    An inactive protection means a human has at some point declared this page special. The
    deterministic block only fires for an ACTIVE one; a lapsed one still raises the tier,
    because "someone protected this once" is exactly the kind of context a fully autonomous
    publisher should treat as unusual."""
    sensitive_anchor: bool
    """A long anchor, or one a reviewer edited by hand (editorial wording is not ours to
    re-place autonomously without extra care)."""
    prior_failure: bool
    """This (source, target) pair has a recorded FAILED publish attempt."""
    measured_rollback: bool
    """A measurement flagged this plan's pair as rolled back inside a window (BEHAVIOURAL:
    raises only)."""
    stale_target_version: bool
    """The plan points at a target version that is no longer current."""
    uncalibrated: bool
    """No calibration applies to this plan's decision class: raw confidence only."""


def tier_from_signals(signals: RiskSignals) -> RiskTier:
    """``low`` unless a signal says otherwise. Monotone: signals only ever raise the tier."""
    tier = RiskTier.LOW
    if signals.hub_destination or signals.sensitive_anchor or signals.stale_target_version:
        tier = RiskTier.MEDIUM
    if signals.uncalibrated and tier is RiskTier.LOW:
        tier = RiskTier.MEDIUM
    if (
        signals.commercial_page
        or signals.protection_nearby
        or signals.prior_failure
        or signals.measured_rollback
    ):
        tier = RiskTier.HIGH
    return tier


def raise_to(current: RiskTier, floor: RiskTier) -> RiskTier:
    """``max`` over :data:`RISK_ORDER`. The ONLY way anything changes a tier."""
    return RISK_ORDER[max(RISK_ORDER.index(current), RISK_ORDER.index(floor))]


class DeterministicRiskClassifier:
    """:class:`~ken_services.autonomy.protocol.RiskClassifier` over PostgreSQL.

    No model provider, by construction: the constructor takes none and ``classify`` only
    reads rows.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        taxonomy: TaxonomyPolicy,
        clock: Callable[[], datetime],
        policy: RiskPolicy,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._taxonomy = taxonomy
        self._clock = clock
        # Every threshold below comes from the versioned autonomy policy
        # (policies/autonomy/autonomy.v<N>.yaml); none is a constant in this module.
        self._policy = policy
        self._commercial = frozenset(policy.commercial_page_types)

    async def signals(
        self, *, facts: PlanFacts, calibrated: CalibratedConfidence | None = None
    ) -> RiskSignals:
        async with self._sessionmaker() as session:
            found = await self._signals(session, facts=facts)
            await session.rollback()
        if calibrated is not None:
            # The CALIBRATION READER's answer, not the plan's stored field: a Phase 3 plan
            # always carries ``calibration_version = NULL`` (raw), so using that here would
            # make every class look uncalibrated and `guarded` mode could never publish
            # anything at all.
            return replace(found, uncalibrated=not calibrated.is_calibrated)
        return found

    async def _signals(self, session: AsyncSession, *, facts: PlanFacts) -> RiskSignals:
        plan = await session.get(LinkPlanRow, facts.plan_id)
        counts = await rec_repo.inbound_counts_for(session, page_ids=[facts.target_page_id])
        inbound_links = int(counts.get(facts.target_page_id, 0))
        protection_ever = bool(
            await session.scalar(
                select(func.count())
                .select_from(PageProtection)
                .where(PageProtection.page_id.in_([facts.source_page_id, facts.target_page_id]))
            )
        )
        prior_failures = int(
            await session.scalar(
                select(func.count())
                .select_from(PublishAttempt)
                .where(
                    PublishAttempt.source_page_id == facts.source_page_id,
                    PublishAttempt.target_page_id == facts.target_page_id,
                    PublishAttempt.succeeded.is_(False),
                )
            )
            or 0
        )
        measured_rollback = bool(
            await session.scalar(
                select(func.count())
                .select_from(LinkMeasurementRow)
                .where(
                    LinkMeasurementRow.plan_id == facts.plan_id,
                    LinkMeasurementRow.flags.contains(["rolled_back_in_window"]),
                )
            )
        )
        anchor = "" if plan is None else plan.anchor_text
        edited = plan is not None and plan.anchor_source == "edited"
        return RiskSignals(
            commercial_page=(facts.source_page_type in self._commercial)
            or (facts.target_page_type in self._commercial),
            hub_destination=inbound_links >= self._policy.hub_inbound_links,
            protection_nearby=protection_ever,
            sensitive_anchor=len(anchor) > self._policy.sensitive_anchor_chars or edited,
            prior_failure=prior_failures >= self._policy.failed_attempts_for_high,
            measured_rollback=measured_rollback,
            stale_target_version=facts.target_page_version != facts.target_current_version,
            uncalibrated=facts.calibration_version is None,
        )

    async def classify(self, *, facts: PlanFacts) -> RiskTier:
        """The frozen protocol entry point: the plan's own stored calibration field."""
        return tier_from_signals(await self.signals(facts=facts))

    async def classify_with(
        self, *, facts: PlanFacts, calibrated: CalibratedConfidence
    ) -> RiskTier:
        """:meth:`classify` with the calibration the reader actually found for this plan's
        decision class. This is what the engine calls."""
        return tier_from_signals(await self.signals(facts=facts, calibrated=calibrated))
