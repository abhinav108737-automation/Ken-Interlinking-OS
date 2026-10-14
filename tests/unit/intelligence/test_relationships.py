"""Typed taxonomy relationships and semantic similarity candidates."""

from __future__ import annotations

import math

import pytest

from ken_contracts.intelligence import RelationshipType
from ken_services.intelligence.relationships import (
    RELATIONSHIP_WEIGHTS,
    RelationshipCandidate,
    semantic_relationship,
    taxonomy_relationships,
)
from tests.unit.intelligence._support import analysis, mini, v1

T = RelationshipType


def _rels(
    source_market: str | None,
    target_market: str | None,
    *,
    source_geos: tuple[str, ...] = (),
    target_geos: tuple[str, ...] = (),
    mini_policy: bool = False,
) -> dict[RelationshipType, RelationshipCandidate]:
    policy = mini() if mini_policy else v1()
    source = analysis(policy, market_id=source_market, geographies=source_geos, page=1)
    target = analysis(policy, market_id=target_market, geographies=target_geos, page=2)
    found = taxonomy_relationships(source, target, taxonomy=policy)
    kinds = [c.relationship_type for c in found]
    assert len(kinds) == len(set(kinds))
    order = list(RelationshipType)
    assert kinds == sorted(kinds, key=order.index)
    for candidate in found:
        assert 0.0 <= candidate.weight <= 1.0
        assert candidate.weight == RELATIONSHIP_WEIGHTS[candidate.relationship_type]
    return {c.relationship_type: c for c in found}


def test_same_market() -> None:
    rels = _rels("electric-vehicles", "electric-vehicles")
    assert set(rels) == {T.SAME_MARKET}
    assert rels[T.SAME_MARKET].evidence == {
        "source_market": "electric-vehicles",
        "target_market": "electric-vehicles",
    }
    assert rels[T.SAME_MARKET].weight == 1.0


def test_parent_and_submarket() -> None:
    assert set(_rels("electric-two-wheelers", "electric-vehicles")) == {T.PARENT_MARKET}
    assert set(_rels("electric-vehicles", "electric-two-wheelers")) == {T.SUBMARKET}
    # A grandparent is neither.
    assert set(_rels("electric-two-wheelers", "automotive")) == set()


def test_related_market_siblings() -> None:
    rels = _rels("electric-two-wheelers", "ev-charging")
    assert set(rels) == {T.RELATED_MARKET}
    assert rels[T.RELATED_MARKET].evidence["parent_market"] == "electric-vehicles"
    # Two industries are not siblings (no common parent).
    assert T.RELATED_MARKET not in _rels("automotive", "energy")


def test_supply_chain_directions() -> None:
    up = _rels("electric-vehicles", "lithium-ion-batteries")
    assert set(up) == {T.SUPPLY_CHAIN_UPSTREAM}
    down = _rels("lithium-ion-batteries", "electric-vehicles")
    assert set(down) == {T.SUPPLY_CHAIN_DOWNSTREAM}
    assert down[T.SUPPLY_CHAIN_DOWNSTREAM].weight == 0.7


def test_complementary_either_side() -> None:
    assert T.COMPLEMENTARY_MARKET in _rels("solar-power", "energy-storage")
    assert T.COMPLEMENTARY_MARKET in _rels("energy-storage", "solar-power")
    # Declared on smart-widgets only; holds in both directions.
    assert set(_rels("smart-widgets", "gadgets", mini_policy=True)) == {T.COMPLEMENTARY_MARKET}
    assert set(_rels("gadgets", "smart-widgets", mini_policy=True)) == {T.COMPLEMENTARY_MARKET}


def test_child_relationship_can_combine_with_complementary() -> None:
    assert set(_rels("ev-charging", "electric-vehicles")) == {
        T.PARENT_MARKET,
        T.COMPLEMENTARY_MARKET,
    }


def test_industry_adjacency() -> None:
    rels = _rels("solar-power", "electric-vehicles")  # energy <-> automotive
    assert set(rels) == {T.INDUSTRY_ADJACENCY}
    assert rels[T.INDUSTRY_ADJACENCY].evidence == {
        "source_industry": "energy",
        "target_industry": "automotive",
    }
    # Declared on food-ind only; holds both ways.
    assert set(_rels("snacks", "widgets", mini_policy=True)) == {T.INDUSTRY_ADJACENCY}
    assert set(_rels("widgets", "snacks", mini_policy=True)) == {T.INDUSTRY_ADJACENCY}
    # Same industry is never "adjacent".
    assert T.INDUSTRY_ADJACENCY not in _rels("solar-power", "energy-storage")


def test_unrelated_markets() -> None:
    assert _rels("pharmaceuticals", "semiconductors") == {}


def test_same_geography() -> None:
    rels = _rels(None, None, source_geos=("india", "uae"), target_geos=("uae", "india", "qatar"))
    assert set(rels) == {T.SAME_GEOGRAPHY}
    assert rels[T.SAME_GEOGRAPHY].evidence == {"shared_geographies": ["india", "uae"]}
    # Sharing only a region is not same_geography.
    assert _rels(None, None, source_geos=("south-asia",), target_geos=("south-asia",)) == {}


def test_regional_parent() -> None:
    rels = _rels(
        None, None, source_geos=("india", "vietnam"), target_geos=("south-asia", "asia-pacific")
    )
    assert set(rels) == {T.REGIONAL_PARENT}
    assert rels[T.REGIONAL_PARENT].evidence == {
        "source_geographies": ["india", "vietnam"],  # vietnam via asia-pacific
        "target_regions": ["asia-pacific", "south-asia"],
    }
    vietnam = _rels(None, None, source_geos=("vietnam",), target_geos=("asia-pacific",))
    assert set(vietnam) == {T.REGIONAL_PARENT}
    # A region that does not contain the country, or the reverse direction: nothing.
    assert _rels(None, None, source_geos=("india",), target_geos=("middle-east",)) == {}
    assert _rels(None, None, source_geos=("south-asia",), target_geos=("india",)) == {}


def test_market_and_geography_combine() -> None:
    rels = _rels(
        "electric-vehicles", "electric-vehicles", source_geos=("india",), target_geos=("india",)
    )
    assert set(rels) == {T.SAME_MARKET, T.SAME_GEOGRAPHY}


def test_missing_or_unknown_markets_give_no_market_edges() -> None:
    assert _rels(None, "electric-vehicles") == {}
    assert _rels("electric-vehicles", None) == {}
    policy = v1()
    source = analysis(policy, market_id="retired-market", industry_id="retired-ind", page=1)
    target = analysis(policy, market_id="electric-vehicles", page=2)
    assert taxonomy_relationships(source, target, taxonomy=policy) == ()


def test_never_relates_a_page_to_itself() -> None:
    policy = v1()
    page = analysis(policy, market_id="electric-vehicles", geographies=("india",), page=5)
    assert taxonomy_relationships(page, page, taxonomy=policy) == ()


def test_relationships_are_deterministic() -> None:
    policy = v1()
    source = analysis(policy, market_id="ev-charging", geographies=("india",), page=1)
    target = analysis(policy, market_id="electric-vehicles", geographies=("india",), page=2)
    assert taxonomy_relationships(source, target, taxonomy=policy) == taxonomy_relationships(
        source, target, taxonomy=policy
    )


# --------------------------------------------------------------------------- #
# Semantic similarity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("score", "min_score", "weight"),
    [
        (0.9, 0.8, 0.5),
        (1.0, 0.8, 1.0),
        (1.0000001, 0.8, 1.0),  # float noise above 1 is clamped
        (0.55, 0.5, 0.1),
        (0.5, 0.0, 0.5),
    ],
)
def test_semantic_weight(score: float, min_score: float, weight: float) -> None:
    candidate = semantic_relationship(score, min_score=min_score)
    assert candidate is not None
    assert candidate.relationship_type is T.SEMANTIC_SIMILAR
    assert math.isclose(candidate.weight, weight, abs_tol=1e-9)
    assert candidate.evidence == {"cosine": round(score, 4)}


@pytest.mark.parametrize(
    ("score", "min_score"),
    [(0.8, 0.8), (0.79, 0.8), (-1.0, 0.5), (float("nan"), 0.5), (1.0, 1.0)],
)
def test_semantic_at_or_below_threshold(score: float, min_score: float) -> None:
    assert semantic_relationship(score, min_score=min_score) is None


def test_semantic_evidence_is_rounded() -> None:
    candidate = semantic_relationship(0.876543, min_score=0.5)
    assert candidate is not None
    assert candidate.evidence == {"cosine": 0.8765}
