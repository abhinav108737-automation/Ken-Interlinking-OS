"""Governed priority-page designations (Business Phase 2A-1). PURE, SHADOW-ONLY.

A designation says "this page matters commercially, for this reason, owned by this person,
between these dates". It is **not** a licence to link: knowledge base §4 is explicit that
priority "is a ranking input, never a licence to override relevance, geography, page
eligibility, editorial safety, or trust controls".

**Why that holds here by construction, not by vigilance.** The hard gates
(:mod:`ken_services.recommendations.gates`) refuse a pair before scoring ever runs -- the
module docstring there states gates run "BEFORE scoring". Nothing in this module is reachable
for a refused pair, because a refused pair never becomes a candidate. The load-bearing test
asserts that directly against ``geography_gate`` / ``market_gate`` rather than inferring it
from an absence.

**Eligibility is a second, independent refusal.** A page that is unpublished, gone, protected,
noindex, non-canonical, HTTP-error, thin, excluded by page type, unanalysed or stale is
ineligible, and :func:`designated_value` yields nothing for it. That check reuses
:func:`ken_services.intelligence.eligibility.compute_eligibility` rather than reimplementing
the rule, so the two can never drift apart.

**Time is part of the record, not a cleanup job.** A designation resolves only inside its own
VALIDITY INTERVAL; nothing sweeps the table, and no row is mutated by the passage of time, so a
shadow report recomputed for a past instant still reproduces what was true then.

**Retirement truncates history, it does not erase it.** Retiring records an INSTANT
(``retired_at``), never a boolean. The validity interval is::

    [effective_from, min(effective_to, retired_at))      -- clamped, never inverted

so a designation retired today still resolves for a date on which it was in force, which is the
whole point of being able to reproduce a past shadow report. A boolean ``active`` flag could not
express this: it answers "is it live now", and every historical question would get today's
answer. ``retired_at`` before ``effective_from`` (cancelling a designation that had not yet
begun) clamps to an EMPTY interval -- it was never in force, and it blocks nothing.

:func:`ken_priority_validity` in migration 0013 is the same arithmetic in SQL, and
``test_the_sql_and_python_validity_intervals_agree`` holds the two to each other.

**Nothing here changes a live score.** :data:`SHADOW_PRIORITY_WEIGHT` is provisional and
deliberately absent from ``policies/ranking/ranking_weights.v1.yaml``: adding a ``priority``
component to :data:`~ken_contracts.recommendations.RANKING_COMPONENTS` is a frozen-contract
change awaiting its own ADR (owner decision 3, 2026-11-19). Until that ADR is accepted this
module only *reports* what priority would have contributed, and records the weight it assumed
so the report is self-describing.

The vocabulary itself (:class:`~ken_core.priority.PriorityType`, the tier bounds and
:func:`~ken_core.priority.tier_weight`) lives one layer down in ``ken_core`` so the ORM and
this module share one definition without ``ken_core`` importing ``ken_services``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from ken_contracts.intelligence import LinkEligibility
from ken_core.priority import MAX_TIER, MIN_TIER, PriorityType, tier_weight

__all__ = [
    "MAX_TIER",
    "MIN_TIER",
    "SHADOW_PRIORITY_WEIGHT",
    "PriorityDesignation",
    "PriorityType",
    "PriorityValue",
    "designated_value",
    "effective_designations",
    "require_aware",
    "tier_weight",
]


def require_aware(value: datetime | None, name: str) -> None:
    """Refuse a timezone-naive instant.

    A naive datetime silently means "whatever the writer's local clock said". A validity
    interval built from one cannot be compared with another machine's, and the database column
    is ``timestamptz``, so the value would be reinterpreted on the way in. Refusing at the
    boundary keeps every stored interval meaningful in one frame.
    """
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{name} must be timezone-aware")


SHADOW_PRIORITY_WEIGHT: Final = 0.10
"""PROVISIONAL. The knowledge base §5.5 hypothesis (10%), used for shadow reporting only.

It is not a ranking-policy weight and is not loaded from one: ``RANKING_COMPONENTS`` is a
closed tuple (``ken_contracts.recommendations``) and adding to it is a frozen-contract change
pending the scoring ADR. Every shadow row records this value, so a report produced under one
assumption is never mistaken for a report produced under another.
"""


@dataclass(frozen=True, slots=True)
class PriorityDesignation:
    """One governed designation. Mirrors a ``page_priorities`` row (migration 0013)."""

    id: uuid.UUID
    page_id: uuid.UUID
    priority_type: PriorityType
    tier: int
    reason: str
    owner: str
    effective_from: datetime
    effective_to: datetime | None
    retired_at: datetime | None
    created_at: datetime
    created_by: str
    updated_at: datetime
    updated_by: str

    def __post_init__(self) -> None:
        # Instants are validated FIRST: comparing a naive datetime with an aware one raises
        # TypeError, which would mask the real fault with an unhelpful message.
        for name in ("effective_from", "effective_to", "retired_at", "created_at", "updated_at"):
            require_aware(getattr(self, name), name)
        if not MIN_TIER <= self.tier <= MAX_TIER:
            raise ValueError(f"tier {self.tier} outside [{MIN_TIER}, {MAX_TIER}]")
        if not self.reason.strip():
            raise ValueError("a designation must carry a reason")
        if not self.owner.strip():
            raise ValueError("a designation must carry an owner")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")

    @property
    def validity_end(self) -> datetime | None:
        """The instant the designation stops being in force; ``None`` = open-ended.

        ``min(effective_to, retired_at)``, clamped up to ``effective_from`` so the interval can
        never invert. Equal to ``effective_from`` means an EMPTY interval: in force at no
        instant at all.
        """
        ends = [e for e in (self.effective_to, self.retired_at) if e is not None]
        return None if not ends else max(self.effective_from, min(ends))

    @property
    def is_empty(self) -> bool:
        """Never in force at any instant (retired at or before it began)."""
        return self.validity_end is not None and self.validity_end <= self.effective_from

    def is_effective_at(self, at: datetime) -> bool:
        """Is this designation in force at ``at``? Half-open: ``[from, validity_end)``.

        Half-open so an end instant belongs to exactly one interval, which is what lets a
        replacement start at the moment its predecessor ends with neither overlap nor gap.

        Retirement is read as an instant, so this answers the question for ANY ``at``, past or
        present -- a designation retired today is still in force for a date before that.
        """
        require_aware(at, "at")
        if at < self.effective_from:
            return False
        end = self.validity_end
        return end is None or at < end

    @property
    def is_retired(self) -> bool:
        """Retirement has been RECORDED. It may still be in force (a scheduled retirement)."""
        return self.retired_at is not None

    @property
    def weight(self) -> float:
        return tier_weight(self.tier)


@dataclass(frozen=True, slots=True)
class PriorityValue:
    """What a page's designations are worth at one instant, and why.

    ``contribution`` is HYPOTHETICAL: it is what a ``priority`` ranking component would have
    added had one existed. It is reported, never applied (module docstring).
    """

    page_id: uuid.UUID
    value: float
    """The strongest effective designation's normalised weight, or 0.0."""
    tier: int | None
    types: tuple[PriorityType, ...]
    """Every effective type, deterministically ordered. Empty when nothing resolves."""
    contribution: float
    """``value * SHADOW_PRIORITY_WEIGHT``."""
    assumed_weight: float
    """The :data:`SHADOW_PRIORITY_WEIGHT` this row was computed under."""
    ineligible_reasons: tuple[str, ...] = ()
    """Non-empty when designations existed but the page was refused them."""

    @property
    def applies(self) -> bool:
        return self.value > 0.0


def _ordering(designation: PriorityDesignation) -> tuple[int, str, str]:
    """Strongest tier first, then type name, then id -- a total order over ties.

    Ties are broken by data that cannot change between runs, so two runs over the same rows
    always report the same winner. Insertion order and dict iteration never participate.
    """
    return (designation.tier, str(designation.priority_type), str(designation.id))


def effective_designations(
    designations: Iterable[PriorityDesignation], *, at: datetime
) -> tuple[PriorityDesignation, ...]:
    """Those in force at ``at``, strongest first. Expired, future and retired rows drop."""
    return tuple(sorted((d for d in designations if d.is_effective_at(at)), key=_ordering))


def designated_value(
    designations: Sequence[PriorityDesignation],
    *,
    page_id: uuid.UUID,
    at: datetime,
    eligibility: LinkEligibility | None = None,
) -> PriorityValue:
    """A page's priority at one instant, after eligibility.

    ``eligibility`` is the page's own :class:`~ken_contracts.intelligence.LinkEligibility`.
    When it refuses the page as a TARGET, the page earns nothing however it is designated:
    priority ranks destinations, and an unpublished, gone, protected, noindex, non-canonical,
    erroring, thin, type-excluded, unanalysed or stale page is not a destination. The reasons
    are carried through so a shadow report can say which rule refused it, rather than
    reporting a silent zero.

    Passing ``None`` means eligibility was not evaluated here -- used by unit tests of the
    time window alone. The shadow report always passes a real verdict.
    """
    if eligibility is not None and not eligibility.as_target:
        return PriorityValue(
            page_id=page_id,
            value=0.0,
            tier=None,
            types=(),
            contribution=0.0,
            assumed_weight=SHADOW_PRIORITY_WEIGHT,
            ineligible_reasons=tuple(str(r) for r in eligibility.target_reasons),
        )

    effective = effective_designations(designations, at=at)
    if not effective:
        return PriorityValue(
            page_id=page_id,
            value=0.0,
            tier=None,
            types=(),
            contribution=0.0,
            assumed_weight=SHADOW_PRIORITY_WEIGHT,
        )

    strongest = effective[0]
    value = strongest.weight
    return PriorityValue(
        page_id=page_id,
        value=value,
        tier=strongest.tier,
        types=tuple(d.priority_type for d in effective),
        contribution=value * SHADOW_PRIORITY_WEIGHT,
        assumed_weight=SHADOW_PRIORITY_WEIGHT,
    )
