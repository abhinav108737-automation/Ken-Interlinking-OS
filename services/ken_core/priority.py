"""The governed priority vocabulary (Business Phase 2A-1, knowledge base §5.1).

**Why this sits in ``ken_core`` and not in ``ken_contracts``.** Every other ORM model draws
its CHECK-constraint vocabulary from a frozen contract enum. Priority cannot do that yet:
whether ``PriorityType`` graduates into ``ken_contracts`` -- alongside a ``priority`` entry in
``RANKING_COMPONENTS`` and priority-specific reason codes -- is precisely what the pending
scoring ADR decides (owner decision 3, 2026-11-19). Putting it here keeps slice 2A-1 free of
any frozen-contract change while still giving the ORM and the service layer ONE definition to
share.

**Why not in ``ken_services``.** ``ken_core`` is the lower layer; the ORM lives here. A
``ken_core -> ken_services`` import would be the only such edge in the repository and would
invert the dependency. The vocabulary therefore lives at the bottom and both layers import
down to it.

When the scoring ADR is accepted, this module is the single place to retire: move the enum
into ``ken_contracts``, re-point the two importers, and add the migration that widens the
CHECK constraint if the member list changed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = ["MAX_TIER", "MIN_TIER", "PriorityType", "tier_weight"]


class PriorityType(StrEnum):
    """The nine governed designations of knowledge base §5.1.

    Migration 0013 holds a frozen copy of these values in a CHECK constraint, following the
    convention migration 0012 set for ``ModuleKind``: the database states what it accepted at
    that revision, independently of what the code later says.
    """

    HIGH_PRIORITY_REPORT = "high_priority_report"
    HIGH_REVENUE_REPORT = "high_revenue_report"
    CONSULTING_PRIORITY = "consulting_priority"
    SURVEY_PRIORITY = "survey_priority"
    EXPERT_PANEL_PRIORITY = "expert_panel_priority"
    COUNTRY_OR_REGION_PRIORITY = "country_or_region_priority"
    TRENDING_MARKET = "trending_market"
    NEW_OR_REFRESHED_REPORT = "new_or_refreshed_report"
    LEAD_GENERATION_PAGE = "lead_generation_page"


MIN_TIER: Final = 1
"""The strongest tier. Tier 1 is the owner's top designation, not a default."""

MAX_TIER: Final = 5


def tier_weight(tier: int) -> float:
    """Tier -> normalised weight in (0, 1]. Tier 1 = 1.0, tier 5 = 0.2.

    Deterministic and linear. A tier is an owner's editorial judgement; turning it into a
    float here keeps that judgement in the data and the arithmetic in one place.
    """
    if not MIN_TIER <= tier <= MAX_TIER:
        raise ValueError(f"tier {tier} outside [{MIN_TIER}, {MAX_TIER}]")
    return (MAX_TIER + 1 - tier) / MAX_TIER
