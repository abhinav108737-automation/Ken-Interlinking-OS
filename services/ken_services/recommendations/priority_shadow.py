"""Deterministic shadow reporting for governed priority (Business Phase 2A-1).

**What this answers.** "If a ``priority`` ranking component existed, what would it have
changed?" -- asked only of candidates that have ALREADY qualified, and answered without
changing a single live score. That is the evidence the pending scoring ADR needs (owner
decision 3, 2026-11-19), gathered before the contract changes rather than after.

**Which candidates qualify -- and why priority cannot smuggle a pair past a gate.** The
pipeline (``pipeline.py``) runs, in order: hard gates and eligibility -> ``min_relevance`` ->
placement -> ``min_score`` -> capacity caps -> plan. A candidate reaches this module only if it
cleared EVERYTHING up to and including ``min_score``:

* a survivor (``link_candidates.rejection IS NULL`` -- it became a plan), or
* a candidate cut ONLY by a capacity cap, :data:`CAPACITY_REJECTIONS` -- it qualified on every
  gate and both floors and lost a limited slot to higher-ranked candidates.

That second group is where priority could legitimately matter: it can change WHICH qualified
candidates win the slots. ``below_min_relevance`` and ``below_min_score`` are deliberately
excluded -- owner decision 1 forbids priority from bypassing relevance, so it must never lift a
sub-threshold pair over the line. ``link_density_exceeded`` is also excluded: it is the
over-linking SAFETY guard, not a capacity race. This module has no path from a designation to
a candidate; it can reorder and report, never admit.

**Per source.** ``max_plans_per_source`` applies per source page, so ranks and the selection
comparison are computed within each source. ``live_score`` is the engine's FINAL score for the
pair (``link_candidates.total_score``, i.e. ``ScoreBreakdown.total``), and the shadow score adds
the hypothetical contribution to it. Whether a real ``priority`` component should enter before
or after the placement factor is the ADR's decision; the additive form here is the simplest
honest approximation and is stated in every report.

**Determinism.** Rows sort by a total order over data that cannot vary between runs, and the
report records the inputs it assumed. Two runs over the same registry and candidates produce
identical output; that is asserted, not assumed.

Nothing here writes to ``link_plans``, ``page_modules`` or any analytics table.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from ken_contracts.intelligence import LinkEligibility
from ken_contracts.recommendations import CandidateRejection
from ken_services.recommendations.priority import (
    SHADOW_PRIORITY_WEIGHT,
    PriorityDesignation,
    PriorityType,
    PriorityValue,
    designated_value,
)

__all__ = [
    "CAPACITY_REJECTIONS",
    "SHADOW_FORMULA",
    "SHADOW_REPORT_VERSION",
    "ShadowCandidate",
    "ShadowReport",
    "ShadowRow",
    "shadow_report",
    "shadow_rows",
]

SHADOW_REPORT_VERSION: Final = 1
"""Bumped when the row shape or the ordering rule changes, so two reports are comparable
only when they agree. Not a policy version: this report steers nothing."""

CAPACITY_REJECTIONS: Final[frozenset[CandidateRejection]] = frozenset(
    {CandidateRejection.PER_SOURCE_LIMIT, CandidateRejection.DIVERSITY_LIMIT}
)
"""Rejections that mean "qualified on every gate and floor, lost a limited slot". The only
rejected candidates a shadow report may consider (module docstring)."""

SHADOW_FORMULA: Final = "shadow_score = min(1, live_score + priority_value * assumed_weight)"
"""Stated in every report so a reader never mistakes the approximation for the ADR's formula."""

_NO_VERDICT: Final = LinkEligibility(
    as_source=False, as_target=False, source_reasons=[], target_reasons=[]
)


@dataclass(frozen=True, slots=True)
class ShadowCandidate:
    """One qualified candidate (module docstring), with the live outcome it already had."""

    source_page_id: uuid.UUID
    target_page_id: uuid.UUID
    target_url: str
    live_score: float
    """The engine's final score (``ScoreBreakdown.total``). Read, never written."""
    live_selected: bool
    """Whether it became a plan (``True``) or was cut by a capacity cap (``False``)."""

    def __post_init__(self) -> None:
        if not 0.0 <= self.live_score <= 1.0:
            raise ValueError("live_score outside [0, 1]")


@dataclass(frozen=True, slots=True)
class ShadowRow:
    """What priority would have done to one candidate. Reported, never applied."""

    source_page_id: uuid.UUID
    target_page_id: uuid.UUID
    target_url: str
    live_score: float
    priority_value: float
    priority_tier: int | None
    priority_types: tuple[PriorityType, ...]
    contribution: float
    """``priority_value * assumed_weight`` -- the hypothetical component contribution."""
    shadow_score: float
    live_rank: int
    """Rank within the candidate's SOURCE, by live score."""
    shadow_rank: int
    delta_rank: int
    """``live_rank - shadow_rank``. Positive = priority would have promoted it."""
    live_selected: bool
    shadow_selected: bool
    """Within its source, would it be among the top-k by shadow score, where k is the number
    the engine actually selected for that source."""
    assumed_weight: float
    ineligible_reasons: tuple[str, ...]
    """Why a designated target earned nothing. Empty when it earned something or had none."""

    @property
    def moved(self) -> bool:
        return self.delta_rank != 0

    @property
    def selection_changed(self) -> bool:
        """Priority would have swapped this candidate into, or out of, the plan set."""
        return self.live_selected != self.shadow_selected

    @property
    def priority_share(self) -> float:
        """Priority's share of the shadow score -- evidence for the ADR's dominance limit.

        Owner decision 3 asks for "limits preventing priority from dominating topical
        relevance". Measuring the share BEFORE choosing a limit is the point of this report.
        """
        return 0.0 if self.shadow_score <= 0.0 else self.contribution / self.shadow_score


def _order(score: float, candidate: ShadowCandidate) -> tuple[float, str, str]:
    """Descending score, ties broken by target url then id: a total order."""
    return (-score, candidate.target_url, str(candidate.target_page_id))


def _rows_for_source(
    candidates: Sequence[ShadowCandidate],
    *,
    designations: Mapping[uuid.UUID, Sequence[PriorityDesignation]],
    eligibility: Mapping[uuid.UUID, LinkEligibility],
    at: datetime,
) -> list[ShadowRow]:
    values: dict[uuid.UUID, PriorityValue] = {
        c.target_page_id: designated_value(
            tuple(designations.get(c.target_page_id, ())),
            page_id=c.target_page_id,
            at=at,
            # Silence is not a verdict: a target with no eligibility record is ineligible.
            eligibility=eligibility.get(c.target_page_id, _NO_VERDICT),
        )
        for c in candidates
    }
    shadow_score = {
        c.target_page_id: min(1.0, c.live_score + values[c.target_page_id].contribution)
        for c in candidates
    }
    live = sorted(candidates, key=lambda c: _order(c.live_score, c))
    shadow = sorted(candidates, key=lambda c: _order(shadow_score[c.target_page_id], c))
    live_rank = {c.target_page_id: i + 1 for i, c in enumerate(live)}
    shadow_rank = {c.target_page_id: i + 1 for i, c in enumerate(shadow)}
    selected_count = sum(1 for c in candidates if c.live_selected)
    shadow_selected = {c.target_page_id for c in shadow[:selected_count]}

    rows: list[ShadowRow] = []
    for c in shadow:
        value = values[c.target_page_id]
        rows.append(
            ShadowRow(
                source_page_id=c.source_page_id,
                target_page_id=c.target_page_id,
                target_url=c.target_url,
                live_score=c.live_score,
                priority_value=value.value,
                priority_tier=value.tier,
                priority_types=value.types,
                contribution=value.contribution,
                shadow_score=shadow_score[c.target_page_id],
                live_rank=live_rank[c.target_page_id],
                shadow_rank=shadow_rank[c.target_page_id],
                delta_rank=live_rank[c.target_page_id] - shadow_rank[c.target_page_id],
                live_selected=c.live_selected,
                shadow_selected=c.target_page_id in shadow_selected,
                assumed_weight=value.assumed_weight,
                ineligible_reasons=value.ineligible_reasons,
            )
        )
    return rows


def shadow_rows(
    candidates: Sequence[ShadowCandidate],
    *,
    designations: Mapping[uuid.UUID, Sequence[PriorityDesignation]],
    eligibility: Mapping[uuid.UUID, LinkEligibility],
    at: datetime,
) -> tuple[ShadowRow, ...]:
    """Rank ``candidates`` live and in shadow within each source, and report the difference.

    ``candidates`` MUST be qualified (module docstring); this function neither re-runs the
    gates nor could admit a pair that failed one. ``designations`` and ``eligibility`` are
    keyed by TARGET page id -- priority ranks destinations.
    """
    by_source: dict[uuid.UUID, list[ShadowCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_source[candidate.source_page_id].append(candidate)
    rows: list[ShadowRow] = []
    for source_id in sorted(by_source, key=str):
        rows.extend(
            _rows_for_source(
                by_source[source_id], designations=designations, eligibility=eligibility, at=at
            )
        )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class ShadowReport:
    """A whole shadow run: the rows plus the inputs they were computed under.

    Self-describing on purpose. A report that does not state the weight, formula and instant it
    assumed cannot be compared with another, and comparing them is the only reason it exists.
    """

    report_version: int
    assumed_weight: float
    formula: str
    at: datetime
    rows: tuple[ShadowRow, ...]

    @property
    def designated(self) -> tuple[ShadowRow, ...]:
        return tuple(r for r in self.rows if r.priority_value > 0.0)

    @property
    def refused(self) -> tuple[ShadowRow, ...]:
        """Rows whose target held designations but was refused them by eligibility."""
        return tuple(r for r in self.rows if r.ineligible_reasons)

    @property
    def moved(self) -> tuple[ShadowRow, ...]:
        return tuple(r for r in self.rows if r.moved)

    @property
    def selection_changes(self) -> tuple[ShadowRow, ...]:
        return tuple(r for r in self.rows if r.selection_changed)

    @property
    def max_priority_share(self) -> float:
        return max((r.priority_share for r in self.rows), default=0.0)

    def summary(self) -> str:
        sources = len({r.source_page_id for r in self.rows})
        return (
            f"priority shadow v{self.report_version} at {self.at.isoformat()} "
            f"(assumed weight {self.assumed_weight}; {self.formula}): "
            f"{len(self.rows)} qualified candidates across {sources} sources, "
            f"{len(self.designated)} designated, {len(self.refused)} refused by eligibility, "
            f"{len(self.moved)} would change rank, "
            f"{len(self.selection_changes)} would change the plan set, "
            f"max priority share {self.max_priority_share:.3f}"
        )


def shadow_report(
    candidates: Sequence[ShadowCandidate],
    *,
    designations: Mapping[uuid.UUID, Sequence[PriorityDesignation]],
    eligibility: Mapping[uuid.UUID, LinkEligibility],
    at: datetime,
) -> ShadowReport:
    """:func:`shadow_rows` plus the inputs it assumed."""
    return ShadowReport(
        report_version=SHADOW_REPORT_VERSION,
        assumed_weight=SHADOW_PRIORITY_WEIGHT,
        formula=SHADOW_FORMULA,
        at=at,
        rows=shadow_rows(candidates, designations=designations, eligibility=eligibility, at=at),
    )
