"""Typed page relationships from taxonomy and similarity (ADR-0010 section 4, PRD 7.3).

Directed: every candidate reads "source -> target". Symmetric types
(same_market, related_market, same_geography, complementary_market,
industry_adjacency) yield one candidate per call; the caller calls once per
direction. Candidates come back in :class:`RelationshipType` declaration order.
Evidence holds structured ids and numbers only, never free text.

Market relationships use each page's ``market_id``; geography relationships
use the page's listed geographies; industry adjacency uses ``industry_id``.
Ids unknown to ``taxonomy`` (an analysis from another taxonomy version) produce
no market or industry relationship.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

from ken_contracts.intelligence import PageAnalysis, RelationshipType, TaxonomyPolicy
from ken_services.intelligence.taxonomy import TaxonomyIndex, taxonomy_index

__all__ = [
    "RELATIONSHIP_WEIGHTS",
    "RelationshipCandidate",
    "semantic_relationship",
    "taxonomy_relationships",
]

RELATIONSHIP_WEIGHTS: Final[dict[RelationshipType, float]] = {
    RelationshipType.SAME_MARKET: 1.0,
    RelationshipType.PARENT_MARKET: 0.8,
    RelationshipType.SUBMARKET: 0.8,
    RelationshipType.RELATED_MARKET: 0.6,
    RelationshipType.SAME_GEOGRAPHY: 0.4,
    RelationshipType.REGIONAL_PARENT: 0.3,
    RelationshipType.SUPPLY_CHAIN_UPSTREAM: 0.7,
    RelationshipType.SUPPLY_CHAIN_DOWNSTREAM: 0.7,
    RelationshipType.COMPLEMENTARY_MARKET: 0.6,
    RelationshipType.INDUSTRY_ADJACENCY: 0.3,
}
"""Fixed weight per taxonomy relationship type, in [0, 1]."""


@dataclass(frozen=True, slots=True)
class RelationshipCandidate:
    relationship_type: RelationshipType
    weight: float
    evidence: dict[str, Any]


def _candidate(kind: RelationshipType, evidence: dict[str, Any]) -> RelationshipCandidate:
    return RelationshipCandidate(
        relationship_type=kind, weight=RELATIONSHIP_WEIGHTS[kind], evidence=evidence
    )


def _market_relationships(
    index: TaxonomyIndex, source: str, target: str
) -> list[RelationshipCandidate]:
    pair: dict[str, Any] = {"source_market": source, "target_market": target}
    out: list[RelationshipCandidate] = []
    source_parent = index.market_parent(source)
    target_parent = index.market_parent(target)
    if source == target:
        out.append(_candidate(RelationshipType.SAME_MARKET, dict(pair)))
        return out  # every other market relationship needs two different markets
    if source_parent == target:
        out.append(_candidate(RelationshipType.PARENT_MARKET, dict(pair)))
    if target_parent == source:
        out.append(_candidate(RelationshipType.SUBMARKET, dict(pair)))
    if source_parent is not None and source_parent == target_parent:
        out.append(
            _candidate(RelationshipType.RELATED_MARKET, {**pair, "parent_market": source_parent})
        )
    return out


def _supply_chain_relationships(
    index: TaxonomyIndex, source: str, target: str
) -> list[RelationshipCandidate]:
    if source == target:
        return []
    pair: dict[str, Any] = {"source_market": source, "target_market": target}
    src = index.market(source)
    tgt = index.market(target)
    out: list[RelationshipCandidate] = []
    if target in src.upstream:
        out.append(_candidate(RelationshipType.SUPPLY_CHAIN_UPSTREAM, dict(pair)))
    if target in src.downstream:
        out.append(_candidate(RelationshipType.SUPPLY_CHAIN_DOWNSTREAM, dict(pair)))
    if target in src.complementary or source in tgt.complementary:
        out.append(_candidate(RelationshipType.COMPLEMENTARY_MARKET, dict(pair)))
    return out


def _geography_relationships(
    index: TaxonomyIndex, source: PageAnalysis, target: PageAnalysis
) -> list[RelationshipCandidate]:
    out: list[RelationshipCandidate] = []
    source_countries = {g.geo_id for g in source.geographies if g.kind == "country"}
    target_countries = {g.geo_id for g in target.geographies if g.kind == "country"}
    shared = sorted(source_countries & target_countries)
    if shared:
        out.append(_candidate(RelationshipType.SAME_GEOGRAPHY, {"shared_geographies": shared}))

    target_regions = {g.geo_id for g in target.geographies if g.kind == "region"}
    pairs = sorted(
        (country, region)
        for country in source_countries
        if index.has_geography(country)
        for region in index.geography_ancestors(country)
        if region in target_regions
    )
    if pairs:
        out.append(
            _candidate(
                RelationshipType.REGIONAL_PARENT,
                {
                    "source_geographies": sorted({c for c, _ in pairs}),
                    "target_regions": sorted({r for _, r in pairs}),
                },
            )
        )
    return out


def _industry_adjacency(
    index: TaxonomyIndex, source: str | None, target: str | None
) -> list[RelationshipCandidate]:
    if source is None or target is None or source == target:
        return []
    if not (index.has_market(source) and index.has_market(target)):
        return []
    if target in index.market(source).adjacent or source in index.market(target).adjacent:
        return [
            _candidate(
                RelationshipType.INDUSTRY_ADJACENCY,
                {"source_industry": source, "target_industry": target},
            )
        ]
    return []


def taxonomy_relationships(
    source: PageAnalysis, target: PageAnalysis, *, taxonomy: TaxonomyPolicy
) -> tuple[RelationshipCandidate, ...]:
    """Every taxonomy relationship ``source -> target``, in RelationshipType order.

    Returns ``()`` when both analyses describe the same page.
    """
    if source.page_id == target.page_id:
        return ()
    index = taxonomy_index(taxonomy)
    found: list[RelationshipCandidate] = []
    s_market, t_market = source.market_id, target.market_id
    markets_known = (
        s_market is not None
        and t_market is not None
        and index.has_market(s_market)
        and index.has_market(t_market)
    )
    if markets_known and s_market is not None and t_market is not None:
        found.extend(_market_relationships(index, s_market, t_market))
        found.extend(_supply_chain_relationships(index, s_market, t_market))
    found.extend(_geography_relationships(index, source, target))
    found.extend(_industry_adjacency(index, source.industry_id, target.industry_id))
    order = {kind: n for n, kind in enumerate(RelationshipType)}
    found.sort(key=lambda c: order[c.relationship_type])
    return tuple(found)


def semantic_relationship(score: float, *, min_score: float) -> RelationshipCandidate | None:
    """A ``semantic_similar`` candidate when cosine ``score`` is above ``min_score``.

    ``weight = clamp((score - min_score) / (1 - min_score), 0, 1)``; a score at or
    below the threshold (or NaN) gives ``None``. Evidence: ``{"cosine": round(score, 4)}``.
    """
    if math.isnan(score) or math.isnan(min_score) or score <= min_score:
        return None
    span = 1.0 - min_score
    weight = 1.0 if span <= 0 else (score - min_score) / span
    weight = min(1.0, max(0.0, weight))
    return RelationshipCandidate(
        relationship_type=RelationshipType.SEMANTIC_SIMILAR,
        weight=weight,
        evidence={"cosine": round(score, 4)},
    )
