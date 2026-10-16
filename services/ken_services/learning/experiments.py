"""Experiments: the real guard, the lifecycle and derived attribution (ADR-0016 15.5, 16.6, 17.2).

**The guard.** :class:`SqlExperimentGuard` replaces ``NoExperiments``. ``locked()`` is true iff
the TARGET page has a ``control`` assignment in an ACTIVE ``holdout`` (protocol.py states why:
a holdout's arms are assigned by target page, so a control page must receive no inbound link
from ANY source). A ``policy_ab`` locks nobody. It is backed by one cached query, never
per-pair SQL; a failed refresh keeps the previous map and never raises, and a guard that has
NEVER managed to load fails CLOSED (it locks), because the alternative is silently adding links
to a page an operator promised to withhold them from.

**Holdouts are built and gated three ways** (17.2): ``treatment_fraction <= 0.20`` (a DB CHECK
and refused here first), ``Settings.experiments_allow_holdout`` (default ``False``), and the
transaction-local ``ken.learning_experiment_activator`` setting, which this module sets in the
SAME transaction as the ``draft -> active`` UPDATE and only after the setting check -- so a raw
``UPDATE experiments SET status = 'active'`` fails in the database as well.

**Assignment is deterministic AND materialised**: ``protocol.experiment_arm`` computes the arm
and the row is written, so it stays auditable if the hash ever changes. The actor on an
assignment is the learning SYSTEM actor: the arm is a computation, not a judgement. The human act
is ``experiments.created_by`` and the activation.

**Attribution is DERIVED** (assignment x ``[started_at, ended_at)`` x the plan's live window
from ``link_plan_transitions``). Nothing here adds a column to ``link_events``.

**Claims.** :func:`compare_arms` reports observed differences between arms with their sample
counts and confounder flags. It computes no significance, no causal lift, nothing about revenue
or ranking causation, and imports no statistics library.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import Integer, cast, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.analytics import PAGE_SOURCES, ConfounderFlag
from ken_contracts.auth import Permission, Principal, has_permission
from ken_contracts.learning import (
    EXPERIMENT_ACTIVATOR_SETTING,
    EXPERIMENT_KEY_PATTERN,
    EXPERIMENT_TRANSITIONS,
    MAX_HOLDOUT_TREATMENT_FRACTION,
    ExperimentArm,
    ExperimentKind,
    ExperimentNotFoundError,
    ExperimentRefusal,
    ExperimentRefusedError,
    ExperimentStatus,
    ExperimentUnit,
    LearningPolicy,
    PromotionRefusal,
    PromotionRefusedError,
    experiment_unit,
)
from ken_contracts.recommendations import actor_class
from ken_core.config import Settings
from ken_core.db.models.analytics import PageMetricsDaily
from ken_core.db.models.learning import Experiment
from ken_core.db.repositories import analytics as analytics_repo
from ken_core.db.repositories import learning as repo
from ken_core.logging import get_logger
from ken_services.jobs.runner import utc_now
from ken_services.learning.policy import load_configured_learning_policy
from ken_services.learning.protocol import experiment_arm

__all__ = [
    "ArmComparison",
    "ArmObservation",
    "ExperimentLifecycleService",
    "SqlExperimentGuard",
    "assignment_arm",
    "attribute",
    "compare_arms",
]

logger = get_logger(__name__)

GUARD_TTL_SECONDS: Final = 30.0
OBSERVED_NOT_CAUSAL: Final = (
    "Observed difference between arms over the interval, with the sample counts and confounder "
    "flags shown. This is an observed change, not a causal effect."
)
_KEY: Final = EXPERIMENT_KEY_PATTERN


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #
class SqlExperimentGuard:
    """The real :class:`~ken_contracts.recommendations.ExperimentGuard` (structural)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        ttl_seconds: float = GUARD_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._ttl = ttl_seconds
        self._monotonic = monotonic
        self._controls: frozenset[uuid.UUID] | None = None
        self._loaded_at = 0.0

    async def refresh(self) -> None:
        """Rebuild the map. A failure keeps the previous map and never raises."""
        try:
            async with self._sessionmaker() as session:
                controls = await repo.live_holdout_control_targets(session)
                await session.rollback()
        except Exception as exc:
            logger.warning("experiment_guard_refresh_failed", error_class=type(exc).__name__)
            return
        self._controls = controls
        self._loaded_at = self._monotonic()

    async def locked(self, *, source_page_id: uuid.UUID, target_page_id: uuid.UUID) -> bool:
        del source_page_id  # a later kind may withhold OUTBOUND links; today only the target counts
        if self._controls is None or self._monotonic() - self._loaded_at > self._ttl:
            await self.refresh()
        if self._controls is None:
            return True  # never loaded: fail closed
        return target_page_id in self._controls


# --------------------------------------------------------------------------- #
# Arm assignment
# --------------------------------------------------------------------------- #
def assignment_arm(
    *, kind: ExperimentKind, key: str, page_id: uuid.UUID, treatment_fraction: float
) -> ExperimentArm:
    """The arm to MATERIALISE for one page: the frozen ``experiment_arm`` hash, oriented so the
    capped quantity is the one that is actually withheld.

    For a ``policy_ab`` the fraction is the share in the TREATMENT arm (the candidate ranking),
    exactly as ``experiment_arm`` documents. For a ``holdout`` the contract says the fraction
    is capped at 0.20 BECAUSE it is "the share of pages a holdout may withhold links from"
    (``ExperimentKind`` / ``ExperimentPolicy``), while ``SqlExperimentGuard`` withholds the
    CONTROL arm. Taken literally, ``experiment_arm(treatment_fraction=0.10)`` would put 90 % of
    pages in control -- and the guard would lock 90 % of the site against the 20 % cap. So a
    holdout hashes against ``1 - fraction``: the CONTROL (withheld) arm is ``fraction`` of the
    pages and the cap bounds what it was written to bound. The hash itself is still
    ``experiment_arm``'s; the stored arm is what is audited.
    """
    effective = 1.0 - treatment_fraction if kind is ExperimentKind.HOLDOUT else treatment_fraction
    return experiment_arm(key=key, page_id=page_id, treatment_fraction=effective)


# --------------------------------------------------------------------------- #
# The lifecycle
# --------------------------------------------------------------------------- #
def _refuse(refusal: ExperimentRefusal) -> ExperimentRefusedError:
    return ExperimentRefusedError(refusal)


class ExperimentLifecycleService:
    """:class:`~ken_services.learning.protocol.ExperimentLifecycle` over PostgreSQL."""

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

    @staticmethod
    def _require_operator(principal: Principal) -> None:
        if not has_permission(principal.role, Permission.PROMOTE):
            raise PromotionRefusedError(PromotionRefusal.PERMISSION_DENIED)
        if actor_class(principal.actor) != "user":
            raise PromotionRefusedError(PromotionRefusal.NOT_A_HUMAN)

    async def create(
        self,
        *,
        principal: Principal,
        key: str,
        description: str,
        kind: ExperimentKind,
        candidate_id: uuid.UUID | None = None,
        proposal_id: uuid.UUID | None = None,
        treatment_fraction: float | None = None,
    ) -> uuid.UUID:
        """Create a DRAFT experiment. Drafts are harmless; activation is what is gated."""
        self._require_operator(principal)
        policy = self._learning_policy()
        fraction = treatment_fraction
        if fraction is None:
            fraction = (
                policy.experiments.holdout_treatment_fraction
                if kind is ExperimentKind.HOLDOUT
                else policy.experiments.policy_ab_treatment_fraction
            )
        if kind is ExperimentKind.HOLDOUT and fraction > MAX_HOLDOUT_TREATMENT_FRACTION:
            raise _refuse(ExperimentRefusal.FRACTION_TOO_LARGE)
        experiment_id = uuid.uuid4()
        async with self._sessionmaker() as session, session.begin():
            await repo.insert_experiment(
                session,
                experiment_id=experiment_id,
                key=key,
                description=description,
                kind=kind,
                unit=experiment_unit(kind).value,
                candidate_id=candidate_id,
                proposal_id=proposal_id,
                treatment_fraction=fraction,
                created_by=principal.actor,
                now=self._clock(),
            )
        return experiment_id

    async def assign(
        self, *, key: str, page_ids: Sequence[uuid.UUID] | None = None, actor: str
    ) -> Mapping[ExperimentArm, int]:
        if actor_class(actor) != "system":
            raise ValueError("an assignment is a computation and carries a system actor")
        now = self._clock()
        async with self._sessionmaker() as session, session.begin():
            experiment = await repo.lock_experiment(session, key)
            if experiment is None:
                raise ExperimentNotFoundError
            if experiment.status != ExperimentStatus.DRAFT.value:
                raise _refuse(ExperimentRefusal.NOT_A_DRAFT)
            existing = {row.page_id for row in await repo.assignments_of(session, experiment.id)}
            population = (
                list(page_ids) if page_ids is not None else await repo.page_ids_for_unit(session)
            )
            fresh = [
                repo.AssignmentRow(
                    page_id=page_id,
                    arm=assignment_arm(
                        kind=ExperimentKind(experiment.kind),
                        key=experiment.key,
                        page_id=page_id,
                        treatment_fraction=experiment.treatment_fraction,
                    ),
                    assigned_by=actor,
                    assigned_at=now,
                )
                for page_id in dict.fromkeys(population)
                if page_id not in existing
            ]
            try:
                async with session.begin_nested():
                    await repo.insert_assignments(session, experiment_id=experiment.id, rows=fresh)
            except DBAPIError as exc:
                if "already assigned to a live experiment" in str(exc):
                    raise _refuse(ExperimentRefusal.PAGE_ALREADY_IN_A_LIVE_EXPERIMENT) from None
                raise
            return await repo.assignment_arm_counts(session, experiment.id)

    async def activate(self, *, principal: Principal, key: str) -> None:
        self._require_operator(principal)
        policy = self._learning_policy()
        now = self._clock()
        async with self._sessionmaker() as session, session.begin():
            experiment = await repo.lock_experiment(session, key)
            if experiment is None:
                raise ExperimentNotFoundError
            holdout = experiment.kind == ExperimentKind.HOLDOUT.value
            # Gate 2: the setting. Checked FIRST so the default refusal is unmistakable.
            if holdout and not self._settings.experiments_allow_holdout:
                raise _refuse(ExperimentRefusal.HOLDOUT_NOT_ENABLED)
            # Gate 1 again, in Python, before the database CHECK has to say it.
            if holdout and experiment.treatment_fraction > MAX_HOLDOUT_TREATMENT_FRACTION:
                raise _refuse(ExperimentRefusal.FRACTION_TOO_LARGE)
            if experiment.status != ExperimentStatus.DRAFT.value:
                raise _refuse(ExperimentRefusal.NOT_A_DRAFT)
            counts = await repo.assignment_arm_counts(session, experiment.id)
            if sum(counts.values()) == 0:
                raise _refuse(ExperimentRefusal.NO_ASSIGNMENTS)
            if min(counts.values()) < policy.experiments.min_pages_per_arm:
                raise _refuse(ExperimentRefusal.NO_ASSIGNMENTS)
            if ExperimentStatus.ACTIVE not in EXPERIMENT_TRANSITIONS[ExperimentStatus.DRAFT]:
                raise _refuse(ExperimentRefusal.TRANSITION_NOT_ALLOWED)  # pragma: no cover
            if holdout:
                # Gate 3: the transaction-local setting the database guard requires.
                await session.execute(
                    text("SELECT set_config(:name, 'on', true)"),
                    {"name": EXPERIMENT_ACTIVATOR_SETTING},
                )
            await repo.set_experiment_status(
                session, experiment_id=experiment.id, status=ExperimentStatus.ACTIVE, now=now
            )
        logger.info("experiment_activated", key=key, kind=experiment.kind)

    async def end(self, *, principal: Principal, key: str) -> None:
        self._require_operator(principal)
        now = self._clock()
        async with self._sessionmaker() as session, session.begin():
            experiment = await repo.lock_experiment(session, key)
            if experiment is None:
                raise ExperimentNotFoundError
            current = ExperimentStatus(experiment.status)
            if ExperimentStatus.ENDED not in EXPERIMENT_TRANSITIONS[current]:
                raise _refuse(ExperimentRefusal.TRANSITION_NOT_ALLOWED)
            await repo.set_experiment_status(
                session, experiment_id=experiment.id, status=ExperimentStatus.ENDED, now=now
            )


# --------------------------------------------------------------------------- #
# Derived attribution and the observed comparison
# --------------------------------------------------------------------------- #
async def _experiment(session: AsyncSession, experiment_id: uuid.UUID) -> Experiment:
    row = await repo.experiment_by_id(session, experiment_id)
    if row is None:
        raise ExperimentNotFoundError
    return row


async def attribute(
    session: AsyncSession, *, experiment_id: uuid.UUID, now: datetime | None = None
) -> Mapping[ExperimentArm, tuple[uuid.UUID, ...]]:
    """Plan ids attributable to each arm: arm membership x the experiment's interval x the
    plan's live window. A draft experiment has no interval and attributes nothing."""
    experiment = await _experiment(session, experiment_id)
    result: dict[ExperimentArm, tuple[uuid.UUID, ...]] = dict.fromkeys(ExperimentArm, ())
    if experiment.started_at is None:
        return result
    start = experiment.started_at
    end = experiment.ended_at or now or datetime.now(UTC)
    role = (
        "source"
        if experiment_unit(ExperimentKind(experiment.kind)) is (ExperimentUnit.SOURCE_PAGE)
        else "target"
    )
    assignments = await repo.assignments_of(session, experiment.id)
    for arm in ExperimentArm:
        pages = [row.page_id for row in assignments if row.arm is arm]
        plan_ids = await repo.plan_ids_of_pages(session, page_ids=pages, role=role)
        windows = await analytics_repo.live_transition_moments(session, plan_ids)
        keep = [
            window.plan_id
            for window in windows
            if window.deployed_at is not None
            and window.deployed_at < end
            and (window.rolled_back_at is None or window.rolled_back_at > start)
        ]
        result[arm] = tuple(sorted(keep, key=str))
    return result


@dataclass(frozen=True, slots=True)
class ArmObservation:
    """What was OBSERVED for one arm. Counts and sums only."""

    arm: ExperimentArm
    pages: int
    attributable_plans: int
    clicks: int = 0
    impressions: int = 0
    pageviews: int = 0
    covered_page_days: int = 0
    mean_clicks_per_page: float | None = None
    mean_pageviews_per_page: float | None = None


@dataclass(frozen=True, slots=True)
class ArmComparison:
    """An observed difference between arms. There is deliberately NO significance field, no
    p-value, no interval estimate, no lift and no revenue figure: ADR-0016 15.5 permits the
    claim "observed difference between arms over [interval], N_control/N_treatment pages, with
    these confounder flags" and nothing stronger."""

    key: str
    kind: ExperimentKind
    interval_start: datetime | None
    interval_end: datetime | None
    control: ArmObservation
    treatment: ArmObservation
    observed_clicks_per_page_difference: float | None
    observed_pageviews_per_page_difference: float | None
    confounder_flags: tuple[ConfounderFlag, ...] = field(default=())
    note: str = OBSERVED_NOT_CAUSAL


async def compare_arms(
    session: AsyncSession,
    *,
    experiment_id: uuid.UUID,
    min_pages_per_arm: int = 1,
    now: datetime | None = None,
) -> ArmComparison:
    """Observed per-page metric differences between arms (``treatment - control``).

    A ``holdout`` compares the TARGET pages' search and pageview totals over the interval; a
    ``policy_ab`` reports arm sizes and attributable plans (its primary metric is editorial and
    is read from the review tables elsewhere, ADR-0016 section 6). Behavioural data is
    advisory: flags are attached rather than hidden.
    """
    experiment = await _experiment(session, experiment_id)
    attributed = await attribute(session, experiment_id=experiment_id, now=now)
    assignments = await repo.assignments_of(session, experiment.id)
    pages = {arm: [a.page_id for a in assignments if a.arm is arm] for arm in ExperimentArm}
    flags: set[ConfounderFlag] = set()
    totals: dict[ExperimentArm, tuple[int, int, int, int]] = dict.fromkeys(
        ExperimentArm, (0, 0, 0, 0)
    )
    start, end = experiment.started_at, experiment.ended_at or now or datetime.now(UTC)
    if experiment.kind == ExperimentKind.HOLDOUT.value and start is not None:
        first, last = start.date(), end.date()
        for arm in ExperimentArm:
            if not pages[arm]:
                continue
            row = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(PageMetricsDaily.clicks), 0),
                        func.coalesce(func.sum(PageMetricsDaily.impressions), 0),
                        func.coalesce(func.sum(PageMetricsDaily.pageviews), 0),
                        func.count(),
                        func.coalesce(func.sum(cast(PageMetricsDaily.synthetic, Integer)), 0),
                    ).where(
                        PageMetricsDaily.page_id.in_(pages[arm]),
                        PageMetricsDaily.metric_date >= first,
                        PageMetricsDaily.metric_date <= last,
                        PageMetricsDaily.source.in_([s.value for s in PAGE_SOURCES]),
                    )
                )
            ).one()
            totals[arm] = (int(row[0]), int(row[1]), int(row[2]), int(row[3]))
            if int(row[4]) > 0:
                flags.add(ConfounderFlag.SYNTHETIC_DATA)
            if int(row[3]) == 0:
                flags.add(ConfounderFlag.INSUFFICIENT_DATA)
        # A plan pointing AT a control page during the interval contaminates the control arm.
        if attributed[ExperimentArm.CONTROL]:
            flags.add(ConfounderFlag.OVERLAPPING_CHANGES)
    for arm in ExperimentArm:
        if len(pages[arm]) < min_pages_per_arm:
            flags.add(ConfounderFlag.INSUFFICIENT_DATA)
    if start is None or experiment.ended_at is None:
        flags.add(ConfounderFlag.POST_WINDOW_TRUNCATED)

    def observation(arm: ExperimentArm) -> ArmObservation:
        clicks, impressions, pageviews, days = totals[arm]
        n = len(pages[arm])
        return ArmObservation(
            arm=arm,
            pages=n,
            attributable_plans=len(attributed[arm]),
            clicks=clicks,
            impressions=impressions,
            pageviews=pageviews,
            covered_page_days=days,
            mean_clicks_per_page=None if n == 0 else clicks / n,
            mean_pageviews_per_page=None if n == 0 else pageviews / n,
        )

    control, treatment = observation(ExperimentArm.CONTROL), observation(ExperimentArm.TREATMENT)

    def difference(a: float | None, b: float | None) -> float | None:
        return None if a is None or b is None else round(a - b, 6)

    return ArmComparison(
        key=experiment.key,
        kind=ExperimentKind(experiment.kind),
        interval_start=start,
        interval_end=end if start is not None else None,
        control=control,
        treatment=treatment,
        observed_clicks_per_page_difference=difference(
            treatment.mean_clicks_per_page, control.mean_clicks_per_page
        ),
        observed_pageviews_per_page_difference=difference(
            treatment.mean_pageviews_per_page, control.mean_pageviews_per_page
        ),
        confounder_flags=tuple(sorted(flags, key=lambda f: f.value)),
    )
