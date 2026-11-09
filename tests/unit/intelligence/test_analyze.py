"""Deterministic page analysis: scoring, selection, keywords, summary, salient blocks."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from typing import Final

import pytest

from ken_contracts.intelligence import (
    ANALYSIS_ALGORITHM_VERSION,
    EntityType,
    Intent,
    PageAnalysis,
    analysis_version,
)
from ken_contracts.inventory import SourceDocument, SourceType
from ken_services.intelligence.analyze import (
    BODY_MENTION_CAP,
    MARKET_CONFIDENCE_THRESHOLD,
    AnalysisInput,
    analyze_page,
)
from ken_services.intelligence.taxonomy import taxonomy_index
from ken_services.intelligence.text import ELLIPSIS, fold, tokens
from ken_services.inventory.normalize import normalize_document
from ken_services.inventory.urls import build_scope
from ken_services.recommendations.gates import primary_geography
from tests.support.fixture_site import FixtureSite
from tests.unit.intelligence._support import item, mini, v1


def _mini(**kwargs: object) -> PageAnalysis:
    return analyze_page(item(**kwargs), taxonomy=mini())  # type: ignore[arg-type]


def _topic_scores(result: PageAnalysis) -> dict[str, float]:
    return {t.node_id: t.score for t in result.topics}


# --------------------------------------------------------------------------- #
# Location weighting and topics
# --------------------------------------------------------------------------- #
def test_location_weights_and_body_cap() -> None:
    result = _mini(
        title="Widget report",
        headings=((2, "Widget outlook"), (3, "More widget news")),
        meta_description="widget",
        body_text="widget " * 12,
    )
    assert BODY_MENTION_CAP == 10
    assert _topic_scores(result) == {"widgets": 5 + 3 + 3 + 2 + 10}


def test_each_location_alone() -> None:
    assert _topic_scores(_mini(title="widget")) == {"widgets": 5.0}
    assert _topic_scores(_mini(headings=((2, "widget"),))) == {"widgets": 3.0}
    assert _topic_scores(_mini(meta_description="widget")) == {"widgets": 2.0}
    assert _topic_scores(_mini(body_text="widget")) == {"widgets": 1.0}


def test_ancestors_are_not_credited() -> None:
    result = _mini(title="smart widget")
    assert _topic_scores(result) == {"smart-widgets": 5.0}
    assert result.topics[0].depth == 2


def test_topics_sorted_by_score_then_depth_then_id_and_capped() -> None:
    result = _mini(title="widget", body_text="gadget. food industry.")
    assert [t.node_id for t in result.topics] == ["widgets", "food-ind", "gadgets"]
    many = analyze_page(
        item(
            body_text=(
                "solar power, wind power, energy storage, electric vehicles, ev charging, "
                "auto parts, chemicals, battery materials, semiconductors, data centers, "
                "cloud computing, pharma, diagnostics, fintech, insurance, cold chain"
            )
        ),
        taxonomy=v1(),
    )
    assert len(many.topics) == 10
    keys = [(-t.score, t.depth, t.node_id) for t in many.topics]
    assert keys == sorted(keys)


# --------------------------------------------------------------------------- #
# market_id / industry_id
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("kwargs", "market", "industry"),
    [
        # equal score, equal depth -> lowest id
        ({"title": "Widget and gadget"}, "gadgets", "tech-ind"),
        # equal score -> deeper wins
        ({"headings": ((2, "Smart widget"), (2, "Widget"))}, "smart-widgets", "tech-ind"),
        # higher score beats deeper
        ({"title": "Widget", "headings": ((2, "Smart widget"),)}, "widgets", "tech-ind"),
        # exactly the threshold qualifies
        ({"body_text": "widget widget widget"}, "widgets", "tech-ind"),
        # below threshold, no industry mention -> root of the top topic
        ({"body_text": "widget widget"}, None, "tech-ind"),
        # below threshold -> the highest-scoring industry node
        ({"body_text": "food industry. widget widget"}, None, "food-ind"),
        # an industry can itself be the confident market
        ({"title": "Tech industry"}, "tech-ind", "tech-ind"),
        # nothing at all
        ({"body_text": "nothing relevant here"}, None, None),
    ],
)
def test_market_and_industry_selection(
    kwargs: dict[str, object], market: str | None, industry: str | None
) -> None:
    result = _mini(**kwargs)
    assert MARKET_CONFIDENCE_THRESHOLD == 3.0
    assert (result.market_id, result.industry_id) == (market, industry)


def test_v1_report_page_selection() -> None:
    result = analyze_page(
        item(
            url="https://site.test/industry-reports/india-e2w",
            title="India Electric Two-Wheeler Market Outlook",
            headings=((1, "India Electric Two-Wheeler Market"),),
            body_text=(
                "India Electric Two-Wheeler Market\nThe electric vehicles sector grew. "
                "Electric scooters lead the way."
            ),
        ),
        taxonomy=v1(),
    )
    assert result.market_id == "electric-two-wheelers"
    assert result.industry_id == "automotive"
    assert _topic_scores(result)["electric-two-wheelers"] == 5 + 3 + 1 + 1
    assert _topic_scores(result)["electric-vehicles"] == 1


# --------------------------------------------------------------------------- #
# Geographies and entities
# --------------------------------------------------------------------------- #
def test_geographies_listed_without_global_or_ancestors() -> None:
    result = _mini(title="Aland", body_text="Aland and Bland, worldwide. Aland again. Xregion.")
    assert [(g.geo_id, g.kind, g.score) for g in result.geographies] == [
        ("aland", "country", 7.0),
        ("bland", "country", 1.0),
        ("xregion", "region", 1.0),
    ]


def test_geographies_v1_no_auto_ancestors() -> None:
    result = analyze_page(item(body_text="Demand in India and the UAE."), taxonomy=v1())
    assert [g.geo_id for g in result.geographies] == ["india", "uae"]


# --------------------------------------------------------------------------- #
# A page that declares itself global (ANALYSIS_ALGORITHM_VERSION 2)
# --------------------------------------------------------------------------- #
BODY_ABOUT_INDIA: Final = (
    "India leads the market. India again, and India once more. Indian demand is strong. "
    "Growth in India outpaces every other country discussed here."
)


def test_a_global_slug_makes_the_page_global_however_much_it_discusses_one_country() -> None:
    """The defect behind NEXT_STEPS item 33: 1,103 pages whose slug said "global" were
    labelled with a specific place, because the mention list held only region and country
    nodes and the most-discussed country won."""
    result = analyze_page(
        item(
            url="https://site.test/industry-reports/global-oat-milk-market",
            body_text=BODY_ABOUT_INDIA,
        ),
        taxonomy=v1(),
    )
    assert result.geographies[0].geo_id == "global"
    assert result.geographies[0].kind == "global"
    # India is still recorded; it simply no longer leads.
    assert "india" in [g.geo_id for g in result.geographies]
    assert primary_geography(result, taxonomy=v1()) is None


def test_a_global_title_is_enough_on_its_own() -> None:
    result = analyze_page(
        item(
            url="https://site.test/reports/x",
            title="Global Methanol Market",
            body_text=BODY_ABOUT_INDIA,
        ),
        taxonomy=v1(),
    )
    assert result.geographies[0].geo_id == "global"
    assert primary_geography(result, taxonomy=v1()) is None


def test_the_body_alone_never_makes_a_page_global() -> None:
    """ "global demand rose" must not turn a Germany report into a worldwide one."""
    result = analyze_page(
        item(
            url="https://site.test/industry-reports/germany-furniture-market",
            title="Germany Furniture Market",
            body_text="Global demand rose worldwide. Germany leads Europe in furniture.",
        ),
        taxonomy=v1(),
    )
    assert result.geographies[0].geo_id != "global"
    node = primary_geography(result, taxonomy=v1())
    assert node is not None and node.id == "germany"


def test_a_country_page_is_unaffected() -> None:
    result = analyze_page(
        item(url="https://site.test/industry-reports/india-ev-market", body_text=BODY_ABOUT_INDIA),
        taxonomy=v1(),
    )
    node = primary_geography(result, taxonomy=v1())
    assert node is not None and node.id == "india"


def test_a_country_page_whose_title_merely_mentions_the_world_stays_a_country_page() -> None:
    """The opposite over-trigger, measured on the corpus: "India Coffee Exports Competitive
    Gap Analysis" is an India page even though its copy discusses global competitors."""
    result = analyze_page(
        item(
            url="https://site.test/articles/india-coffee-exports-competitive-gap-analysis",
            title="India Coffee Exports: Competitive Gap vs Global Peers",
            body_text=BODY_ABOUT_INDIA,
        ),
        taxonomy=v1(),
    )
    node = primary_geography(result, taxonomy=v1())
    assert node is not None and node.id == "india"


def test_the_leading_geography_wins_when_both_are_named() -> None:
    """A global report that forecasts named regions is still global: "global" leads."""
    result = analyze_page(
        item(
            url="https://site.test/articles/global-personal-lines-market-forecast-india-uae",
            body_text=BODY_ABOUT_INDIA,
        ),
        taxonomy=v1(),
    )
    assert primary_geography(result, taxonomy=v1()) is None

    # ... and the mirror image: the country leads, so the page is that country's.
    mirrored = analyze_page(
        item(
            url="https://site.test/articles/india-opportunity-in-the-global-market",
            body_text=BODY_ABOUT_INDIA,
        ),
        taxonomy=v1(),
    )
    node = primary_geography(mirrored, taxonomy=v1())
    assert node is not None and node.id == "india"


def test_the_slug_outranks_the_title_for_the_qualifier() -> None:
    """The slug is the stable identity; a title may open with an editorial hook."""
    result = analyze_page(
        item(
            url="https://site.test/industry-reports/india-ev-market",
            title="Global Shifts Reshaping Electric Mobility",
            body_text=BODY_ABOUT_INDIA,
        ),
        taxonomy=v1(),
    )
    node = primary_geography(result, taxonomy=v1())
    assert node is not None and node.id == "india"


def test_the_algorithm_version_was_bumped_for_this_change() -> None:
    """A behaviour change that is not versioned leaves stored analyses silently
    disagreeing with the code that reads them."""
    assert ANALYSIS_ALGORITHM_VERSION >= 2
    assert analysis_version(3) == "tax3-alg2"


def test_entities_counted_and_sorted() -> None:
    result = analyze_page(
        item(
            title="Voltara Motors and Sunvale",
            body_text="Voltara leads. Sunvale Energy follows. Voltara again. Battery swapping.",
            headings=((2, "Voltara"),),  # headings are part of body_text; not counted twice
        ),
        taxonomy=v1(),
    )
    assert [(e.entity_id, e.mentions) for e in result.entities] == [
        ("voltara-motors", 3),
        ("sunvale-energy", 2),
        ("battery-swapping", 1),
    ]
    assert result.entities[0].type is EntityType.COMPANY
    assert result.entities[0].label == "Voltara Motors"


# --------------------------------------------------------------------------- #
# Page type
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("url", "page_type", "intent", "importance"),
    [
        ("https://site.test/reports/", "hub", Intent.NAVIGATIONAL, 1),
        ("https://site.test/reports/india-ev?x=1#top", "report", Intent.COMMERCIAL_RESEARCH, 3),
        ("https://site.test/insights/a", "insight", Intent.INFORMATIONAL, 2),
        ("https://site.test/contact", "transactional", Intent.TRANSACTIONAL, 1),
        ("https://site.test/terms", "corporate", Intent.CORPORATE, 0),
        ("https://site.test/misc/page", "other", Intent.INFORMATIONAL, 1),
        ("https://site.test", "hub", Intent.NAVIGATIONAL, 1),
    ],
)
def test_page_type_from_url_path(url: str, page_type: str, intent: Intent, importance: int) -> None:
    result = analyze_page(item(url=url), taxonomy=v1())
    assert (result.page_type, result.intent, result.commercial_importance) == (
        page_type,
        intent,
        importance,
    )


def test_default_page_type() -> None:
    result = _mini(url="https://site.test/whatever")
    assert result.page_type == "misc"
    assert result.intent is Intent.INFORMATIONAL


# --------------------------------------------------------------------------- #
# Keywords
# --------------------------------------------------------------------------- #
def test_keywords_scored_with_boosts_and_ties_by_token() -> None:
    result = _mini(title="Widget outlook", body_text="widget outlook alpha alpha alpha the of 2026")
    assert result.keywords == ("outlook", "widget", "alpha")


def test_keywords_capped_and_deterministic() -> None:
    body = " ".join(f"term{n:02d} " * (n % 4 + 1) for n in range(40))
    first = _mini(body_text=body).keywords
    assert len(first) == 15
    assert first == _mini(body_text=body).keywords
    assert first[:3] == ("term03", "term07", "term11")


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def test_summary_stops_at_sentence_boundary() -> None:
    sentence = ("word " * 49 + "end.").strip()  # 249 chars
    body = f"{sentence} {sentence} {sentence}"
    summary = _mini(body_text=body).summary
    assert summary == f"{sentence} {sentence}"
    assert len(summary) <= 600


def test_summary_skips_heading_paragraphs_and_joins_paragraphs() -> None:
    result = _mini(
        headings=((1, "Big Title"),),
        body_text="Big Title\n\nFirst paragraph. Second sentence!\n\nNext paragraph",
    )
    assert result.summary == "First paragraph. Second sentence! Next paragraph"


def test_summary_long_first_sentence_cut_at_word_with_ellipsis() -> None:
    sentence = " ".join(f"word{n}" for n in range(200)) + "."
    summary = _mini(body_text=sentence).summary
    assert len(summary) <= 600
    assert summary.endswith(ELLIPSIS)
    kept = summary[: -len(ELLIPSIS)]
    assert sentence.startswith(kept)
    assert sentence[len(kept)] == " "  # cut at a word boundary


def test_summary_empty() -> None:
    assert _mini(body_text="").summary == ""


# --------------------------------------------------------------------------- #
# Salient blocks
# --------------------------------------------------------------------------- #
def test_salient_blocks_scored_and_ranked() -> None:
    html = (
        "<main><h1>Widget widget widget market</h1>"
        "<p>Widget news</p>"  # too short
        "<p>The widget and gadget makers sold widgets here today.</p>"
        "<p>Unrelated words fill this long paragraph about nothing in particular at all.</p>"
        "<p>Smart widget demand rises with gadget sales in many regions.</p>"
        "<ul><li>A smart widget list item with five words</li></ul>"
        "</main>"
    )
    text = (
        "Widget widget widget market\n\nWidget news\n\n"
        "The widget and gadget makers sold widgets here today.\n\n"
        "Unrelated words fill this long paragraph about nothing in particular at all.\n\n"
        "Smart widget demand rises with gadget sales in many regions.\n\n"
        "A smart widget list item with five words"
    )
    result = _mini(headings=((1, "Widget widget widget market"),), body_html=html, body_text=text)
    keywords = set(result.keywords)
    blocks = result.salient_blocks
    assert len(blocks) == 3
    assert "b0" not in {b.block_id for b in blocks}  # heading
    assert "b1" not in {b.block_id for b in blocks}  # short
    for block in blocks:
        words = len(block.text.split())
        topic_hits = len(taxonomy_index(mini()).market_matcher.matches(fold(block.text)))
        kw_hits = sum(1 for t in tokens(block.text) if t in keywords)
        assert block.score == round((topic_hits + kw_hits) / math.sqrt(words), 6)
    keys = [(-b.score, int(b.block_id[1:])) for b in blocks]
    assert keys == sorted(keys)


def test_salient_block_text_truncated() -> None:
    long_text = "widget " + " ".join(f"filler{n}" for n in range(150))
    result = _mini(body_html=f"<main><p>{long_text}</p></main>", body_text=long_text)
    assert len(result.salient_blocks) == 1
    assert len(result.salient_blocks[0].text) <= 600
    assert result.salient_blocks[0].text.endswith(ELLIPSIS)


def test_no_salient_blocks_without_hits_or_html() -> None:
    assert _mini(body_text="widget widget widget").salient_blocks == ()


# --------------------------------------------------------------------------- #
# Versioning, determinism, fixture corpus
# --------------------------------------------------------------------------- #
def test_analysis_version_and_identity() -> None:
    result = _mini(body_text="widget")
    assert result.analysis_version == f"tax7-alg{ANALYSIS_ALGORITHM_VERSION}"
    assert result.extractor == "deterministic"
    assert result.page_version == 1


def _corpus_inputs() -> list[AnalysisInput]:
    site = FixtureSite()
    scope = build_scope(site.origin)
    out: list[AnalysisInput] = []
    for n, route in enumerate(site.html_pages(), start=1):
        url = site.url(route.path)
        page = normalize_document(
            SourceDocument(
                source_type=SourceType.PUBLIC_WEB,
                source_id=url,
                requested_url=url,
                final_url=url,
                http_status=200,
                content_type=route.content_type,
                fetched_at=datetime(2026, 10, 14, tzinfo=UTC),
                depth=0,
                html=route.text(),
            ),
            scope=scope,
        )
        out.append(
            item(
                url=page.canonical_url,
                title=page.title,
                meta_description=page.meta_description,
                headings=tuple((h.level, h.text) for h in page.headings),
                body_html=page.body_html,
                body_text=page.body_text,
                metadata=page.metadata,
                page_id=uuid.UUID(int=n),
            )
        )
    return out


def test_analysis_is_deterministic_over_the_fixture_corpus() -> None:
    policy = v1()
    inputs = _corpus_inputs()
    assert inputs
    for analysis_input in inputs:
        first = analyze_page(analysis_input, taxonomy=policy)
        second = analyze_page(analysis_input, taxonomy=policy)
        assert first == second
        assert first.model_dump_json() == second.model_dump_json()
        PageAnalysis.model_validate(first.model_dump(mode="json"))


def test_fixture_sea_ev_report() -> None:
    sea = next(i for i in _corpus_inputs() if i.url.endswith("/reports/sea-ev"))
    result = analyze_page(sea, taxonomy=v1())
    assert result.page_type == "report"
    assert result.market_id == "electric-vehicles"
    assert result.industry_id == "automotive"
    assert result.geographies[0].geo_id == "southeast-asia"
