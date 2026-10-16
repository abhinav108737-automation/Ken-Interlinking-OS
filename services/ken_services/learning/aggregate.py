"""The ONLY producer of :class:`~ken_contracts.learning.ProposalEvidence` (W1).

Implements the frozen :class:`~ken_services.learning.protocol.AggregateStreams` and
:class:`~ken_services.learning.protocol.DecisionClassOf`. **Every number a proposal carries is
computed here**, in SQL and plain Python, over the three ADR-0016 section 2 streams:

================  ==========================================================  ==============
Stream            Tables                                                      May it gate?
================  ==========================================================  ==============
Editorial         ``review_actions``, ``link_plans``, ``link_plan_transitions``  yes
Behavioural       ``link_metrics_daily``, ``link_measurements``                 BLOCK only
Search            ``page_metrics_daily`` (``search_console``)                   never
================  ==========================================================  ==============

``tests/unit/learning/test_boundaries.py`` asserts no other module in the package constructs
``ProposalEvidence``. That static rule plus this module's purity is what makes "no
model-produced number appears in evidence" a MECHANISM: the model is handed this table and
answers with an index (ADR-0016 15.2d / 15.7.7).

**The unit of observation is a link plan that a human decided inside the window.** One plan,
one sample, in exactly one decision class (its relationship type x anchor source x channel
set). Every other count -- edits, rollbacks, measured outcomes, link totals -- is computed over
that SAME plan set, which is what keeps the frozen
:class:`~ken_contracts.learning.DecisionClassObservation` validators satisfiable
(``approvals + rejections <= sample_count``) and makes the window mean one thing: *the plans
decided in it*.

Two consequences worth stating, because they are judgements and not accidents:

* **Approve and reject are mutually exclusive per plan**, decided by the LAST action in the
  window. ``approved -> rejected`` is a real transition (a reversal before deployment), so a
  plan can hold both actions; counting both would break the frozen validator and would also
  double-count one editorial decision. Edits are counted independently, because a plan that was
  edited and then approved really is both.
* **``positive_outcome_rate`` is ``positive_outcomes / sample_count``**, so it re-derives from
  the stored row alone (the ``reflection-grounding`` suite's requirement). A plan with no
  ``link_measurements`` row counts as not-positive, and the class carries
  :attr:`~ken_contracts.analytics.ConfounderFlag.INSUFFICIENT_DATA` so "measured negative" and
  "never measured" are distinguishable. It is BEHAVIOURAL, so it reaches a gate only through
  :class:`~ken_services.learning.protocol.BlockOnly` and can never pass one.

Resolved against the ADR (reported, not quietly taken):

* **Target-replacement pairs** (ADR-0016 section 3) are computed here but have **no field** in
  the frozen ``DecisionClassObservation``. They are carried on :class:`ClassAggregate` and
  logged; nothing stored or prompted depends on them, and no option is enumerated from a number
  that is not in the evidence.
* **Search data has no evidence field either**, which is the contract agreeing with "diagnostic
  only": it is summed into :class:`AggregateReport` and logged for a human, and no option, no
  prompt and no stored row reads it.
* **The default observation window is a code constant** (:data:`DEFAULT_WINDOW_DAYS`), because
  the frozen ``LearningPolicy`` carries no trailing-window field.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.agents import FallbackReason
from ken_contracts.analytics import AnalyticsSource, ConfounderFlag, MetricTotals
from ken_contracts.intelligence import RelationshipType
from ken_contracts.learning import (
    MAX_OBSERVATIONS,
    DecisionClass,
    DecisionClassObservation,
    EvidenceWindow,
    LearningPolicy,
    ProposalEvidence,
    ProposalOption,
)
from ken_contracts.recommendations import ENGINE_VERSION, RetrievalChannel
from ken_core.config import REPO_ROOT, Settings
from ken_core.logging import get_logger

__all__ = [
    "AGGREGATE_VERSION",
    "DEFAULT_WINDOW_DAYS",
    "UNKNOWN_VERSION",
    "AggregateReport",
    "ClassAggregate",
    "aggregate_report",
    "aggregate_streams",
    "build_evidence",
    "decision_class_of",
    "default_window",
    "policy_versions",
]

logger = get_logger(__name__)

AGGREGATE_VERSION: Final = "1"
"""Stamped into every ``ProposalEvidence``. Bump it when a number's DEFINITION changes, so an
old proposal's evidence is never re-read under new arithmetic."""
DEFAULT_WINDOW_DAYS: Final = 90
"""The trailing observation window when a caller gives none. A code constant on purpose: the
frozen ``LearningPolicy`` has no window field, and inventing one would be a contract change."""
UNKNOWN_VERSION: Final = "unknown"
"""``engine_version`` / ``analysis_version`` when the sample holds no plan to read them from."""

# --------------------------------------------------------------------------- #
# SQL. Every statement is parameterised; nothing is interpolated.
# --------------------------------------------------------------------------- #
_EDITORIAL_SQL: Final = text(
    """
    SELECT
        ra.plan_id                                              AS plan_id,
        p.target_page_id                                        AS target_page_id,
        p.relationship_type                                     AS relationship_type,
        p.anchor_source                                          AS anchor_source,
        p.channels                                               AS channels,
        p.engine_version                                         AS engine_version,
        p.analysis_version                                       AS analysis_version,
        (ARRAY_AGG(ra.action ORDER BY ra.created_at DESC, ra.id DESC))[1] AS final_action,
        BOOL_OR(ra.action IN ('edit_anchor', 'replace_target'))  AS edited,
        COUNT(*) FILTER (
            WHERE ra.action = 'replace_target' AND ra.target_after IS NOT NULL
        )                                                        AS replacements,
        COUNT(DISTINCT (ra.target_before, ra.target_after)) FILTER (
            WHERE ra.action = 'replace_target' AND ra.target_after IS NOT NULL
        )                                                        AS replacement_pairs,
        COALESCE(
            SUM(char_length(ra.anchor_after) - char_length(ra.anchor_before)) FILTER (
                WHERE ra.action = 'edit_anchor' AND ra.anchor_after IS NOT NULL
            ),
            0
        )                                                        AS anchor_delta
    FROM review_actions ra
    JOIN link_plans p ON p.id = ra.plan_id
    WHERE ra.created_at >= :start AND ra.created_at < :stop
    GROUP BY p.id, ra.plan_id
    ORDER BY ra.plan_id
    """
)
"""One row per plan decided in the window.

``anchor_before`` / ``anchor_after`` appear ONLY inside ``char_length`` and ``note`` is not
selected at all: an anchor is page content and a note is free text (ADR-0016 15.2e), so the
only thing that leaves the database is a character COUNT."""

_REASONS_SQL: Final = text(
    """
    SELECT ra.plan_id AS plan_id, r.value AS reason, COUNT(*) AS occurrences
    FROM review_actions ra
    CROSS JOIN LATERAL jsonb_array_elements_text(ra.reasons) AS r(value)
    WHERE ra.created_at >= :start AND ra.created_at < :stop
    GROUP BY ra.plan_id, r.value
    """
)
"""Structured ``FeedbackReason`` CODES only (``REASONS_BY_ACTION`` constrains them at the
review boundary), which is why a histogram may be rendered into a prompt."""

_ROLLBACK_SQL: Final = text(
    """
    SELECT DISTINCT t.plan_id AS plan_id
    FROM link_plan_transitions t
    WHERE t.to_status = 'rolled_back'
      AND t.occurred_at < :stop
      AND t.plan_id IN :plans
    """
).bindparams(bindparam("plans", expanding=True))

_MEASUREMENT_SQL: Final = text(
    """
    SELECT DISTINCT ON (m.plan_id)
        m.plan_id AS plan_id,
        m.flags   AS flags,
        (
            SELECT BOOL_OR((c ->> 'delta_per_day')::double precision >= 0)
            FROM jsonb_array_elements(m.target_comparison) AS c
            WHERE c ->> 'delta_per_day' IS NOT NULL
        )         AS positive
    FROM link_measurements m
    WHERE m.measured_at < :stop AND m.plan_id IN :plans
    ORDER BY m.plan_id, m.measured_at DESC
    """
).bindparams(bindparam("plans", expanding=True))
"""``link_measurements`` -- not live metrics -- because ADR-0015 section 12.6.7 wrote that row
precisely so a past measurement stays reproducible after a replace-import."""

_LINK_TOTALS_SQL: Final = text(
    """
    SELECT d.plan_id AS plan_id,
           d.source  AS source,
           SUM(d.impressions) AS impressions,
           SUM(d.clicks)      AS clicks
    FROM link_metrics_daily d
    WHERE d.metric_date >= :start_date
      AND d.metric_date <= :end_date
      AND d.plan_id IN :plans
    GROUP BY d.plan_id, d.source
    """
).bindparams(bindparam("plans", expanding=True))

_SEARCH_SQL: Final = text(
    """
    SELECT COALESCE(SUM(m.impressions), 0) AS impressions,
           COALESCE(SUM(m.clicks), 0)      AS clicks
    FROM page_metrics_daily m
    WHERE m.source = 'search_console'
      AND m.metric_date >= :start_date
      AND m.metric_date <= :end_date
      AND m.page_id IN :pages
    """
).bindparams(bindparam("pages", expanding=True))
"""DIAGNOSTIC ONLY (``LearningPolicy.search_is_diagnostic_only`` is a pinned ``Literal[True]``).
It is summed for a human to read and reaches no option, no prompt and no stored row."""


# --------------------------------------------------------------------------- #
# Decision class
# --------------------------------------------------------------------------- #
def decision_class_of(*, row: Mapping[str, Any]) -> DecisionClass:
    """The ONE place a plan row becomes a :class:`DecisionClass` (frozen ``DecisionClassOf``).

    ``row`` needs ``relationship_type`` (nullable), ``anchor_source`` and ``channels``.

    Raises:
        ValueError: an unknown enum value, or a plan with no retrieval channel -- a
            ``DecisionClass`` requires at least one, so such a plan cannot be keyed and is
            counted as skipped rather than silently folded into another class.
    """
    raw_channels = row["channels"] or ()
    if not raw_channels:
        raise ValueError("a plan with no retrieval channel has no decision class")
    relationship = row["relationship_type"]
    return DecisionClass(
        relationship_type=None if relationship is None else RelationshipType(relationship),
        anchor_source=row["anchor_source"],
        channels=tuple(RetrievalChannel(name) for name in raw_channels),
    )


def default_window(now: datetime | None = None) -> EvidenceWindow:
    """The trailing :data:`DEFAULT_WINDOW_DAYS` complete UTC days, ending yesterday."""
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    end = today - timedelta(days=1)
    return EvidenceWindow(start=end - timedelta(days=DEFAULT_WINDOW_DAYS - 1), end=end)


def _bounds(window: EvidenceWindow) -> tuple[datetime, datetime]:
    """``[start 00:00 UTC, end+1d 00:00 UTC)``, computed in Python so no server timezone can
    move a day boundary."""
    start = datetime.combine(window.start, datetime.min.time(), tzinfo=UTC)
    stop = datetime.combine(window.end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return start, stop


# --------------------------------------------------------------------------- #
# The aggregate table
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ClassAggregate:
    """One decision class: the frozen observation plus what the contract has no field for."""

    decision_class: DecisionClass
    observation: DecisionClassObservation
    plans: tuple[uuid.UUID, ...]
    target_replacements: int = 0
    """``replace_target`` actions that named a new destination."""
    distinct_replacement_pairs: int = 0
    """Distinct ``(target_before, target_after)`` pairs: how CONCENTRATED the disagreement is.
    No frozen evidence field holds it (ADR-0016 section 3 asked for it); it is logged."""
    measured_plans: int = 0


@dataclass(frozen=True, slots=True)
class AggregateReport:
    """Everything one reflection pass computed. The superset of what evidence can carry."""

    window: EvidenceWindow
    rows: tuple[ClassAggregate, ...]
    sample_count_total: int
    classes_below_floor: int
    plans_skipped_no_channel: int
    search_impressions: int
    search_clicks: int
    engine_version: str
    analysis_version: str
    policy_versions: dict[str, str]

    @property
    def observations(self) -> tuple[DecisionClassObservation, ...]:
        return tuple(row.observation for row in self.rows)

    @property
    def sufficient_rows(self) -> tuple[ClassAggregate, ...]:
        """The classes at or above the policy's sample floor. Below it, nothing is proposed."""
        return tuple(row for row in self.rows if row.observation.sufficient)


def policy_versions(settings: Settings) -> dict[str, str]:
    """``policy kind -> version`` for the six live documents, as ``/settings/policies`` reads
    them. Recorded in evidence so a stored proposal names the exact configuration it saw."""
    from ken_services.agents.policy import load_configured_agent_policy
    from ken_services.analytics.policy import load_analytics_policy
    from ken_services.intelligence.taxonomy import load_taxonomy
    from ken_services.learning.policy import load_learning_policy
    from ken_services.recommendations.policy import (
        load_recommendation_policy,
        resolve_ranking_policy,
    )

    recommendation = load_recommendation_policy(Path(settings.recommendation_policy_path))
    return {
        "taxonomy": str(load_taxonomy(Path(settings.taxonomy_path)).version),
        "recommendation": str(recommendation.version),
        "ranking": str(resolve_ranking_policy(recommendation, root=REPO_ROOT).version),
        "agents": str(load_configured_agent_policy(settings).version),
        "analytics": str(load_analytics_policy(Path(settings.analytics_policy_path)).version),
        "learning": str(load_learning_policy(Path(settings.learning_policy_path)).version),
    }


async def aggregate_streams(
    session: AsyncSession,
    *,
    settings: Settings,
    policy: LearningPolicy,
    window: EvidenceWindow,
) -> tuple[DecisionClassObservation, ...]:
    """The frozen :class:`~ken_services.learning.protocol.AggregateStreams` entry point."""
    report = await aggregate_report(session, settings=settings, policy=policy, window=window)
    return report.observations


async def aggregate_report(
    session: AsyncSession,
    *,
    settings: Settings,
    policy: LearningPolicy,
    window: EvidenceWindow,
) -> AggregateReport:
    """Compute the whole aggregate table for ``window``. Read-only; raises nothing typed."""
    start, stop = _bounds(window)
    rows = (await session.execute(_EDITORIAL_SQL, {"start": start, "stop": stop})).mappings().all()

    per_plan: dict[uuid.UUID, _PlanFacts] = {}
    skipped = 0
    for row in rows:
        try:
            klass = decision_class_of(row=dict(row))
        except ValueError:
            skipped += 1
            continue
        plan_id = uuid.UUID(str(row["plan_id"]))
        per_plan[plan_id] = _PlanFacts(
            plan_id=plan_id,
            target_page_id=uuid.UUID(str(row["target_page_id"])),
            decision_class=klass,
            final_action=str(row["final_action"]),
            edited=bool(row["edited"]),
            replacements=int(row["replacements"]),
            replacement_pairs=int(row["replacement_pairs"]),
            anchor_delta=int(row["anchor_delta"]),
            engine_version=str(row["engine_version"]),
            analysis_version=str(row["analysis_version"]),
        )

    plan_ids = sorted(per_plan, key=str)
    reasons = await _reasons(session, start=start, stop=stop, known=set(plan_ids))
    rolled_back = await _rolled_back(session, stop=stop, plans=plan_ids)
    measurements = await _measurements(session, stop=stop, plans=plan_ids)
    link_totals = await _link_totals(session, window=window, plans=plan_ids)
    search_impressions, search_clicks = await _search_totals(
        session,
        window=window,
        pages=sorted({facts.target_page_id for facts in per_plan.values()}, key=str),
    )

    floor = policy.gates.min_sample_count
    grouped: dict[str, list[_PlanFacts]] = {}
    for plan_id in plan_ids:
        grouped.setdefault(per_plan[plan_id].decision_class.canonical, []).append(per_plan[plan_id])

    aggregates: list[ClassAggregate] = []
    for canonical in sorted(grouped):
        facts = grouped[canonical]
        aggregates.append(
            _class_aggregate(
                canonical=canonical,
                facts=facts,
                reasons=reasons,
                rolled_back=rolled_back,
                measurements=measurements,
                link_totals=link_totals,
                floor=floor,
            )
        )
    if len(aggregates) > MAX_OBSERVATIONS:
        # The frozen evidence caps the table. Keep the largest classes: a class with more
        # samples is the only one that can ever clear the floor anyway.
        aggregates.sort(
            key=lambda row: (-row.observation.sample_count, row.decision_class.canonical)
        )
        aggregates = aggregates[:MAX_OBSERVATIONS]
        aggregates.sort(key=lambda row: row.decision_class.canonical)

    report = AggregateReport(
        window=window,
        rows=tuple(aggregates),
        sample_count_total=sum(row.observation.sample_count for row in aggregates),
        classes_below_floor=sum(1 for row in aggregates if not row.observation.sufficient),
        plans_skipped_no_channel=skipped,
        search_impressions=search_impressions,
        search_clicks=search_clicks,
        engine_version=_modal(facts.engine_version for facts in per_plan.values()),
        analysis_version=_modal(facts.analysis_version for facts in per_plan.values()),
        policy_versions=policy_versions(settings),
    )
    logger.info(
        "reflection_aggregate",
        window_start=window.start.isoformat(),
        window_end=window.end.isoformat(),
        decision_classes=len(report.rows),
        samples=report.sample_count_total,
        classes_below_floor=report.classes_below_floor,
        plans_skipped_no_channel=skipped,
        replacement_pairs=sum(row.distinct_replacement_pairs for row in report.rows),
        search_impressions=search_impressions,
        search_clicks=search_clicks,
    )
    return report


@dataclass(frozen=True, slots=True)
class _PlanFacts:
    plan_id: uuid.UUID
    target_page_id: uuid.UUID
    decision_class: DecisionClass
    final_action: str
    edited: bool
    replacements: int
    replacement_pairs: int
    anchor_delta: int
    engine_version: str
    analysis_version: str


def _class_aggregate(
    *,
    canonical: str,
    facts: Sequence[_PlanFacts],
    reasons: Mapping[uuid.UUID, Mapping[str, int]],
    rolled_back: frozenset[uuid.UUID],
    measurements: Mapping[uuid.UUID, _Measurement],
    link_totals: Mapping[uuid.UUID, Mapping[str, tuple[int, int]]],
    floor: int,
) -> ClassAggregate:
    sample_count = len(facts)
    approvals = sum(1 for item in facts if item.final_action == "approve")
    rejections = sum(1 for item in facts if item.final_action == "reject")
    edits = sum(1 for item in facts if item.edited)
    rollbacks = sum(1 for item in facts if item.plan_id in rolled_back)
    measured = [measurements[item.plan_id] for item in facts if item.plan_id in measurements]
    positives = sum(1 for item in measured if item.positive)

    histogram: dict[str, int] = {}
    for item in facts:
        for code, count in reasons.get(item.plan_id, {}).items():
            histogram[code] = histogram.get(code, 0) + count

    totals: dict[AnalyticsSource, MetricTotals] = {}
    accumulated: dict[str, tuple[int, int]] = {}
    for item in facts:
        for source, (impressions, clicks) in link_totals.get(item.plan_id, {}).items():
            previous = accumulated.get(source, (0, 0))
            accumulated[source] = (previous[0] + impressions, previous[1] + clicks)
    for source in sorted(accumulated):
        try:
            key = AnalyticsSource(source)
        except ValueError:  # pragma: no cover - a DB CHECK constrains the column
            continue
        impressions, clicks = accumulated[source]
        totals[key] = MetricTotals.of(impressions=impressions, clicks=clicks)

    flags = {flag for item in measured for flag in item.flags}
    if not measured:
        flags.add(ConfounderFlag.INSUFFICIENT_DATA)

    observation = DecisionClassObservation(
        decision_class=canonical,
        sample_count=sample_count,
        approvals=approvals,
        edits=edits,
        rejections=rejections,
        rollbacks=rollbacks,
        positive_outcomes=positives,
        approval_rate=_rate(approvals, sample_count),
        edit_rate=_rate(edits, sample_count),
        reject_rate=_rate(rejections, sample_count),
        rollback_rate=_rate(rollbacks, sample_count),
        positive_outcome_rate=_rate(positives, sample_count),
        reason_histogram=dict(sorted(histogram.items())),
        anchor_edit_char_delta=sum(item.anchor_delta for item in facts),
        link_totals=totals,
        behavioural_flags=tuple(sorted(flags, key=lambda flag: flag.value)),
        sufficient=sample_count >= floor,
    )
    return ClassAggregate(
        decision_class=DecisionClass.parse(canonical),
        observation=observation,
        plans=tuple(item.plan_id for item in facts),
        target_replacements=sum(item.replacements for item in facts),
        distinct_replacement_pairs=sum(item.replacement_pairs for item in facts),
        measured_plans=len(measured),
    )


def _rate(numerator: int, denominator: int) -> float | None:
    """``None`` when there is nothing to divide by: a rate over zero samples is not 0.0."""
    return None if denominator == 0 else numerator / denominator


def _modal(values: Any) -> str:
    """The most frequent value, ties broken lexicographically; :data:`UNKNOWN_VERSION` if none.

    The sample may span two engine or analysis versions. Evidence has one field for each, so it
    records the version MOST of the sample was produced under rather than inventing a list.
    """
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return UNKNOWN_VERSION
    return min(counts, key=lambda value: (-counts[value], value))


@dataclass(frozen=True, slots=True)
class _Measurement:
    positive: bool
    flags: tuple[ConfounderFlag, ...]


async def _reasons(
    session: AsyncSession, *, start: datetime, stop: datetime, known: set[uuid.UUID]
) -> dict[uuid.UUID, dict[str, int]]:
    result = await session.execute(_REASONS_SQL, {"start": start, "stop": stop})
    out: dict[uuid.UUID, dict[str, int]] = {}
    for row in result.mappings().all():
        key = uuid.UUID(str(row["plan_id"]))
        if key not in known:
            continue
        out.setdefault(key, {})[str(row["reason"])] = int(row["occurrences"])
    return out


async def _rolled_back(
    session: AsyncSession, *, stop: datetime, plans: Sequence[uuid.UUID]
) -> frozenset[uuid.UUID]:
    if not plans:
        return frozenset()
    result = await session.execute(_ROLLBACK_SQL, {"stop": stop, "plans": list(plans)})
    return frozenset(uuid.UUID(str(row["plan_id"])) for row in result.mappings().all())


async def _measurements(
    session: AsyncSession, *, stop: datetime, plans: Sequence[uuid.UUID]
) -> dict[uuid.UUID, _Measurement]:
    if not plans:
        return {}
    rows = (
        (await session.execute(_MEASUREMENT_SQL, {"stop": stop, "plans": list(plans)}))
        .mappings()
        .all()
    )
    out: dict[uuid.UUID, _Measurement] = {}
    for row in rows:
        flags: list[ConfounderFlag] = []
        for raw in row["flags"] or ():
            try:
                flags.append(ConfounderFlag(raw))
            except ValueError:  # pragma: no cover - the writer validates the contract
                continue
        out[uuid.UUID(str(row["plan_id"]))] = _Measurement(
            positive=bool(row["positive"]), flags=tuple(flags)
        )
    return out


async def _link_totals(
    session: AsyncSession, *, window: EvidenceWindow, plans: Sequence[uuid.UUID]
) -> dict[uuid.UUID, dict[str, tuple[int, int]]]:
    if not plans:
        return {}
    rows = (
        await session.execute(
            _LINK_TOTALS_SQL,
            {"start_date": window.start, "end_date": window.end, "plans": list(plans)},
        )
    ).all()
    out: dict[uuid.UUID, dict[str, tuple[int, int]]] = {}
    for row in rows:
        plan = uuid.UUID(str(row[0]))
        out.setdefault(plan, {})[str(row[1])] = (int(str(row[2])), int(str(row[3])))
    return out


async def _search_totals(
    session: AsyncSession, *, window: EvidenceWindow, pages: Sequence[uuid.UUID]
) -> tuple[int, int]:
    if not pages:
        return 0, 0
    result = await session.execute(
        _SEARCH_SQL,
        {"start_date": window.start, "end_date": window.end, "pages": list(pages)},
    )
    row = result.mappings().one()
    return int(row["impressions"]), int(row["clicks"])


# --------------------------------------------------------------------------- #
# Evidence: the ONLY construction site in the codebase
# --------------------------------------------------------------------------- #
def build_evidence(
    *,
    report: AggregateReport,
    options: Sequence[ProposalOption],
    chosen_index: int,
    generated_at: datetime,
    model: str | None = None,
    fallback_reason: FallbackReason | None = None,
) -> ProposalEvidence:
    """Assemble the frozen evidence. Not one number here comes from a model.

    ``options`` is the FULL server-enumerated set (ADR-0016 15.7.7), so the model's choice is
    reconstructable long after the run; ``model`` is the resolved REASONING model, or ``None``
    with a ``fallback_reason`` when the deterministic rule chose instead.

    Raises:
        ValueError: ``chosen_index`` is outside ``options``, or the evidence is incoherent --
            both frozen-model validations, deliberately not caught here.
    """
    return ProposalEvidence(
        window=report.window,
        generated_at=generated_at,
        aggregate_version=AGGREGATE_VERSION,
        observations=report.observations,
        options=tuple(options),
        chosen_index=chosen_index,
        sample_count_total=report.sample_count_total,
        policy_versions=dict(report.policy_versions),
        engine_version=report.engine_version or ENGINE_VERSION,
        analysis_version=report.analysis_version,
        model=model,
        fallback_used=fallback_reason is not None,
        fallback_reason=fallback_reason,
    )
