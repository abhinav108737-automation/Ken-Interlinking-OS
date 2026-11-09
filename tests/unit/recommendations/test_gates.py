"""The hard gates: primary geography, the geography truth table, the market gate.

Every row of the table in ``ken_services.recommendations.gates`` is asserted in BOTH
directions, and the product owner's two concrete rejections are regression tests on the LIVE
(v3) taxonomy: they must now be refused.
"""

from __future__ import annotations

import pytest

from ken_contracts.intelligence import PageAnalysis, RelationshipType
from ken_contracts.recommendations import ReasonCode
from ken_services.recommendations.gates import (
    GeographyDecision,
    geography_gate,
    geography_terms,
    market_gate,
    mentions_geography,
    primary_geography,
)
from ken_services.recommendations.placement import market_core, market_keyword_anchors
from tests.unit.recommendations._support import analysis, policy, taxonomy, taxonomy_v3

D = GeographyDecision
NO_MENTION = "A paragraph about the market with no place name in it at all."


def _gate(
    source: PageAnalysis, target: PageAnalysis, text: str = NO_MENTION
) -> GeographyDecision | None:
    verdict = geography_gate(source, target, taxonomy=taxonomy(), source_text=text)
    return None if verdict is None else verdict.decision


def _page(*geographies: str, **kwargs: object) -> PageAnalysis:
    return analysis(geographies=list(geographies), **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Primary geography
# --------------------------------------------------------------------------- #
def test_the_primary_geography_is_the_highest_scored_mention() -> None:
    page = analysis(geographies=["india", "vietnam"], geography_scores=[2.0, 9.0])
    node = primary_geography(page, taxonomy=taxonomy())
    assert node is not None and node.id == "vietnam"


def test_ties_break_on_geo_id_deterministically() -> None:
    first = analysis(geographies=["vietnam", "india"], geography_scores=[4.0, 4.0])
    second = analysis(geographies=["india", "vietnam"], geography_scores=[4.0, 4.0])
    for page in (first, second):
        node = primary_geography(page, taxonomy=taxonomy())
        assert node is not None and node.id == "india"


def test_a_page_without_or_beyond_the_taxonomy_has_no_primary_geography() -> None:
    assert primary_geography(analysis(), taxonomy=taxonomy()) is None
    # v1 has no Kenya: an analysis from another taxonomy version adds no geography here.
    v3_page = analysis(geographies=["kenya"], tax=taxonomy_v3())
    assert primary_geography(v3_page, taxonomy=taxonomy()) is None


def test_an_incidental_mention_is_not_a_geography_match() -> None:
    """The regression behind the whole gate: an Africa page that cites India once used to
    count as ``same_country`` with an India page."""
    cashew = analysis(geographies=["indonesia", "india"], geography_scores=[12.0, 1.0])
    dry_fruits = analysis(geographies=["india"])
    assert _gate(cashew, dry_fruits) is None


# --------------------------------------------------------------------------- #
# The geography truth table, both directions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("source_geos", "target_geos", "forward", "backward"),
    [
        # same country / same region node
        (("india",), ("india",), D.SAME_COUNTRY, D.SAME_COUNTRY),
        (("south-asia",), ("south-asia",), D.SAME_REGION, D.SAME_REGION),
        # country <-> a region that contains it
        (
            ("india",),
            ("south-asia",),
            D.TARGET_REGION_CONTAINS_SOURCE,
            D.SOURCE_REGION_CONTAINS_TARGET,
        ),
        (
            ("india",),
            ("asia-pacific",),
            D.TARGET_REGION_CONTAINS_SOURCE,
            D.SOURCE_REGION_CONTAINS_TARGET,
        ),
        # region <-> a region inside it
        (
            ("south-asia",),
            ("asia-pacific",),
            D.TARGET_REGION_CONTAINS_SOURCE,
            D.SOURCE_REGION_CONTAINS_TARGET,
        ),
        # any -> global target (and a global source needs the mention: refused here)
        (("india",), (), D.GLOBAL_TARGET, None),
        ((), (), D.GLOBAL_TARGET, D.GLOBAL_TARGET),
        # different countries of one region: refused without the mention
        (("india",), ("bangladesh",), None, None),
        (("india",), ("vietnam",), None, None),
        # different region
        (("india",), ("germany",), None, None),
        (("north-america",), ("asia-pacific",), None, None),
        (("united-states",), ("vietnam",), None, None),
    ],
)
def test_geography_truth_table(
    source_geos: tuple[str, ...],
    target_geos: tuple[str, ...],
    forward: GeographyDecision | None,
    backward: GeographyDecision | None,
) -> None:
    source, target = _page(*source_geos), _page(*target_geos)
    assert _gate(source, target) is forward
    assert _gate(target, source) is backward


@pytest.mark.parametrize(
    ("source_geos", "target_geos", "mention", "decision"),
    [
        (("india",), ("bangladesh",), "Bangladesh demand is rising.", D.CROSS_GEOGRAPHY_MENTIONED),
        (("india",), ("vietnam",), "Vietnamese exporters compete.", D.CROSS_GEOGRAPHY_MENTIONED),
        ((), ("india",), "Volumes in India lead the region.", D.GLOBAL_SOURCE_MENTIONED),
        # Still refused: a mention cannot bridge two REGIONS.
        (("india",), ("germany",), "German brands are present.", None),
        # A mention of the wrong place proves nothing.
        (("india",), ("vietnam",), "Thailand is the larger market.", None),
    ],
)  # fmt: skip
def test_a_mention_opens_the_conditional_rows_only(
    source_geos: tuple[str, ...],
    target_geos: tuple[str, ...],
    mention: str,
    decision: GeographyDecision | None,
) -> None:
    assert _gate(_page(*source_geos), _page(*target_geos), mention) is decision


def test_a_mention_must_be_a_whole_phrase_not_a_substring() -> None:
    """ "Indiana" is not India, and a short ambiguous alias ("sea region") is not evidence."""
    assert _gate(_page("india"), _page("vietnam"), "Vietnamese is spoken") is (
        D.CROSS_GEOGRAPHY_MENTIONED
    )
    assert _gate(_page("vietnam"), _page("india"), "Indiana is a US state") is None


def test_the_anchor_must_name_the_target_geography_only_when_it_differs() -> None:
    tax = taxonomy()
    labelled = {
        D.SOURCE_REGION_CONTAINS_TARGET,
        D.CROSS_GEOGRAPHY_MENTIONED,
        D.GLOBAL_SOURCE_MENTIONED,
    }
    cases = [
        (_page("india"), _page("india"), NO_MENTION),
        (_page("india"), _page("south-asia"), NO_MENTION),
        (_page("india"), _page(), NO_MENTION),
        (_page("south-asia"), _page("india"), NO_MENTION),
        (_page("india"), _page("vietnam"), "Vietnam also exports."),
        (_page(), _page("india"), "India leads."),
    ]
    for source, target, text in cases:
        verdict = geography_gate(source, target, taxonomy=tax, source_text=text)
        assert verdict is not None
        assert verdict.anchor_must_name_target_geography is (verdict.decision in labelled)


def test_the_verdict_explains_itself_with_reason_codes() -> None:
    tax = taxonomy()
    same = geography_gate(_page("india"), _page("india"), taxonomy=tax, source_text=NO_MENTION)
    glob = geography_gate(_page("india"), _page(), taxonomy=tax, source_text=NO_MENTION)
    cross = geography_gate(
        _page("india"), _page("vietnam"), taxonomy=tax, source_text="Vietnam too."
    )
    assert same is not None and same.reason_codes == (ReasonCode.SAME_GEOGRAPHY,)
    assert glob is not None and glob.reason_codes == (ReasonCode.GLOBAL_TARGET,)
    assert cross is not None and cross.reason_codes == (ReasonCode.CROSS_GEOGRAPHY_SUPPORTED,)
    assert same.geography_score_key == "same_country"
    assert glob.geography_score_key == "global_target"
    assert cross.geography_score_key == "same_region"


# --------------------------------------------------------------------------- #
# Regional siblings (ADR-0027 §1 amendment, owner 2026-11-01)
# --------------------------------------------------------------------------- #
def _sibling_gate(
    source: PageAnalysis, target: PageAnalysis, *, same_core: bool, text: str = NO_MENTION
) -> GeographyDecision | None:
    verdict = geography_gate(
        source, target, taxonomy=taxonomy(), source_text=text, same_market_core=same_core
    )
    return None if verdict is None else verdict.decision


@pytest.mark.parametrize(
    ("source_geos", "target_geos"),
    [
        (("india",), ("germany",)),
        (("north-america",), ("asia-pacific",)),
        (("united-states",), ("vietnam",)),
        (("india",), ("united-kingdom",)),
        # Two countries of ONE region: a sibling, NOT the conditional row. Without this the
        # rule would demand a Malaysia report write the word "Indonesia".
        (("malaysia",), ("indonesia",)),
        (("india",), ("bangladesh",)),
        (("saudi-arabia",), ("qatar",)),
    ],
)
def test_the_same_market_in_another_region_is_a_regional_sibling(
    source_geos: tuple[str, ...], target_geos: tuple[str, ...]
) -> None:
    """The owner's first priority: "Germany EV market" -> "India EV market".

    Each of these pairs is refused by the pre-amendment table (see
    ``test_geography_truth_table``); the ONLY thing that admits them is the market core.
    """
    source, target = _page(*source_geos), _page(*target_geos)
    assert _sibling_gate(source, target, same_core=True) is D.REGIONAL_SIBLING
    assert _sibling_gate(target, source, same_core=True) is D.REGIONAL_SIBLING


@pytest.mark.parametrize(
    ("source_geos", "target_geos"),
    [
        (("india",), ("germany",)),
        (("north-america",), ("asia-pacific",)),
        (("indonesia",), ("india",)),
    ],
)
def test_a_different_market_across_regions_is_still_refused(
    source_geos: tuple[str, ...], target_geos: tuple[str, ...]
) -> None:
    """The amendment must not become "any cross-region link is fine".

    The last row is the owner's original rejection (Indonesia natural personal care ->
    India cosmetics): different markets, so no market core matches and it stays refused.
    """
    source, target = _page(*source_geos), _page(*target_geos)
    assert _sibling_gate(source, target, same_core=False) is None
    assert _sibling_gate(target, source, same_core=False) is None


def test_a_regional_sibling_must_name_the_target_geography_in_the_anchor() -> None:
    """The Brazil case. This is the only guard on a sibling link, so it is load-bearing:
    a reader on a page reading "Brazil Niche Fragrance Market" must not be sent to the
    GLOBAL fragrance report on a bare "fragrance market"."""
    verdict = geography_gate(
        _page("india"),
        _page("germany"),
        taxonomy=taxonomy(),
        source_text=NO_MENTION,
        same_market_core=True,
    )
    assert verdict is not None
    assert verdict.decision is D.REGIONAL_SIBLING
    assert verdict.anchor_must_name_target_geography is True
    assert verdict.reason_codes == (ReasonCode.REGIONAL_SIBLING_MARKET,)

    # ... and with that flag the generator offers ONLY geography-qualified anchors.
    assert verdict.target_geography is not None
    anchors = market_keyword_anchors(
        target_url="https://www.kenresearch.com/industry-reports/germany-ev-market",
        target_title="Germany EV Market Outlook to 2030",
        taxonomy=taxonomy(),
        policy=policy(),
        geography_terms=geography_terms(verdict.target_geography),
        require_geography=verdict.anchor_must_name_target_geography,
    )
    assert anchors, "a sibling link needs at least one geography-qualified anchor"
    # Any term of the node qualifies, label or alias: "german EV market" names Germany just
    # as "Germany EV market" does.
    terms = [t.casefold() for t in geography_terms(verdict.target_geography)]
    assert all(any(t in a.text.casefold() for t in terms) for a in anchors), [
        a.text for a in anchors
    ]


def test_a_sibling_needs_no_mention_in_the_source_text() -> None:
    """Unlike the conditional rows, a sibling is structural: the anchor names the geography,
    so the body text need not. Without this the 962 measured sibling sets stay unreachable,
    because a Germany report rarely writes the word "India"."""
    assert _sibling_gate(_page("india"), _page("germany"), same_core=True) is D.REGIONAL_SIBLING
    assert (
        _sibling_gate(_page("india"), _page("germany"), same_core=False, text="German brands.")
        is None
    )


def test_market_core_groups_the_same_market_across_geographies() -> None:
    tax = taxonomy()

    def core(url: str, title: str) -> str | None:
        return market_core(target_url=url, target_title=title, taxonomy=tax)

    germany = core("/industry-reports/germany-ev-market", "Germany EV Market Outlook")
    india = core("/industry-reports/india-ev-market", "India EV Market Outlook")
    charging = core(
        "/industry-reports/india-ev-charging-market", "India EV Charging Market Outlook"
    )
    assert germany is not None and germany == india
    # A component market is NOT the same market: it is Priority 2 (adjacency), and must not
    # be smuggled in as a regional sibling.
    assert charging != germany


def test_title_core_decodes_html_entities() -> None:
    """Titles arrive with entities intact; an anchor must never read "&amp;"."""
    assert market_core(
        target_url=None,
        target_title="Top Mobile Financial Services &amp; Neobanking Players",
        taxonomy=taxonomy(),
    ) == market_core(
        target_url=None,
        target_title="Top Mobile Financial Services & Neobanking Players",
        taxonomy=taxonomy(),
    )
    assert "amp" not in (
        market_core(
            target_url=None,
            target_title="Edible Oil Manufacturing &amp; Refining Market",
            taxonomy=taxonomy(),
        )
        or ""
    )


def test_geography_terms_drop_short_ambiguous_aliases() -> None:
    node = taxonomy_v3().geographies
    sea = next(g for g in node if g.id == "southeast-asia")
    terms = geography_terms(sea)
    assert "Southeast Asia" in terms and "asean" in terms
    assert "sea" not in terms  # "at sea" must not make a page a Southeast Asia page
    assert mentions_geography("Growth across Southeast Asia is strong.", sea)
    assert not mentions_geography("Shipments by sea are cheaper.", sea)


# --------------------------------------------------------------------------- #
# Market gate
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("source_market", "target_market", "expected"),
    [
        ("solar-power", "solar-power", (RelationshipType.SAME_MARKET,)),
        (
            "ev-charging",
            "electric-vehicles",
            (RelationshipType.PARENT_MARKET, RelationshipType.COMPLEMENTARY_MARKET),
        ),
        (
            "electric-vehicles",
            "ev-charging",
            (RelationshipType.SUBMARKET, RelationshipType.COMPLEMENTARY_MARKET),
        ),
        (
            "electric-vehicles",
            "lithium-ion-batteries",
            (RelationshipType.SUPPLY_CHAIN_UPSTREAM,),
        ),
        (
            "lithium-ion-batteries",
            "energy-storage",
            (RelationshipType.SUPPLY_CHAIN_DOWNSTREAM,),
        ),
        ("solar-power", "energy-storage", (RelationshipType.COMPLEMENTARY_MARKET,)),
        # Siblings are NOT relatable: a shared parent is not a shared subject.
        ("solar-power", "wind-power", ()),
        # Industry adjacency is far too coarse to justify a link.
        ("energy", "automotive", ()),
        # No relation at all, and an unknown market on either side.
        ("electric-vehicles", "cold-chain", ()),
        (None, "cold-chain", ()),
        ("cold-chain", None, ()),
    ],
)
def test_market_gate_allows_only_declared_relations(
    source_market: str | None, target_market: str | None, expected: tuple[RelationshipType, ...]
) -> None:
    source = analysis(market_id=source_market)
    target = analysis(market_id=target_market)
    found = market_gate(source, target, taxonomy=taxonomy())
    assert tuple(found) == expected


def test_word_overlap_never_relates_two_markets() -> None:
    """ "nut and seeds" (edible) and "vegetable seed" (planting) share a word and a parent."""
    tax = taxonomy_v3()
    nuts = analysis(market_id="nut-and-seeds", tax=tax)
    seed = analysis(market_id="vegetable-seed", tax=tax)
    assert market_gate(nuts, seed, taxonomy=tax) == ()
    assert market_gate(seed, nuts, taxonomy=tax) == ()


# --------------------------------------------------------------------------- #
# The product owner's two rejections (regressions)
# --------------------------------------------------------------------------- #
def test_indonesia_personal_care_does_not_link_to_india_cosmetics() -> None:
    """Rejected: a reader researching Indonesia does not want India, and "cosmetics" is not a
    rankable anchor."""
    tax = taxonomy_v3()
    source = analysis(market_id="natural-personal-care", geographies=["indonesia"], tax=tax)
    target = analysis(market_id="cosmetics", geographies=["india"], tax=tax)
    body = "Indonesian demand for cosmetics and natural personal care keeps growing."

    # Geography: Indonesia and India share only a region, and the page never names India.
    assert geography_gate(source, target, taxonomy=tax, source_text=body) is None
    # Market: siblings under consumer retail are not relatable.
    assert market_gate(source, target, taxonomy=tax) == ()
    # Anchor: even naming India, the anchor is the target's market keyword, never "cosmetics".
    named = "The India cosmetics market is far larger than Indonesia's."
    verdict = geography_gate(source, target, taxonomy=tax, source_text=named)
    assert verdict is not None and verdict.anchor_must_name_target_geography
    anchors = market_keyword_anchors(
        target_url="https://www.kenresearch.com/industry-reports/india-cosmetics-market",
        target_title="India Cosmetics Market Share, Companies & Trends Report 2025-2032",
        taxonomy=tax,
        policy=policy(),
        geography_terms=geography_terms(verdict.target_geography),  # type: ignore[arg-type]
        require_geography=True,
    )
    texts = [phrase.text for phrase in anchors]
    # The target's own spelling, geography first; the bare noun is never offered.
    assert texts == ["India Cosmetics market", "indian Cosmetics market"]
    assert "cosmetics" not in texts and "cosmetics market" not in texts


def test_north_america_nuts_does_not_link_to_asia_pacific_vegetable_seed() -> None:
    """Rejected: edible seed vs planting seed, different regions, and "seed" is not the
    market keyword."""
    tax = taxonomy_v3()
    source = analysis(market_id="nut-and-seeds", geographies=["north-america"], tax=tax)
    target = analysis(market_id="vegetable-seed", geographies=["asia-pacific"], tax=tax)
    body = "North American processors buy nuts and seeds from across Asia Pacific."

    assert geography_gate(source, target, taxonomy=tax, source_text=body) is None
    assert market_gate(source, target, taxonomy=tax) == ()
    anchors = market_keyword_anchors(
        target_url="https://www.kenresearch.com/asia-pacific-vegetable-seed-market",
        target_title="Asia Pacific Vegetable Seed Market Share, Companies & Trends Report",
        taxonomy=tax,
        policy=policy(),
        geography_terms=("Asia Pacific",),
        require_geography=True,
    )
    assert [phrase.text for phrase in anchors] == ["Asia Pacific Vegetable Seed market"]


# --------------------------------------------------------------------------- #
# Siblings: allowed only when the embeddings agree (owner decision, 2026-10-30)
# --------------------------------------------------------------------------- #
SIBLINGS = ("solar-power", "wind-power")  # one parent, not relatable on the taxonomy alone


@pytest.mark.parametrize(
    ("similarity", "floor", "expected"),
    [
        (0.90, 0.78, (RelationshipType.RELATED_MARKET,)),  # embeddings agree -> allowed
        (0.78, 0.78, (RelationshipType.RELATED_MARKET,)),  # exactly at the floor
        (0.77, 0.78, ()),  # just below -> refused
        (0.10, 0.78, ()),
        (None, 0.78, ()),  # no signal -> FAIL CLOSED
        (0.99, None, ()),  # no floor configured -> FAIL CLOSED
    ],
)
def test_siblings_need_the_embeddings_to_agree(
    similarity: float | None, floor: float | None, expected: tuple[RelationshipType, ...]
) -> None:
    source = analysis(market_id=SIBLINGS[0])
    target = analysis(market_id=SIBLINGS[1])
    found = market_gate(
        source,
        target,
        taxonomy=taxonomy(),
        semantic_similarity=similarity,
        sibling_floor=floor,
    )
    assert tuple(found) == expected


def test_a_declared_relation_never_needs_the_similarity_signal() -> None:
    """Same/parent/child/supply-chain stand on the taxonomy alone: the floor is irrelevant."""
    source = analysis(market_id="solar-power")
    target = analysis(market_id="energy-storage")  # declared complementary
    assert market_gate(source, target, taxonomy=taxonomy()) == (
        RelationshipType.COMPLEMENTARY_MARKET,
    )
    assert market_gate(
        source, target, taxonomy=taxonomy(), semantic_similarity=0.0, sibling_floor=0.78
    ) == (RelationshipType.COMPLEMENTARY_MARKET,)


def test_unrelated_markets_stay_refused_however_similar_the_embeddings_say() -> None:
    """The sibling relief valve opens for SIBLINGS only, never for unrelated markets."""
    source = analysis(market_id="electric-vehicles")
    target = analysis(market_id="cold-chain")
    assert (
        market_gate(
            source, target, taxonomy=taxonomy(), semantic_similarity=0.99, sibling_floor=0.78
        )
        == ()
    )
