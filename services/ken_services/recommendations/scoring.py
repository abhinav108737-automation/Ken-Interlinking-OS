"""The ADR-0011 section 4 transparent score: ``relevance = sum(weight x component)``.

Every component is in [0, 1] and computed from the pair alone (pure):

=============  ==============================================================================
semantic       ``clamp((cosine - floor) / (1 - floor))``; 0 without an embedding (or NaN)
taxonomy       max weight (``RELATIONSHIP_WEIGHTS``) over the MARKET-STRUCTURE relationships
               source -> target (:data:`MARKET_STRUCTURE_TYPES`: every taxonomy type except
               the two geography types, which feed ``geography``); computed live
intent         ``intent_compatibility[source intent][target intent]`` (missing = 0)
graph          complementarity: ``1 / (1 + links from the source into the target's market)``
geography      the PRIMARY-geography condition's ``geography_scores`` value
               (:func:`geography_match`). Geography is a GATE
               (:func:`~ken_services.recommendations.gates.geography_gate`): a mismatch
               REFUSES the pair, so this component only reports how close the match is
commercial     target ``commercial_importance / 3``
underlinked    ``max(0, 1 - inbound / underlinked_inbound_threshold)``
historical     0 until Phase 8 provides calibrated outcome evidence
=============  ==============================================================================

:func:`score_pair` returns the RELEVANCE breakdown (``factors = {}``, ``total = relevance``);
:func:`with_placement` multiplies in the placement quality once an anchor is found.

**Signals are reported separately, never collapsed** (directive sections 6 and 10).
:attr:`ScoredPair.signals` lists every measured signal under its own name --
``semantic_similarity``, ``lexical_similarity``, ``topic_relevance``, ``geography``,
``destination_priority``, the ``graph_*`` position, ``orphan_recovery_value``,
``gsc_opportunity`` and ``ga4_engagement`` -- exactly as the generators measured them. The eight
weighted components above are the FROZEN ranking contract (``RANKING_COMPONENTS``); a signal
that is not one of them is RECORDED, not weighted, until a new ranking policy version adds a
component. In particular the analytics signals never enter ``relevance``: first-party counts are
spoofable, so analytics may surface and may block but never justify (ADR-0016 s16.7).
:func:`explain_ranking` packages the final score, the full breakdown, the policy versions, the
(raw) confidence and the per-channel attribution into one record.

Reason codes are derived from the components (never free text), deduplicated and listed in
:class:`~ken_contracts.recommendations.ReasonCode` declaration order:

* ``semantic > 0`` -> ``semantically_similar``;
* every market-structure relationship -> its code (both ``supply_chain_*`` -> ``supply_chain``);
* geography ``same_country`` -> ``same_geography``; ``region_contains_source`` or
  ``same_region`` -> ``regional_context`` (``global_target`` adds no code);
* target ``commercial_importance == 3`` -> ``high_commercial_value``;
* ``underlinked > 0`` -> ``underlinked_target``; ``inbound == 0`` -> ``orphan_target``.

Channel and anchor codes (``lexical_overlap``, ``anchor_matches_target_*``) are not score
components; add them with :func:`merge_reason_codes`, :func:`channel_reason_codes` and
:func:`anchor_reason_code`. The geography GATE's own codes (``global_target``,
``cross_geography_supported``) come from
:attr:`~ken_services.recommendations.gates.GeographyVerdict.reason_codes`, which the pipeline
merges in: a gate decision is not a score component either.

``ScoredPair.relationship_type`` is the strongest market-structure relationship (highest
weight, then :class:`RelationshipType` order); else ``semantic_similar`` when ``semantic > 0``;
else ``same_geography`` (same country) or ``regional_parent`` (target region contains a
source country); else ``None``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, cast

from ken_contracts.intelligence import PageAnalysis, RelationshipType, TaxonomyPolicy
from ken_contracts.recommendations import (
    RANKING_COMPONENTS,
    CandidateAttribution,
    GraphSignals,
    RankingPolicy,
    ReasonCode,
    RecommendationPolicy,
    RetrievalChannel,
    ScoreBreakdown,
    ScoreComponent,
)
from ken_services.intelligence.relationships import RELATIONSHIP_WEIGHTS, taxonomy_relationships
from ken_services.recommendations.filters import intent_compatibility
from ken_services.recommendations.gates import GEOGRAPHY_SCORE_KEYS, geography_relation

__all__ = [
    "GEOGRAPHY_KEYS",
    "MARKET_STRUCTURE_TYPES",
    "PLACEMENT_FACTOR",
    "PairFeatures",
    "RankingExplanation",
    "ScoredPair",
    "anchor_reason_code",
    "channel_reason_codes",
    "channel_scores_for",
    "explain_ranking",
    "geography_match",
    "merge_reason_codes",
    "score_pair",
    "with_placement",
]

PLACEMENT_FACTOR: Final = "placement_quality"

MARKET_STRUCTURE_TYPES: Final[frozenset[RelationshipType]] = frozenset(
    {
        RelationshipType.SAME_MARKET,
        RelationshipType.PARENT_MARKET,
        RelationshipType.SUBMARKET,
        RelationshipType.RELATED_MARKET,
        RelationshipType.SUPPLY_CHAIN_UPSTREAM,
        RelationshipType.SUPPLY_CHAIN_DOWNSTREAM,
        RelationshipType.COMPLEMENTARY_MARKET,
        RelationshipType.INDUSTRY_ADJACENCY,
    }
)
"""Taxonomy types that feed the ``taxonomy`` component (geography types feed ``geography``)."""

type GeographyKey = Literal[
    "same_country", "region_contains_source", "same_region", "global_target"
]
GEOGRAPHY_KEYS: Final[tuple[GeographyKey, ...]] = (
    "same_country",
    "region_contains_source",
    "same_region",
    "global_target",
)
"""Every ``geography_scores`` key, strongest first. Exactly one applies to a pair: the key of
the gate's structural decision (``gates.GEOGRAPHY_SCORE_KEYS``), so there is no contest
between keys to resolve any more."""

_RELATIONSHIP_CODES: Final[Mapping[RelationshipType, ReasonCode]] = {
    RelationshipType.SAME_MARKET: ReasonCode.SAME_MARKET,
    RelationshipType.PARENT_MARKET: ReasonCode.PARENT_MARKET,
    RelationshipType.SUBMARKET: ReasonCode.SUBMARKET,
    RelationshipType.RELATED_MARKET: ReasonCode.RELATED_MARKET,
    RelationshipType.SUPPLY_CHAIN_UPSTREAM: ReasonCode.SUPPLY_CHAIN,
    RelationshipType.SUPPLY_CHAIN_DOWNSTREAM: ReasonCode.SUPPLY_CHAIN,
    RelationshipType.COMPLEMENTARY_MARKET: ReasonCode.COMPLEMENTARY_MARKET,
    RelationshipType.INDUSTRY_ADJACENCY: ReasonCode.INDUSTRY_ADJACENCY,
}
_GEOGRAPHY_CODES: Final[Mapping[GeographyKey, ReasonCode]] = {
    "same_country": ReasonCode.SAME_GEOGRAPHY,
    "region_contains_source": ReasonCode.REGIONAL_CONTEXT,
    "same_region": ReasonCode.REGIONAL_CONTEXT,
}
_GEOGRAPHY_RELATIONSHIPS: Final[Mapping[GeographyKey, RelationshipType]] = {
    "same_country": RelationshipType.SAME_GEOGRAPHY,
    "region_contains_source": RelationshipType.REGIONAL_PARENT,
}
_REASON_ORDER: Final = {code: n for n, code in enumerate(ReasonCode)}
_RELATIONSHIP_ORDER: Final = {kind: n for n, kind in enumerate(RelationshipType)}
_COMMERCIAL_MAX: Final = 3


@dataclass(frozen=True, slots=True)
class PairFeatures:
    """Everything the score reads about one (source, target) pair."""

    cosine: float | None
    """Cosine of the current-identity page embeddings; ``None`` without one."""
    source: PageAnalysis
    target: PageAnalysis
    source_links_into_target_market: int
    """Current internal links from the source to pages in the target's market."""
    target_inbound_links: int
    """Current internal links into the target from any current page."""
    lexical_similarity: float | None = None
    """``ts_rank_cd`` the lexical generator measured; recorded, not weighted."""
    graph: GraphSignals | None = None
    """The target's link-graph position (``page_graph_metrics``); recorded, not weighted."""
    orphan_recovery_value: float | None = None
    gsc_opportunity: float | None = None
    ga4_engagement: float | None = None
    """Analytics signals: surfaced and recorded; they never enter ``relevance``."""


@dataclass(frozen=True, slots=True)
class ScoredPair:
    breakdown: ScoreBreakdown
    """Relevance only: ``factors = {}``, ``total == relevance``."""
    reason_codes: tuple[ReasonCode, ...]
    relationship_type: RelationshipType | None
    component_values: Mapping[str, float]
    """Component name -> value, in ``RANKING_COMPONENTS`` order."""
    signals: Mapping[str, float] = field(default_factory=dict)
    """Every measured signal under its own name (module docstring); never summed."""


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _semantic(cosine: float | None, floor: float) -> float:
    if cosine is None or math.isnan(cosine) or math.isnan(floor) or cosine <= floor:
        return 0.0
    span = 1.0 - floor
    return 1.0 if span <= 0.0 else _clamp((cosine - floor) / span)


def geography_match(
    source: PageAnalysis,
    target: PageAnalysis,
    *,
    taxonomy: TaxonomyPolicy,
    scores: Mapping[str, float],
) -> tuple[float, GeographyKey | None]:
    """The geography condition of the pair and its score (``(0.0, None)`` if none).

    The condition is the GATE's structural decision
    (:func:`~ken_services.recommendations.gates.geography_relation`) mapped to a
    ``geography_scores`` key, so the component can never disagree with the gate that decided
    whether the pair may exist at all:

    * ``same_country``: the two PRIMARY geographies are the same country;
    * ``region_contains_source``: the target's primary geography is a region containing the
      source's (taxonomy tree);
    * ``same_region``: the same region, the source's region containing the target's
      geography, or two geographies sharing a (non-global) region;
    * ``global_target``: the target has no specific geography -- and a global SOURCE reaching
      into one geography, the weakest allowed affinity.

    Only the primary geography counts: a country merely mentioned in passing is not a
    geography match. A key missing from ``scores`` is 0.
    """
    decision = geography_relation(source, target, taxonomy=taxonomy)
    if decision is None:
        return (0.0, None)
    key = cast("GeographyKey", GEOGRAPHY_SCORE_KEYS[decision])
    return (_clamp(scores.get(key, 0.0)), key)


def merge_reason_codes(*groups: Iterable[ReasonCode]) -> tuple[ReasonCode, ...]:
    """Every code of every group, once, in :class:`ReasonCode` declaration order."""
    return tuple(
        sorted({code for group in groups for code in group}, key=_REASON_ORDER.__getitem__)
    )


def channel_reason_codes(channels: Iterable[RetrievalChannel]) -> tuple[ReasonCode, ...]:
    """``lexical_overlap`` when the lexical channel retrieved the pair (else ``()``)."""
    return (ReasonCode.LEXICAL_OVERLAP,) if RetrievalChannel.LEXICAL in set(channels) else ()


def anchor_reason_code(anchor_source: str) -> tuple[ReasonCode, ...]:
    """The anchor's reason code: topic alias or title core (keyword anchors add none)."""
    if anchor_source == "target_topic":
        return (ReasonCode.ANCHOR_MATCHES_TARGET_TOPIC,)
    if anchor_source == "target_title":
        return (ReasonCode.ANCHOR_MATCHES_TARGET_TITLE,)
    return ()


def channel_scores_for(scores: Mapping[RetrievalChannel, float]) -> dict[str, float]:
    """``CandidateView.channel_scores``: channel value -> score rounded to 6 places, enum order."""
    return {
        channel.value: round(float(scores[channel]), 6)
        for channel in RetrievalChannel
        if channel in scores
    }


def _signals(
    features: PairFeatures, *, values: Mapping[str, float], topic_relevance: float
) -> dict[str, float]:
    """The separate signals of one pair, each under its own name (only those measured)."""
    out: dict[str, float] = {}
    if features.cosine is not None and not math.isnan(features.cosine):
        out["semantic_similarity"] = max(-1.0, min(1.0, features.cosine))
    if features.lexical_similarity is not None and math.isfinite(features.lexical_similarity):
        out["lexical_similarity"] = features.lexical_similarity
    out["topic_relevance"] = topic_relevance
    out["geography"] = values["geography"]
    out["destination_priority"] = values["commercial"]
    out["underlinked"] = values["underlinked"]
    if features.graph is not None:
        out["graph_in_degree"] = float(features.graph.in_degree)
        out["graph_out_degree"] = float(features.graph.out_degree)
        out["graph_pagerank"] = features.graph.internal_pagerank
        out["graph_hub_score"] = features.graph.hub_score
        out["graph_authority_score"] = features.graph.authority_score
        if features.graph.crawl_depth is not None:
            out["graph_crawl_depth"] = float(features.graph.crawl_depth)
        out["graph_is_orphan"] = 1.0 if features.graph.is_orphan else 0.0
        out["graph_is_bridge"] = 1.0 if features.graph.is_bridge else 0.0
    for name in ("orphan_recovery_value", "gsc_opportunity", "ga4_engagement"):
        value = getattr(features, name)
        if value is not None and math.isfinite(value):
            out[name] = float(value)
    return out


def score_pair(
    features: PairFeatures,
    *,
    policy: RecommendationPolicy,
    ranking: RankingPolicy,
    taxonomy: TaxonomyPolicy,
    semantic_floor: float,
) -> ScoredPair:
    """The relevance breakdown, reason codes and primary relationship of one pair. Pure."""
    source, target = features.source, features.target
    relationships = [
        rel
        for rel in taxonomy_relationships(source, target, taxonomy=taxonomy)
        if rel.relationship_type in MARKET_STRUCTURE_TYPES
    ]
    taxonomy_value = max(
        (RELATIONSHIP_WEIGHTS[r.relationship_type] for r in relationships), default=0.0
    )
    geography_value, geography_key = geography_match(
        source, target, taxonomy=taxonomy, scores=policy.geography_scores
    )
    inbound = max(0, features.target_inbound_links)
    into_market = max(0, features.source_links_into_target_market)
    values: dict[str, float] = {
        "semantic": _semantic(features.cosine, semantic_floor),
        "taxonomy": _clamp(taxonomy_value),
        "intent": _clamp(intent_compatibility(source.intent, target.intent, policy=policy)),
        "graph": 1.0 / (1.0 + into_market),
        "geography": geography_value,
        "commercial": _clamp(target.commercial_importance / _COMMERCIAL_MAX),
        "underlinked": max(0.0, 1.0 - inbound / policy.underlinked_inbound_threshold),
        "historical": 0.0,
    }
    components = tuple(
        ScoreComponent(
            name=name,
            value=values[name],
            weight=ranking.weights[name],
            contribution=values[name] * ranking.weights[name],
        )
        for name in RANKING_COMPONENTS
    )
    relevance = min(1.0, math.fsum(c.contribution for c in components))
    breakdown = ScoreBreakdown(
        components=components,
        relevance=relevance,
        factors={},
        total=relevance,
        semantic_floor=semantic_floor,
        policy_name=ranking.policy,
        policy_version=ranking.version,
    )

    codes: list[ReasonCode] = []
    if values["semantic"] > 0.0:
        codes.append(ReasonCode.SEMANTICALLY_SIMILAR)
    codes.extend(_RELATIONSHIP_CODES[r.relationship_type] for r in relationships)
    if geography_key is not None and geography_key in _GEOGRAPHY_CODES:
        codes.append(_GEOGRAPHY_CODES[geography_key])
    if target.commercial_importance >= _COMMERCIAL_MAX:
        codes.append(ReasonCode.HIGH_COMMERCIAL_VALUE)
    if values["underlinked"] > 0.0:
        codes.append(ReasonCode.UNDERLINKED_TARGET)
    if inbound == 0:
        codes.append(ReasonCode.ORPHAN_TARGET)

    relationship_type: RelationshipType | None = None
    if relationships:
        strongest = min(
            relationships,
            key=lambda r: (-r.weight, _RELATIONSHIP_ORDER[r.relationship_type]),
        )
        relationship_type = strongest.relationship_type
    elif values["semantic"] > 0.0:
        relationship_type = RelationshipType.SEMANTIC_SIMILAR
    elif geography_key is not None:
        relationship_type = _GEOGRAPHY_RELATIONSHIPS.get(geography_key)

    return ScoredPair(
        breakdown=breakdown,
        reason_codes=merge_reason_codes(codes),
        relationship_type=relationship_type,
        component_values=dict(values),
        signals=_signals(features, values=values, topic_relevance=_clamp(taxonomy_value)),
    )


def with_placement(breakdown: ScoreBreakdown, placement_quality: float) -> ScoreBreakdown:
    """The FINAL breakdown: ``factors = {"placement_quality": q}``, ``total = relevance x q``.

    Raises:
        pydantic.ValidationError: ``q`` is not in (0, 1].
    """
    return ScoreBreakdown(
        components=breakdown.components,
        relevance=breakdown.relevance,
        factors={PLACEMENT_FACTOR: placement_quality},
        total=breakdown.relevance * placement_quality,
        semantic_floor=breakdown.semantic_floor,
        policy_name=breakdown.policy_name,
        policy_version=breakdown.policy_version,
    )


@dataclass(frozen=True, slots=True)
class RankingExplanation:
    """Everything a reviewer, an eval or the learning pipeline needs to see about one rank
    (directive section 10): the final score, the full weighted breakdown, the SEPARATE signals,
    both policy versions, the confidence and which generators nominated the candidate."""

    final_score: float
    confidence: float
    calibration_version: str | None
    """``None`` = the confidence is RAW; no threshold may treat it as calibrated."""
    components: tuple[ScoreComponent, ...]
    factors: Mapping[str, float]
    signals: Mapping[str, float]
    ranking_policy_name: str
    ranking_policy_version: int
    recommendation_policy_version: int
    sources: tuple[RetrievalChannel, ...]
    attribution: tuple[CandidateAttribution, ...]


def explain_ranking(
    final: ScoreBreakdown,
    scored: ScoredPair,
    *,
    channels: Iterable[RetrievalChannel],
    attribution: Iterable[CandidateAttribution],
    recommendation_policy_version: int,
) -> RankingExplanation:
    """Package a FINAL breakdown (``with_placement``) with its signals and attribution.

    The confidence is the raw final score until a calibration exists; callers that calibrate
    replace it and set ``calibration_version`` (see ``ken_services.autonomy.calibration``).
    """
    return RankingExplanation(
        final_score=final.total,
        confidence=final.total,
        calibration_version=None,
        components=final.components,
        factors=dict(final.factors),
        signals=dict(scored.signals),
        ranking_policy_name=final.policy_name,
        ranking_policy_version=final.policy_version,
        recommendation_policy_version=recommendation_policy_version,
        sources=tuple(channels),
        attribution=tuple(attribution),
    )
