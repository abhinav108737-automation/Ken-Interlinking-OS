"""ADR-0011 section 4: every score component, the identities, reason codes, relationship."""

from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import ValidationError

from ken_contracts.intelligence import Intent, PageAnalysis, RelationshipType
from ken_contracts.recommendations import (
    RANKING_COMPONENTS,
    ReasonCode,
    RetrievalChannel,
    ScoreBreakdown,
)
from ken_services.recommendations.scoring import (
    MARKET_STRUCTURE_TYPES,
    PairFeatures,
    ScoredPair,
    anchor_reason_code,
    channel_reason_codes,
    channel_scores_for,
    geography_match,
    merge_reason_codes,
    score_pair,
    with_placement,
)
from tests.unit.recommendations._support import (
    analysis,
    policy,
    policy_with,
    ranking,
    ranking_with,
    taxonomy,
)

C = ReasonCode


def _score(
    source: PageAnalysis | None = None,
    target: PageAnalysis | None = None,
    *,
    cosine: float | None = None,
    into_market: int = 0,
    inbound: int = 5,
    floor: float = 0.5,
    **kwargs: Any,
) -> ScoredPair:
    return score_pair(
        PairFeatures(
            cosine=cosine,
            source=source or analysis(intent=Intent.INFORMATIONAL, commercial_importance=2),
            target=target or analysis(commercial_importance=1),
            source_links_into_target_market=into_market,
            target_inbound_links=inbound,
        ),
        policy=kwargs.get("policy", policy()),
        ranking=kwargs.get("ranking", ranking()),
        taxonomy=taxonomy(),
        semantic_floor=floor,
    )


def _value(scored: ScoredPair, name: str) -> float:
    return scored.component_values[name]


# --------------------------------------------------------------------------- #
# Breakdown identities and policy weights
# --------------------------------------------------------------------------- #
def test_breakdown_lists_every_component_in_order_with_policy_weights() -> None:
    scored = _score(cosine=0.8)
    weights = ranking().weights
    assert tuple(c.name for c in scored.breakdown.components) == RANKING_COMPONENTS
    assert tuple(scored.component_values) == RANKING_COMPONENTS
    for component in scored.breakdown.components:
        assert component.weight == weights[component.name]
        assert component.value == scored.component_values[component.name]
        assert math.isclose(component.contribution, component.value * component.weight)


def test_relevance_is_the_weighted_sum_and_total_equals_relevance() -> None:
    scored = _score(cosine=0.8)
    b = scored.breakdown
    assert b.factors == {}
    assert b.total == b.relevance
    assert math.isclose(b.relevance, math.fsum(c.contribution for c in b.components), abs_tol=1e-9)
    # The record re-validates from its own dump (both identities hold).
    assert ScoreBreakdown.model_validate(b.model_dump()) == b
    assert (b.policy_name, b.policy_version, b.semantic_floor) == ("ranking", 1, 0.5)


def test_weights_come_from_the_ranking_policy() -> None:
    only_semantic = ranking_with({"semantic": 1.0})
    scored = _score(cosine=0.75, floor=0.5, ranking=only_semantic)
    assert math.isclose(scored.breakdown.relevance, 0.5)
    assert scored.breakdown.policy_version == 9
    only_commercial = ranking_with({"commercial": 1.0})
    scored = _score(target=analysis(commercial_importance=2), ranking=only_commercial)
    assert math.isclose(scored.breakdown.relevance, 2 / 3)


def test_a_perfect_pair_tops_out_at_097_until_historical_exists() -> None:
    india = ["india"]
    source = analysis(market_id="solar-power", geographies=india, intent=Intent.INFORMATIONAL)
    target = analysis(market_id="solar-power", geographies=india, commercial_importance=3)
    scored = _score(source, target, cosine=1.0, inbound=0, into_market=0)
    assert all(scored.component_values[n] == 1.0 for n in RANKING_COMPONENTS[:-1])
    assert scored.component_values["historical"] == 0.0
    assert math.isclose(scored.breakdown.relevance, 0.97)


# --------------------------------------------------------------------------- #
# semantic
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("cosine", "floor", "expected"),
    [
        (None, 0.5, 0.0),  # no embedding
        (float("nan"), 0.5, 0.0),
        (0.5, 0.5, 0.0),  # at the floor
        (0.3, 0.5, 0.0),  # below the floor (clamped, never negative)
        (0.75, 0.5, 0.5),
        (1.0, 0.5, 1.0),
        (1.2, 0.5, 1.0),  # clamped above
        (0.4, -0.2, 0.5),
        (1.0, 1.0, 0.0),  # floor 1: nothing is above it
        (0.9, float("nan"), 0.0),
    ],
)
def test_semantic_component(cosine: float | None, floor: float, expected: float) -> None:
    scored = _score(cosine=cosine, floor=floor)
    assert math.isclose(_value(scored, "semantic"), expected)
    assert (C.SEMANTICALLY_SIMILAR in scored.reason_codes) is (expected > 0)


def test_semantic_floor_above_one_cannot_divide_by_zero() -> None:
    assert _value(_score(cosine=1.5, floor=1.2), "semantic") == 1.0


# --------------------------------------------------------------------------- #
# taxonomy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("source_market", "target_market", "value", "codes", "relationship"),
    [
        ("solar-power", "solar-power", 1.0, {C.SAME_MARKET}, RelationshipType.SAME_MARKET),
        ("solar-power", "renewable-energy", 0.8, {C.PARENT_MARKET}, RelationshipType.PARENT_MARKET),
        ("renewable-energy", "solar-power", 0.8, {C.SUBMARKET}, RelationshipType.SUBMARKET),
        ("solar-power", "wind-power", 0.6, {C.RELATED_MARKET}, RelationshipType.RELATED_MARKET),
        (
            "energy-storage",
            "lithium-ion-batteries",
            0.7,
            {C.SUPPLY_CHAIN, C.INDUSTRY_ADJACENCY},
            RelationshipType.SUPPLY_CHAIN_UPSTREAM,
        ),
        (
            "lithium-ion-batteries",
            "electric-vehicles",
            0.7,
            {C.SUPPLY_CHAIN},
            RelationshipType.SUPPLY_CHAIN_DOWNSTREAM,
        ),
        (
            "solar-power",
            "energy-storage",
            0.6,
            {C.COMPLEMENTARY_MARKET},
            RelationshipType.COMPLEMENTARY_MARKET,
        ),
        (
            "electric-vehicles",
            "ev-charging",
            0.8,
            {C.SUBMARKET, C.COMPLEMENTARY_MARKET},
            RelationshipType.SUBMARKET,
        ),
        (
            "solar-power",
            "auto-components",
            0.3,
            {C.INDUSTRY_ADJACENCY},
            RelationshipType.INDUSTRY_ADJACENCY,
        ),
        ("solar-power", "fintech", 0.0, set(), None),
    ],
)
def test_taxonomy_component(
    source_market: str,
    target_market: str,
    value: float,
    codes: set[ReasonCode],
    relationship: RelationshipType | None,
) -> None:
    scored = _score(analysis(market_id=source_market), analysis(market_id=target_market))
    assert _value(scored, "taxonomy") == value
    market_codes = {
        C.SAME_MARKET,
        C.PARENT_MARKET,
        C.SUBMARKET,
        C.RELATED_MARKET,
        C.SUPPLY_CHAIN,
        C.COMPLEMENTARY_MARKET,
        C.INDUSTRY_ADJACENCY,
    }
    assert set(scored.reason_codes) & market_codes == codes
    assert scored.relationship_type is relationship


def test_geography_relationships_never_feed_the_taxonomy_component() -> None:
    assert RelationshipType.SAME_GEOGRAPHY not in MARKET_STRUCTURE_TYPES
    assert RelationshipType.REGIONAL_PARENT not in MARKET_STRUCTURE_TYPES
    scored = _score(analysis(geographies=["india"]), analysis(geographies=["india"]))
    assert _value(scored, "taxonomy") == 0.0
    assert _value(scored, "geography") == 1.0


def test_unknown_market_ids_score_zero_taxonomy() -> None:
    source = analysis(market_id="no-such-market", industry_id="nowhere")
    scored = _score(source, analysis(market_id="solar-power"))
    assert _value(scored, "taxonomy") == 0.0


def test_same_page_scores_no_relationship() -> None:
    same = analysis(market_id="solar-power")
    assert _value(_score(same, same), "taxonomy") == 0.0


# --------------------------------------------------------------------------- #
# intent, graph, commercial, underlinked, historical
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("source_intent", "target_intent", "expected"),
    [
        (Intent.INFORMATIONAL, Intent.COMMERCIAL_RESEARCH, 1.0),
        (Intent.COMMERCIAL_RESEARCH, Intent.INFORMATIONAL, 0.7),
        (Intent.UNKNOWN, Intent.INFORMATIONAL, 0.5),
        (Intent.CORPORATE, Intent.INFORMATIONAL, 0.0),
        (Intent.NAVIGATIONAL, Intent.NAVIGATIONAL, 0.0),
    ],
)
def test_intent_component(source_intent: Intent, target_intent: Intent, expected: float) -> None:
    scored = _score(analysis(intent=source_intent), analysis(intent=target_intent))
    assert _value(scored, "intent") == expected


@pytest.mark.parametrize(("links", "expected"), [(0, 1.0), (1, 0.5), (3, 0.25), (-4, 1.0)])
def test_graph_complementarity(links: int, expected: float) -> None:
    assert _value(_score(into_market=links), "graph") == expected


@pytest.mark.parametrize(("importance", "expected"), [(0, 0.0), (1, 1 / 3), (2, 2 / 3), (3, 1.0)])
def test_commercial_component(importance: int, expected: float) -> None:
    scored = _score(target=analysis(commercial_importance=importance))
    assert math.isclose(_value(scored, "commercial"), expected)
    assert (C.HIGH_COMMERCIAL_VALUE in scored.reason_codes) is (importance == 3)


@pytest.mark.parametrize(
    ("inbound", "expected", "underlinked", "orphan"),
    [(0, 1.0, True, True), (1, 2 / 3, True, False), (2, 1 / 3, True, False), (3, 0.0, False, False),
     (10, 0.0, False, False), (-1, 1.0, True, True)],
)  # fmt: skip
def test_underlinked_component(
    inbound: int, expected: float, underlinked: bool, orphan: bool
) -> None:
    scored = _score(inbound=inbound)
    assert math.isclose(_value(scored, "underlinked"), expected)
    assert (C.UNDERLINKED_TARGET in scored.reason_codes) is underlinked
    assert (C.ORPHAN_TARGET in scored.reason_codes) is orphan


def test_underlinked_threshold_comes_from_policy() -> None:
    scored = _score(inbound=3, policy=policy_with(underlinked_inbound_threshold=6))
    assert math.isclose(_value(scored, "underlinked"), 0.5)


def test_historical_is_always_zero() -> None:
    assert _value(_score(cosine=1.0, inbound=0), "historical") == 0.0


# --------------------------------------------------------------------------- #
# geography
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("source_geos", "target_geos", "value", "key", "code"),
    [
        (["india"], ["india", "vietnam"], 1.0, "same_country", C.SAME_GEOGRAPHY),
        (["india"], ["south-asia"], 0.6, "region_contains_source", C.REGIONAL_CONTEXT),
        (["india"], ["asia-pacific"], 0.6, "region_contains_source", C.REGIONAL_CONTEXT),
        (["southeast-asia"], ["southeast-asia"], 0.5, "same_region", C.REGIONAL_CONTEXT),
        (["india"], [], 0.3, "global_target", None),
        ([], [], 0.3, "global_target", None),
        # Two countries of one region: the gate allows the pair only when the source names
        # the target's geography, and the component reports the shared region either way.
        (["india"], ["vietnam"], 0.5, "same_region", C.REGIONAL_CONTEXT),
        # A global source reaching into one country: the weakest allowed affinity.
        ([], ["india"], 0.3, "global_target", None),
        (["india"], ["europe"], 0.0, None, None),
        (["india"], ["germany"], 0.0, None, None),
    ],
)
def test_geography_component(
    source_geos: list[str],
    target_geos: list[str],
    value: float,
    key: str | None,
    code: ReasonCode | None,
) -> None:
    source, target = analysis(geographies=source_geos), analysis(geographies=target_geos)
    scored = _score(source, target)
    assert _value(scored, "geography") == value
    assert geography_match(
        source, target, taxonomy=taxonomy(), scores=policy().geography_scores
    ) == (value, key)
    geo_codes = set(scored.reason_codes) & {C.SAME_GEOGRAPHY, C.REGIONAL_CONTEXT}
    assert geo_codes == ({code} if code else set())


def test_only_the_primary_geography_decides_the_component() -> None:
    """The condition follows the PRIMARY geography of each page, not every listed one: the
    target below is an India page that also mentions South Asia, so the pair is
    ``same_country`` and the (higher) regional score never applies."""
    source, target = analysis(geographies=["india"]), analysis(geographies=["india", "south-asia"])
    scores = {"same_country": 0.2, "region_contains_source": 0.9}
    assert geography_match(source, target, taxonomy=taxonomy(), scores=scores) == (
        0.2,
        "same_country",
    )
    # The CONDITION is structural; a key the policy does not score is simply worth 0.
    assert geography_match(source, target, taxonomy=taxonomy(), scores={}) == (
        0.0,
        "same_country",
    )


# --------------------------------------------------------------------------- #
# Relationship fallbacks and reason-code order
# --------------------------------------------------------------------------- #
def test_relationship_falls_back_to_semantic_then_geography() -> None:
    plain = analysis(geographies=["vietnam"]), analysis(geographies=["india"])
    assert _score(*plain, cosine=0.9).relationship_type is RelationshipType.SEMANTIC_SIMILAR
    assert _score(*plain).relationship_type is None
    india = analysis(geographies=["india"])
    assert _score(india, analysis(geographies=["india"])).relationship_type is (
        RelationshipType.SAME_GEOGRAPHY
    )
    assert _score(india, analysis(geographies=["south-asia"])).relationship_type is (
        RelationshipType.REGIONAL_PARENT
    )
    sea = analysis(geographies=["southeast-asia"])
    assert _score(sea, analysis(geographies=["southeast-asia"])).relationship_type is None


def test_market_relationship_beats_semantic() -> None:
    scored = _score(analysis(market_id="solar-power"), analysis(market_id="wind-power"), cosine=1)
    assert scored.relationship_type is RelationshipType.RELATED_MARKET


def test_reason_codes_are_in_declaration_order() -> None:
    source = analysis(market_id="electric-vehicles", geographies=["india"])
    target = analysis(market_id="ev-charging", geographies=["india"], commercial_importance=3)
    scored = _score(source, target, cosine=0.9, inbound=0)
    assert scored.reason_codes == (
        C.SEMANTICALLY_SIMILAR,
        C.SUBMARKET,
        C.COMPLEMENTARY_MARKET,
        C.SAME_GEOGRAPHY,
        C.HIGH_COMMERCIAL_VALUE,
        C.UNDERLINKED_TARGET,
        C.ORPHAN_TARGET,
    )


def test_scoring_is_deterministic() -> None:
    source = analysis(market_id="solar-power", geographies=["india"])
    target = analysis(market_id="energy-storage", geographies=["south-asia"])
    assert _score(source, target, cosine=0.7) == _score(source, target, cosine=0.7)


# --------------------------------------------------------------------------- #
# Placement factor and helpers
# --------------------------------------------------------------------------- #
def test_with_placement_multiplies_in_the_quality() -> None:
    relevance = _score(cosine=0.9, inbound=0).breakdown
    final = with_placement(relevance, 0.9)
    assert final.factors == {"placement_quality": 0.9}
    assert math.isclose(final.total, relevance.relevance * 0.9)
    assert final.relevance == relevance.relevance
    assert final.components == relevance.components
    assert final.semantic_floor == relevance.semantic_floor
    assert ScoreBreakdown.model_validate(final.model_dump()) == final
    # Applying it to an already-placed breakdown replaces (never compounds) the factor.
    assert with_placement(final, 0.75).factors == {"placement_quality": 0.75}


@pytest.mark.parametrize("quality", [0.0, -0.1, 1.01])
def test_with_placement_rejects_a_quality_outside_0_1(quality: float) -> None:
    with pytest.raises(ValidationError):
        with_placement(_score().breakdown, quality)


def test_merge_and_extra_reason_code_helpers() -> None:
    merged = merge_reason_codes(
        [C.ORPHAN_TARGET, C.SAME_MARKET],
        channel_reason_codes([RetrievalChannel.GRAPH, RetrievalChannel.LEXICAL]),
        anchor_reason_code("target_topic"),
        [C.SAME_MARKET],
    )
    assert merged == (
        C.LEXICAL_OVERLAP,
        C.SAME_MARKET,
        C.ORPHAN_TARGET,
        C.ANCHOR_MATCHES_TARGET_TOPIC,
    )
    assert channel_reason_codes([RetrievalChannel.SEMANTIC]) == ()
    assert anchor_reason_code("target_title") == (C.ANCHOR_MATCHES_TARGET_TITLE,)
    assert anchor_reason_code("target_keyword") == ()
    assert merge_reason_codes() == ()


def test_channel_scores_for_is_ordered_and_rounded() -> None:
    scores = {RetrievalChannel.GRAPH: 1, RetrievalChannel.SEMANTIC: 0.123456789}
    assert channel_scores_for(scores) == {"semantic": 0.123457, "graph": 1.0}
    assert list(channel_scores_for(scores)) == ["semantic", "graph"]
