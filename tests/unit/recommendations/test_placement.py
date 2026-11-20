"""ADR-0011 section 5: anchor phrases and deterministic, verbatim, safe placement."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

import pytest

from ken_contracts.intelligence import Intent
from ken_contracts.recommendations import RecommendationPolicy
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import ContentBlock
from ken_services.recommendations.placement import (
    ANCHOR_MAX_CHARS,
    MAX_LABEL_WORDS,
    MAX_SECONDARY_TOPICS,
    PREVIEW_CHARS,
    AnchorPhrase,
    AnchorSource,
    PlacementCandidate,
    anchor_phrases,
    anchor_rule_ok,
    block_is_prose,
    corpus_title_phrases,
    find_placement,
    keyword_bigrams,
    market_keyword_anchors,
    market_keyword_phrases,
    market_phrases,
    phrase_occurrences,
    preview_for,
    slug_text,
    span_is_safe,
    title_core,
)
from tests.unit.recommendations._support import (
    SOLAR,
    analysis,
    blocks,
    policy,
    policy_with,
    taxonomy,
    taxonomy_v3,
    taxonomy_with,
)


def _place(
    body: str | Sequence[ContentBlock],
    phrases: Sequence[AnchorPhrase] = (SOLAR,),
    *,
    links_per_block: Mapping[str, int] | None = None,
    blocks_linking_target: Collection[str] = (),
    taken_spans: Collection[tuple[str, int, int]] = (),
    salience: Mapping[str, float] | None = None,
    rules: RecommendationPolicy | None = None,
) -> PlacementCandidate | None:
    return find_placement(
        blocks=blocks(body) if isinstance(body, str) else body,
        phrases=phrases,
        links_per_block=links_per_block or {},
        blocks_linking_target=blocks_linking_target,
        taken_spans=taken_spans,
        salience=salience or {},
        policy=rules or policy(),
    )


def _phrase(text: str, source: AnchorSource = "target_topic", rank: int = 0) -> AnchorPhrase:
    return AnchorPhrase(text=text, source=source, rank=rank)


INTRO = "<p>Opening paragraph about nothing in particular.</p>"


# --------------------------------------------------------------------------- #
# Anchor phrases
# --------------------------------------------------------------------------- #
def _texts(phrases: Sequence[AnchorPhrase]) -> list[tuple[str, str, int]]:
    return [(p.text, p.source, p.rank) for p in phrases]


def test_market_label_and_aliases_come_first_longest_first() -> None:
    target = analysis(market_id="ev-charging")
    phrases = anchor_phrases(target=target, target_title=None, taxonomy=taxonomy(), policy=policy())
    assert _texts(phrases) == [
        ("EV Charging Infrastructure", "target_topic", 0),
        ("charging infrastructure", "target_topic", 0),
        ("charging stations", "target_topic", 0),
        ("charging station", "target_topic", 0),
        ("charge points", "target_topic", 0),
        ("fast chargers", "target_topic", 0),
        ("ev charging", "target_topic", 0),
    ]


def test_secondary_topics_are_confident_sub_markets_of_the_target_market() -> None:
    """Only sub-markets of the target's OWN market: anything else names another page's market."""
    target = analysis(
        market_id="electric-vehicles",
        topics=[
            ("electric-vehicles", 20),
            ("automotive", 15),  # depth 0: an industry, never an anchor source
            ("ev-charging", 9),  # child of electric-vehicles: allowed
            ("lithium-ion-batteries", 6),  # another industry's market: misleading, excluded
            ("auto-components", 4),  # a sibling, not a sub-market: excluded
            ("electric-two-wheelers", 3),  # child, confident: allowed
            ("solar-power", 2),  # below the confidence threshold
        ],
    )
    phrases = anchor_phrases(target=target, target_title=None, taxonomy=taxonomy(), policy=policy())
    by_rank: dict[int, set[str]] = {}
    for p in phrases:
        by_rank.setdefault(p.rank, set()).add(p.text)
    assert "Electric Vehicles" in by_rank[0]
    assert "EV Charging Infrastructure" in by_rank[1]
    assert "Electric Two-Wheelers" in by_rank[2]
    flat = {p.text.casefold() for p in phrases}
    for excluded in ("automotive", "lithium-ion batteries", "auto components", "solar power"):
        assert excluded not in flat, excluded
    assert len([r for r in by_rank if r >= 1]) <= MAX_SECONDARY_TOPICS + 2  # + title/keyword ranks


def test_title_core_and_keyword_bigrams_follow_the_topics() -> None:
    target = analysis(
        market_id="solar-power", keywords=["rooftop", "installers", "solar", "india", "market"]
    )
    phrases = anchor_phrases(
        target=target,
        target_title="India Rooftop Installers Market Report 2026",
        taxonomy=taxonomy(),
        policy=policy(),
    )
    tail = [(p.text, p.source, p.rank) for p in phrases if p.source != "target_topic"]
    assert tail == [("Rooftop Installers", "target_title", 1)]  # bigram deduplicated away
    target2 = analysis(market_id="solar-power", keywords=["rooftop", "installers", "growth"])
    phrases2 = anchor_phrases(
        target=target2,
        target_title="Rooftop Installers Growth: India Outlook",
        taxonomy=taxonomy(),
        policy=policy(),
    )
    assert [(p.text, p.source) for p in phrases2 if p.source != "target_topic"] == [
        ("Rooftop Installers Growth", "target_title"),
        ("Rooftop Installers", "target_keyword"),
    ]


def test_geography_is_never_an_anchor() -> None:
    tax = taxonomy_with(solar_power=["india", "Southeast Asia"])
    target = analysis(market_id="solar-power", keywords=["india", "solar"])
    for title in ("India", "India Market Report", "Southeast Asia Outlook", "India Solar"):
        phrases = anchor_phrases(target=target, target_title=title, taxonomy=tax, policy=policy())
        folded = {p.text.casefold() for p in phrases}
        assert not folded & {"india", "southeast asia", "india solar"}, title
    # ... and so a geography mention in the source is never linked.
    phrases = anchor_phrases(target=target, target_title="India", taxonomy=tax, policy=policy())
    assert _place("<p>India is a large country with many people.</p>", phrases) is None


def test_phrases_are_deduplicated_case_insensitively_keeping_the_best_rank() -> None:
    tax = taxonomy_with(solar_power=["SOLAR POWER", "Rooftop Solar"])
    target = analysis(market_id="solar-power", keywords=["solar", "power"])
    phrases = anchor_phrases(
        target=target, target_title="Solar Power Market", taxonomy=tax, policy=policy()
    )
    folded = [p.text.casefold() for p in phrases]
    assert len(folded) == len(set(folded))
    assert ("solar power", "target_topic") in {(p.text.casefold(), p.source) for p in phrases}


def test_anchor_rules_filter_phrases_per_source_kind() -> None:
    target = analysis(market_id="energy-storage")
    default = anchor_phrases(target=target, target_title=None, taxonomy=taxonomy(), policy=policy())
    assert "bess" in {p.text for p in default}  # 1 word, 4 chars: allowed for a topic
    stricter = policy_with(placement={"min_anchor_words": 2, "min_anchor_chars": 5})
    phrases = anchor_phrases(target=target, target_title=None, taxonomy=taxonomy(), policy=stricter)
    assert all(len(p.text.split()) >= 2 for p in phrases)
    # "evs" (3 chars) is below the default minimum of 4 characters.
    ev = analysis(market_id="electric-vehicles")
    assert "evs" not in {
        p.text
        for p in anchor_phrases(target=ev, target_title=None, taxonomy=taxonomy(), policy=policy())
    }
    # A one-word title core is not enough (non-topic anchors need 2 words).
    plain = analysis()
    assert (
        anchor_phrases(
            target=plain, target_title="Healthcare Market", taxonomy=taxonomy(), policy=policy()
        )
        == ()
    )


def test_forbidden_and_overlong_phrases_are_dropped() -> None:
    rules = policy_with(placement={"forbidden_anchors": ["SOLAR PV", "here"]})
    target = analysis(market_id="solar-power")
    texts = {
        p.text
        for p in anchor_phrases(target=target, target_title=None, taxonomy=taxonomy(), policy=rules)
    }
    assert "solar pv" not in texts
    assert "photovoltaic" in texts
    one_word = policy_with(placement={"max_anchor_words": 1})
    texts = {
        p.text
        for p in anchor_phrases(
            target=target, target_title=None, taxonomy=taxonomy(), policy=one_word
        )
    }
    assert texts == {"photovoltaic"}


def test_no_market_and_no_title_gives_no_phrases() -> None:
    assert (
        anchor_phrases(target=analysis(), target_title=None, taxonomy=taxonomy(), policy=policy())
        == ()
    )


def test_anchor_phrases_are_deterministic() -> None:
    target = analysis(
        market_id="ev-charging", topics=[("ev-charging", 9), ("electric-vehicles", 5)]
    )
    calls = [
        anchor_phrases(
            target=target, target_title="EV Charging Market", taxonomy=taxonomy(), policy=policy()
        )
        for _ in range(3)
    ]
    assert calls[0] == calls[1] == calls[2]


@pytest.mark.parametrize(
    ("title", "core"),
    [
        ("India Electric Vehicle Market Report 2025-2030", "Electric Vehicle"),
        (
            "Southeast Asia EV Charging Infrastructure Market Outlook to 2030",
            "EV Charging Infrastructure",
        ),
        ("Solar Power in Vietnam: Trends and Forecast", "Solar Power"),
        ("India's Cold Chain Industry", "Cold Chain"),
        ("Data Centers | Example Research", "Data Centers"),
        ("Lithium-ion Battery Market Size and Share", "Lithium-ion Battery"),
        ("Cold Chain \u2013 Trends and Size", "Cold Chain"),
        ("Cold Chain \u2014 Trends", "Cold Chain"),
        ("Battery Storage Outlook for India and the Middle East", "Battery Storage"),
        ("Rooftop  Solar\nInstallers", "Rooftop Solar Installers"),
        ("Indonesia", "Indonesia"),  # never stripped to nothing (anchor_phrases drops it)
        ("Market Outlook", "Market"),
        ("2030", None),
        ("", None),
        (None, None),
    ],
)
def test_title_core(title: str | None, core: str | None) -> None:
    assert title_core(title, taxonomy=taxonomy()) == core


def test_keyword_bigrams() -> None:
    keywords = ["india", "electric", "vehicle", "market", "two", "wheeler", "charging"]
    assert keyword_bigrams(
        "India Electric Vehicle Market Report", keywords=keywords, taxonomy=taxonomy()
    ) == ("Electric Vehicle",)
    assert keyword_bigrams("Electric, Vehicle", keywords=keywords, taxonomy=taxonomy()) == ()
    assert keyword_bigrams(
        "Electric Two-Wheeler Charging", keywords=keywords, taxonomy=taxonomy()
    ) == ("Electric Two-Wheeler", "Two-Wheeler Charging")
    assert keyword_bigrams("Unknown Words", keywords=keywords, taxonomy=taxonomy()) == ()
    assert keyword_bigrams(None, keywords=keywords, taxonomy=taxonomy()) == ()


def test_anchor_rule_ok() -> None:
    rules = policy().placement
    assert anchor_rule_ok("photovoltaic", "target_topic", policy=rules)
    assert not anchor_rule_ok("photovoltaic", "target_title", policy=rules)
    assert anchor_rule_ok("solar panels", "target_keyword", policy=rules)
    assert not anchor_rule_ok("Read More", "target_keyword", policy=rules)
    assert not anchor_rule_ok("abc", "target_topic", policy=rules)
    assert not anchor_rule_ok("-- --", "target_title", policy=rules)
    assert not anchor_rule_ok(
        "one two three four five six seven eight nine", "target_title", policy=rules
    )
    assert not anchor_rule_ok("x" * (ANCHOR_MAX_CHARS + 1), "target_topic", policy=rules)


# --------------------------------------------------------------------------- #
# Matching: verbatim, whole phrase, case-insensitive
# --------------------------------------------------------------------------- #
def test_anchor_is_copied_verbatim_in_original_case() -> None:
    found = _place(INTRO + "<p>Demand for Solar POWER is rising fast.</p>")
    assert found is not None
    assert found.anchor_text == "Solar POWER"
    assert found.preview.anchor == "Solar POWER"
    assert found.preview.before == "Demand for "
    assert found.preview.after == " is rising fast."
    block = blocks(INTRO + "<p>Demand for Solar POWER is rising fast.</p>")[1]
    loc = found.locator
    assert (loc.block_id, loc.block_kind, loc.occurrence_index) == ("b1", "p", 0)
    assert block.text[loc.start : loc.end] == found.anchor_text
    assert loc.context_hash == sha256_hex(block.text)


@pytest.mark.parametrize(
    "text",
    [
        "Solar powered pumps are common.",  # longer word
        "The solar-power segment is small.",  # hyphen is not a space
        "Asolar power plant is odd.",  # glued prefix
        "Solar power-based pumps are common.",  # hyphenated compound
        "Solar_power is not a phrase.",
    ],
)
def test_whole_phrase_boundaries(text: str) -> None:
    assert _place(f"{INTRO}<p>{text}</p>") is None


def test_punctuation_is_a_boundary() -> None:
    found = _place(INTRO + "<p>Investment (solar power) rose; solar power, too.</p>")
    assert found is not None
    assert found.anchor_text == "solar power"
    assert found.locator.occurrence_index == 0


def test_phrase_occurrences() -> None:
    text = "Solar power, solar POWER and solar powered."
    assert phrase_occurrences("solar  power", text) == ((0, 11), (13, 24))
    assert phrase_occurrences("", text) == ()
    assert phrase_occurrences("li-ion", "Li-ion and li-ion-based cells") == ((0, 6),)


# --------------------------------------------------------------------------- #
# Placement safety
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body",
    [
        '<p>See <a href="/x">solar power</a> here.</p>',
        "<p>Call <code>solar power</code> now.</p>",
        "<p>As <cite>solar power</cite> says.</p>",
        "<p>Note<sup>solar power</sup> here.</p>",
        "<p>Note<sub>solar power</sub> here.</p>",
        "<p>He said <q>solar power</q> twice.</p>",
        "<p>Type <kbd>solar power</kbd> please.</p>",
        "<table><tr><td>solar power is here</td></tr></table>",
        "<table><tr><td><p>solar power is here</p></td></tr></table>",
        "<blockquote><p>solar power is here</p></blockquote>",
        "<figure><figcaption>solar power is here</figcaption></figure>",
        "<figure><p>solar power is here</p></figure>",
        "<h2>Solar power</h2>",
        "<ul><li><a href='/x'>solar power</a></li></ul>",
        "<div>solar power in a div</div>",
        "<dl><dd>solar power in a dd</dd></dl>",
        "<pre>solar power</pre>",
    ],
)
def test_excluded_ancestors_enclosures_and_kinds_are_never_used(body: str) -> None:
    assert _place(body) is None


def test_allowed_inline_formatting_inside_one_run_is_fine() -> None:
    found = _place("<p>Our view: <strong>solar power</strong> wins.</p><ul><li>x</li></ul>")
    assert found is not None
    assert found.anchor_text == "solar power"
    # Punctuated, so `block_is_prose` admits it: a list item is placeable when it is
    # written as a sentence, which is what this test is about.
    found = _place("<ul><li>Solar power wins in a list item.</li></ul>")
    assert found is not None
    assert found.locator.block_kind == "li"


@pytest.mark.parametrize(
    "body",
    [
        "<p>Big solar <em>power</em> plants.</p>",
        "<p>Big <b>solar</b> power plants.</p>",
        "<p>Big solar<br>power plants.</p>",
        '<p>Big solar <a href="/p">power</a> plants.</p>',
    ],
)
def test_a_span_across_two_runs_is_rejected(body: str) -> None:
    assert _place(body) is None


def test_block_kinds_come_from_policy() -> None:
    body = "<div>solar power in a div, which is a sentence.</div>"
    assert _place(body, rules=policy_with(placement={"block_kinds": ["div"]})) is not None


def test_custom_excluded_ancestors_come_from_policy() -> None:
    body = "<p>Our view: <strong>solar power</strong> wins.</p>"
    rules = policy_with(placement={"excluded_ancestors": ["strong"]})
    assert _place(body, rules=rules) is None


def test_blocks_built_without_runs_are_never_used() -> None:
    bare = (ContentBlock(block_id="b0", kind="p", text="solar power everywhere"),)
    assert _place(bare) is None
    assert not span_is_safe(bare[0], 0, 11, excluded_ancestors=())


def test_span_is_safe_bounds() -> None:
    (block,) = blocks("<p>solar power</p>")
    assert span_is_safe(block, 0, 11, excluded_ancestors=())
    assert not span_is_safe(block, 0, 12, excluded_ancestors=())
    assert not span_is_safe(block, 5, 5, excluded_ancestors=())


# --------------------------------------------------------------------------- #
# Anchor rules at placement time
# --------------------------------------------------------------------------- #
def test_forbidden_anchors_are_rejected_case_insensitively() -> None:
    phrases = [_phrase("Read More", "target_keyword"), _phrase("this ARTICLE", "target_title")]
    assert _place("<p>Please read more in this article today.</p>", phrases) is None


def test_min_words_depend_on_the_anchor_source() -> None:
    body = INTRO + "<p>Photovoltaic output keeps rising.</p>"
    assert _place(body, [_phrase("photovoltaic", "target_topic")]) is not None
    assert _place(body, [_phrase("photovoltaic", "target_title")]) is None
    assert _place(body, [_phrase("photovoltaic", "target_keyword")]) is None
    two = [_phrase("photovoltaic output", "target_keyword")]
    found = _place(body, two)
    assert found is not None
    assert found.anchor_source == "target_keyword"


def test_min_chars_and_max_words() -> None:
    body = INTRO + "<p>The bess and the big solar power plant.</p>"
    assert (
        _place(body, [_phrase("bess")], rules=policy_with(placement={"min_anchor_chars": 5}))
        is None
    )
    assert _place(body, [SOLAR], rules=policy_with(placement={"max_anchor_words": 1})) is None


# --------------------------------------------------------------------------- #
# Block skipping, taken spans, occurrences
# --------------------------------------------------------------------------- #
TWO_BLOCKS = INTRO + "<p>First solar power mention.</p><p>Second solar power mention.</p>"


def test_blocks_at_the_link_cap_are_skipped() -> None:
    found = _place(TWO_BLOCKS, links_per_block={"b1": 1})
    assert found is not None
    assert found.locator.block_id == "b2"
    raised = policy_with(selection={"max_links_per_block": 2})
    found = _place(TWO_BLOCKS, links_per_block={"b1": 1}, rules=raised)
    assert found is not None
    assert found.locator.block_id == "b1"
    assert _place(TWO_BLOCKS, links_per_block={"b1": 1, "b2": 3}) is None


def test_blocks_already_linking_the_target_are_skipped() -> None:
    found = _place(TWO_BLOCKS, blocks_linking_target={"b1"})
    assert found is not None
    assert found.locator.block_id == "b2"
    assert _place(TWO_BLOCKS, blocks_linking_target={"b1", "b2"}) is None


def test_taken_spans_reject_overlaps_only_in_the_same_block_text() -> None:
    body = INTRO + "<p>Solar power and solar power again.</p>"
    block = blocks(body)[1]
    digest = sha256_hex(block.text)
    found = _place(body, taken_spans={(digest, 6, 9)})  # overlaps the first occurrence
    assert found is not None
    assert found.locator.occurrence_index == 1
    assert found.anchor_text == "solar power"
    assert (found.locator.start, found.locator.end) == (16, 27)
    touching = _place(body, taken_spans={(digest, 11, 16)})  # touches, does not overlap
    assert touching is not None
    assert touching.locator.occurrence_index == 0
    elsewhere = _place(body, taken_spans={("0" * 64, 0, 40)})
    assert elsewhere is not None
    assert elsewhere.locator.occurrence_index == 0
    assert _place(body, taken_spans={(digest, 0, 40)}) is None


def test_occurrence_index_counts_case_insensitive_occurrences() -> None:
    body = INTRO + '<p>SOLAR POWER, <a href="/x">solar power</a> and Solar Power.</p>'
    found = _place(body)
    assert found is not None
    # The first occurrence is free; the second (inside a link) is never used.
    assert found.locator.occurrence_index == 0
    block = blocks(body)[1]
    digest = sha256_hex(block.text)
    found = _place(body, taken_spans={(digest, 0, 11)})
    assert found is not None
    assert found.anchor_text == "Solar Power"
    assert found.locator.occurrence_index == 2
    start, end = phrase_occurrences(found.anchor_text, block.text)[2]
    assert (start, end) == (found.locator.start, found.locator.end)


def test_skip_first_block_skips_the_opening_paragraph() -> None:
    body = "<h1>Title</h1><p>Solar power opens.</p><p>Solar power later.</p>"
    found = _place(body, rules=policy_with(placement={"skip_first_block": True}))
    assert found is not None
    assert found.locator.block_id == "b2"


def test_market_overview_is_a_hard_placement_exclusion() -> None:
    body = (
        "<h2>Market Overview</h2>"
        "<p>The solar power market supports utility planning.</p>"
        "<h2>Growth Drivers</h2>"
        "<p>Manufacturers compare the solar power market when planning capacity.</p>"
    )
    found = _place(body)
    assert found is not None
    assert found.locator.block_id == "b3"


def test_related_reports_section_never_hosts_an_inline_anchor() -> None:
    body = "<h2>Related Reports</h2><p>The solar power market supports utility planning.</p>"
    assert _place(body) is None
    assert _place(body) is not None


# --------------------------------------------------------------------------- #
# Choice and quality
# --------------------------------------------------------------------------- #
def test_rank_beats_length() -> None:
    phrases = [_phrase("solar power", rank=0), _phrase("rooftop solar power plants", rank=1)]
    body = INTRO + "<p>New rooftop solar power plants; solar power demand grows.</p>"
    found = _place(body, phrases)
    assert found is not None
    assert found.anchor_text == "solar power"
    # The first "solar power" is inside the longer phrase: a fragment, never chosen.
    assert found.locator.start == blocks(body)[1].text.index("; solar") + 2


def test_length_beats_salience() -> None:
    phrases = [_phrase("solar power"), _phrase("rooftop solar power")]
    body = INTRO + "<p>Solar power is salient.</p><p>Rooftop solar power is not.</p>"
    found = _place(body, phrases, salience={"b1": 9.0})
    assert found is not None
    assert found.anchor_text == "Rooftop solar power"


def test_salience_beats_block_order_and_block_order_beats_occurrence() -> None:
    found = _place(TWO_BLOCKS, salience={"b2": 0.5})
    assert found is not None
    assert found.locator.block_id == "b2"
    found = _place(TWO_BLOCKS)
    assert found is not None
    assert found.locator.block_id == "b1"


def test_choice_is_independent_of_phrase_order() -> None:
    phrases = [_phrase("solar power"), _phrase("rooftop solar power"), _phrase("solar", rank=1)]
    body = INTRO + "<p>Rooftop solar power and solar.</p>"
    assert _place(body, phrases) == _place(body, list(reversed(phrases)))


@pytest.mark.parametrize(
    ("source", "text", "opening", "later"),
    [
        ("target_topic", "photovoltaic", 0.9, 1.0),
        ("target_title", "photovoltaic output", 0.8, 0.9),
        ("target_keyword", "photovoltaic output", 0.65, 0.75),
    ],
)
def test_placement_quality(source: AnchorSource, text: str, opening: float, later: float) -> None:
    phrase = [_phrase(text, source)]
    first = _place("<h1>Heading first</h1><p>Photovoltaic output rose.</p>", phrase)
    assert first is not None
    assert first.placement_quality == opening  # the opening paragraph, headings never count
    second = _place(INTRO + "<p>Photovoltaic output rose.</p>", phrase)
    assert second is not None
    assert second.placement_quality == later


def test_nothing_is_fabricated_when_no_phrase_occurs() -> None:
    assert _place(INTRO + "<p>Wind turbines only.</p>") is None
    assert _place(INTRO, ()) is None
    assert _place((), (SOLAR,)) is None


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
def test_preview_is_trimmed_at_word_boundaries_and_stays_verbatim() -> None:
    before_words = " ".join(f"word{n:03d}" for n in range(60))
    after_words = " ".join(f"tail{n:03d}" for n in range(60))
    body = f"<p>{before_words} solar power {after_words}</p>"
    found = _place(INTRO + body)
    assert found is not None
    preview = found.preview
    assert len(preview.before) <= PREVIEW_CHARS
    assert len(preview.after) <= PREVIEW_CHARS
    assert preview.before.endswith("word059 ")
    assert preview.before.startswith("word")
    assert preview.after.startswith(" tail000")
    assert preview.after.endswith(tuple(f"tail{n:03d}" for n in range(60)))
    text = blocks(INTRO + body)[1].text
    assert preview.before + preview.anchor + preview.after in text


def test_preview_hard_cuts_only_a_single_overlong_word() -> None:
    word = "x" * 250
    preview = preview_for(f"{word} solar power {word}", 251, 262)
    assert preview.anchor == "solar power"
    assert preview.before == "x" * (PREVIEW_CHARS - 1) + " "  # the boundary space is kept
    assert preview.after == " " + "x" * (PREVIEW_CHARS - 1)


def test_short_previews_are_the_whole_block() -> None:
    preview = preview_for("A solar power plant.", 2, 13)
    assert (preview.before, preview.anchor, preview.after) == ("A ", "solar power", " plant.")


def test_anchors_never_name_the_source_pages_own_market() -> None:
    """India pharma -> Bangladesh pharma must not be anchored on 'pharmaceuticals' phrases."""
    source = analysis(market_id="pharmaceuticals")
    target = analysis(market_id="pharmaceuticals")
    with_source = anchor_phrases(
        target=target, target_title=None, taxonomy=taxonomy(), policy=policy(), source=source
    )
    without = anchor_phrases(target=target, target_title=None, taxonomy=taxonomy(), policy=policy())
    names = {p.text.casefold() for p in without if p.source == "target_topic"}
    assert "active pharmaceutical ingredients" in names
    assert not any(p.source == "target_topic" for p in with_source)
    # A DIFFERENT target market keeps its own topic anchors.
    other = anchor_phrases(
        target=analysis(market_id="cold-chain"),
        target_title=None,
        taxonomy=taxonomy(),
        policy=policy(),
        source=source,
    )
    assert any(p.text.casefold() == "cold chain" for p in other)


def test_own_market_anchor_needs_the_targets_geography_in_the_same_block() -> None:
    """'cold chain' on the India report may link to the UAE report only next to 'UAE'."""
    from tests.unit.recommendations._support import blocks

    source = analysis(market_id="cold-chain", geographies=["india"])
    target = analysis(market_id="cold-chain", geographies=["uae"])
    phrases = anchor_phrases(
        target=target, target_title=None, taxonomy=taxonomy(), policy=policy(), source=source
    )
    topic = [p for p in phrases if p.source == "target_topic"]
    assert topic and all("UAE" in p.context_terms or "uae" in p.context_terms for p in topic)

    def place(body: str) -> PlacementCandidate | None:
        return find_placement(
            blocks=blocks(body),
            phrases=phrases,
            links_per_block={},
            blocks_linking_target=(),
            taken_spans=(),
            salience={},
            policy=policy(),
        )

    assert place("<p>India's cold chain is growing fast across states.</p>") is None
    found = place("<p>Cold chain capacity in the UAE is expanding around Dubai ports.</p>")
    assert found is not None and found.anchor_text.casefold() == "cold chain"
    # Same geography on both sides: nothing distinguishes the target, so no anchor at all.
    same = anchor_phrases(
        target=analysis(market_id="cold-chain", geographies=["india"]),
        target_title=None,
        taxonomy=taxonomy(),
        policy=policy(),
        source=source,
    )
    assert not any(p.source == "target_topic" for p in same)


def test_a_fragment_of_the_sources_own_market_is_gated_like_the_market_itself() -> None:
    """QA H1: 'Battery Energy' (a keyword bigram) is a fragment of the source's own market
    'battery energy storage': it needs the target's geography exactly as the topic does."""
    source = analysis(market_id="energy-storage", geographies=["uae"])
    target = analysis(
        market_id="energy-storage",
        geographies=["india"],
        keywords=["battery", "energy", "storage"],
    )
    phrases = anchor_phrases(
        target=target,
        target_title="Battery Energy Storage Market in India",
        taxonomy=taxonomy(),
        policy=policy(),
        source=source,
    )
    by_text = {p.text.casefold(): p for p in phrases}
    assert "battery energy" in by_text
    for phrase in phrases:
        assert phrase.context_terms, phrase  # every phrase names the source's market
        assert any(term.casefold() == "india" for term in phrase.context_terms)
    # A different source market leaves the same fragment ungated.
    other = anchor_phrases(
        target=target,
        target_title="Battery Energy Storage Market in India",
        taxonomy=taxonomy(),
        policy=policy(),
        source=analysis(market_id="cold-chain", geographies=["uae"]),
    )
    assert not any(p.context_terms for p in other)


def test_a_strict_fragment_of_a_longer_market_phrase_is_never_an_anchor() -> None:
    """QA H1: never '<a>battery energy</a> storage projects' -- that cuts a market name."""
    keyword = _phrase("battery energy", "target_keyword", rank=2)
    body = INTRO + "<p>New battery energy storage projects are planned.</p>"

    def place(text: str, enclosing: Collection[str]) -> PlacementCandidate | None:
        return find_placement(
            blocks=blocks(text),
            phrases=(keyword,),
            links_per_block={},
            blocks_linking_target=(),
            taken_spans=(),
            salience={},
            policy=policy(),
            enclosing_phrases=enclosing,
        )

    assert place(body, ()) is not None  # without the guard: the fragment would be linked
    assert place(body, market_phrases(taxonomy())) is None
    # A longer phrase of the SAME target guards too, with no taxonomy phrases passed.
    longer = _phrase("battery energy storage", "target_topic", rank=0)
    found = _place(body, (keyword, longer))
    assert found is not None and found.anchor_text == "battery energy storage"
    # Not a fragment where the longer phrase does not occur around it.
    free = INTRO + "<p>Battery energy demand is rising.</p>"
    assert place(free, market_phrases(taxonomy())) is not None


@pytest.mark.parametrize(
    "wrapper",
    ["label", "del", "s", "small", "video", "audio", "canvas", "map", "footer", "details"],
)
def test_wrappers_media_fallbacks_and_non_body_regions_are_excluded(wrapper: str) -> None:
    """QA M1: never an anchor in a form label, struck text, media fallback text or a footer."""
    inline = INTRO + f"<p>Demand for <{wrapper}>solar power</{wrapper}> keeps rising.</p>"
    block = INTRO + f"<{wrapper}><p>Demand for solar power keeps rising.</p></{wrapper}>"
    assert _place(inline) is None
    assert _place(block) is None
    assert _place(INTRO + "<p>Demand for solar power keeps rising.</p>") is not None


def test_a_parent_markets_topic_label_sharing_a_word_with_an_own_alias_is_kept() -> None:
    """QA re-review: the fragment rule applies to title cores and keyword bigrams only.
    'Logistics' (parent market) must survive on a cold-chain source whose alias is
    'temperature-controlled logistics'."""
    source = analysis(market_id="cold-chain", geographies=["india"])
    target = analysis(market_id="logistics", geographies=["india"])
    phrases = anchor_phrases(
        target=target, target_title=None, taxonomy=taxonomy(), policy=policy(), source=source
    )
    topic = {p.text.casefold(): p for p in phrases if p.source == "target_topic"}
    assert "logistics" in topic
    assert topic["logistics"].context_terms == ()


@pytest.mark.parametrize(
    ("source_intent", "anchored"),
    [
        (Intent.INFORMATIONAL, True),  # article -> the report on its market: the hop
        (Intent.COMMERCIAL_RESEARCH, False),  # report -> report: self-describing, dropped
        (Intent.NAVIGATIONAL, False),
        (Intent.TRANSACTIONAL, False),
    ],
)
def test_the_commercial_hop_keeps_the_plain_market_anchor_only_for_articles(
    source_intent: Intent, anchored: bool
) -> None:
    """ADR-0011 s9.1: same market AND geography, nothing distinctive -- only an
    informational source keeps 'cold chain' (unconditioned) toward the report."""
    source = analysis(
        market_id="cold-chain", geographies=["india"], intent=source_intent, page_type="insight"
    )
    target = analysis(market_id="cold-chain", geographies=["india"])
    phrases = anchor_phrases(
        target=target, target_title=None, taxonomy=taxonomy(), policy=policy(), source=source
    )
    names = {p.text.casefold(): p for p in phrases if p.source == "target_topic"}
    assert ("cold chain" in names) is anchored
    if anchored:
        assert names["cold chain"].context_terms == ()


def test_the_commercial_hop_still_needs_a_distinctive_geography_when_there_is_one() -> None:
    """India article -> UAE report: the plain phrase is kept, but only next to 'UAE'."""
    source = analysis(
        market_id="cold-chain",
        geographies=["india"],
        intent=Intent.INFORMATIONAL,
        page_type="insight",
    )
    target = analysis(market_id="cold-chain", geographies=["uae"])
    phrases = anchor_phrases(
        target=target, target_title=None, taxonomy=taxonomy(), policy=policy(), source=source
    )
    cold = next(p for p in phrases if p.text.casefold() == "cold chain")
    assert any(term.casefold() == "uae" for term in cold.context_terms)


# --------------------------------------------------------------------------- #
# Market-keyword anchors: the target's own market phrase plus "market"
# --------------------------------------------------------------------------- #
KEN = "https://www.kenresearch.com"


@pytest.mark.parametrize(
    ("url", "title", "expected"),
    [
        # The real shapes of the site: <geography>-<market>-market.
        (
            f"{KEN}/industry-reports/india-cosmetics-market",
            "India Cosmetics Market Share, Companies & Trends Report 2025-2032",
            ("Cosmetics",),
        ),
        (
            f"{KEN}/saudi-arabia-contract-logistics-market",
            "Saudi Arabia Contract Logistics Market Outlook to 2030",
            ("Contract Logistics",),
        ),
        (
            f"{KEN}/industry-reports/north-america-nut-and-seeds-market",
            "North America Nut and Seeds Market Size USD 16.80 Billion, Forecast To 2030",
            ("Nut and Seeds",),
        ),
        # Title only, slug only, neither.
        (f"{KEN}/asia-pacific-vegetable-seed-market", None, ("vegetable seed",)),
        (None, "Indonesia Natural Personal Care Market Share", ("Natural Personal Care",)),
        (None, None, ()),
        # A trailing report id never hides the slug.
        (f"{KEN}/industry-reports/india-cosmetics-market/120045", None, ("cosmetics",)),
        # The title keeps real hyphenation that the slug flattens: both are offered.
        (
            f"{KEN}/india-lithium-ion-batteries-market",
            "India Lithium-ion Batteries Market Report",
            ("Lithium-ion Batteries", "lithium ion batteries"),
        ),
    ],
)  # fmt: skip
def test_market_keyword_phrases_come_from_the_targets_identity(
    url: str | None, title: str | None, expected: tuple[str, ...]
) -> None:
    found = market_keyword_phrases(target_url=url, target_title=title, taxonomy=taxonomy_v3())
    assert found == expected


def test_the_anchor_is_the_market_phrase_plus_the_word_market() -> None:
    anchors = market_keyword_anchors(
        target_url=f"{KEN}/industry-reports/india-cosmetics-market",
        target_title="India Cosmetics Market Share, Companies & Trends Report 2025-2032",
        taxonomy=taxonomy_v3(),
        policy=policy(),
    )
    assert [(p.text, p.source, p.rank) for p in anchors] == [
        ("Cosmetics market", "target_topic", 1)
    ]


def test_the_anchor_never_comes_from_the_broad_market_id() -> None:
    """``market_id`` generalises ("food"), which produced "food market" anchors pointing at a
    vending-machine report. The derivation cannot see ``market_id`` at all."""
    anchors = market_keyword_anchors(
        target_url=f"{KEN}/industry-reports/india-vending-machine-market",
        target_title="India Vending Machine Market Outlook to 2030",
        taxonomy=taxonomy_v3(),
        policy=policy(),
    )
    texts = [p.text for p in anchors]
    assert "Vending Machine market" in texts
    assert all("food" not in text.casefold() for text in texts)


def test_a_geography_qualified_anchor_ranks_first_and_can_be_required() -> None:
    kwargs: dict[str, object] = {
        "target_url": f"{KEN}/industry-reports/india-cosmetics-market",
        "target_title": "India Cosmetics Market Share, Companies & Trends Report",
        "taxonomy": taxonomy_v3(),
        "policy": policy(),
        "geography_terms": ("India", "indian"),
    }
    optional = market_keyword_anchors(**kwargs)  # type: ignore[arg-type]
    assert [p.text for p in optional][:2] == ["India Cosmetics market", "indian Cosmetics market"]
    assert "Cosmetics market" in [p.text for p in optional]
    assert {p.rank for p in optional} == {0, 1}

    required = market_keyword_anchors(require_geography=True, **kwargs)  # type: ignore[arg-type]
    assert all(p.text.casefold().startswith(("india ", "indian ")) for p in required)
    assert all(p.rank == 0 for p in required)


def test_market_keyword_anchors_obey_the_anchor_rules() -> None:
    """A noisy editorial title is dropped rather than truncated; the slug rescues the pair."""
    anchors = market_keyword_anchors(
        target_url=f"{KEN}/case-studies/catering-market-entry-strategy-ksa-data-assessment",
        target_title="How a Global Food Leader Found Many Opportunities in KSA Catering Market",
        taxonomy=taxonomy_v3(),
        policy=policy(),
    )
    assert [p.text for p in anchors] == ["catering market"]


def test_a_target_with_no_market_phrase_of_its_own_gets_no_anchor() -> None:
    """ "market market" is never an anchor, and neither is a bare geography slug."""
    tax = taxonomy_v3()
    assert (
        market_keyword_anchors(
            target_url=None, target_title="Market", taxonomy=tax, policy=policy()
        )
        == ()
    )
    assert (
        market_keyword_anchors(
            target_url=f"{KEN}/india-market", target_title=None, taxonomy=tax, policy=policy()
        )
        == ()
    )


def test_slug_text_reads_the_last_segment_with_letters() -> None:
    assert slug_text(f"{KEN}/industry-reports/india-cosmetics-market") == "india cosmetics market"
    assert slug_text(f"{KEN}/india-cosmetics-market/2025") == "india cosmetics market"
    assert slug_text(f"{KEN}/") is None
    assert slug_text(None) is None


# --------------------------------------------------------------------------- #
# The "<market> market" fragment guard (owner's 2026-10-30 review)
# --------------------------------------------------------------------------- #
def test_market_phrases_include_the_suffixed_forms() -> None:
    """Without "<label> market" the guard misses an anchor that overlaps a longer name."""
    phrases = set(market_phrases(taxonomy()))
    assert "solar power" in phrases
    assert "solar power market" in phrases


def test_an_anchor_is_not_taken_out_of_a_longer_market_name() -> None:
    """ "The India Cloud Gaming and [[Esports Market]] is projected to ..." misreads the
    sentence: the anchor is a slice of the SOURCE's own market name."""
    body = blocks(
        INTRO + "<p>The India Cloud Gaming and Esports Market is projected to expand sharply.</p>"
    )
    assert _enclosed(body) is None


def test_the_same_phrase_is_placeable_in_ordinary_prose() -> None:
    """The guard must not refuse the anchor where it is NOT inside a longer name."""
    body = blocks(
        INTRO + "<p>Sleepwell is a leader in the esports market across the region today.</p>"
    )
    placed = _enclosed(body)
    assert placed is not None
    assert placed.anchor_text == "esports market"


def _enclosed(body: Sequence[ContentBlock]) -> PlacementCandidate | None:
    return find_placement(
        blocks=body,
        phrases=(_phrase("esports market"),),
        links_per_block={},
        blocks_linking_target=(),
        taken_spans=(),
        salience={},
        policy=policy(),
        enclosing_phrases=("cloud gaming and esports market",),
    )


# --------------------------------------------------------------------------- #
# Page titles as a fragment guard (the cross-sell list defect, 18% of 1,028 plans)
# --------------------------------------------------------------------------- #
def _with_titles(body: Sequence[ContentBlock], titles: Sequence[str]) -> PlacementCandidate | None:
    return find_placement(
        blocks=body,
        phrases=(_phrase("furniture market"),),
        links_per_block={},
        blocks_linking_target=(),
        taken_spans=(),
        salience={},
        policy=policy(),
        enclosing_phrases=corpus_title_phrases(titles),
    )


def test_an_anchor_is_not_taken_from_another_reports_title() -> None:
    """The real defect: a report ends with a cross-sell list of OTHER reports, one title per
    list item, and the engine anchored on the tail of one -- "Thailand Residential
    [[Furniture Market]]" pointing at a GERMANY furniture report."""
    body = blocks(INTRO + "<ul><li>Thailand Residential Furniture Market</li></ul>")
    assert _with_titles(body, ["Thailand Residential Furniture Market"]) is None


def test_prose_is_still_placeable_when_titles_are_guarded() -> None:
    """The guard is occurrence-based: a title blocks the anchor only where that title
    actually appears. "the German furniture market grew" does not contain the title
    "Germany Furniture Market", so the anchor stands."""
    body = blocks(INTRO + "<p>Analysts say the German furniture market grew again in 2024.</p>")
    placed = _with_titles(body, ["Thailand Residential Furniture Market"])
    assert placed is not None
    assert placed.anchor_text == "furniture market"


def test_a_short_title_is_not_used_as_a_guard() -> None:
    """A two-word title IS usually the right anchor. Guarding on "Furniture Market" would
    block the anchor everywhere the phrase occurs, including legitimate prose."""
    assert corpus_title_phrases(["Furniture Market"]) == ()
    body = blocks(INTRO + "<p>The furniture market expanded across the region.</p>")
    assert _with_titles(body, ["Furniture Market"]) is not None


def test_a_cross_sell_list_entry_is_not_prose_and_hosts_no_anchor() -> None:
    """The defect the corpus-title guard could NOT catch, because these entries link to
    pages outside the inventory, so their titles are unknown to us."""
    for title in (
        "KSA Modular Kitchen Furniture Market",
        "Kuwait E-commerce Furniture Market",
        "Thailand Residential Furniture Market",
        "Indonesia Payments Market Size, Share & Forecast, By Payment Method, "
        "Customer Segment & Distribution Channel",
    ):
        assert not block_is_prose(title), title
        body = blocks(INTRO + f"<ul><li>{title}</li></ul>")
        assert _with_titles(body, []) is None, title


def test_a_list_item_written_as_a_sentence_is_prose() -> None:
    """The rule must not exclude list items as a kind -- only labels."""
    text = "Fleet managers cite the furniture market as their main planning input."
    assert block_is_prose(text)
    placed = _with_titles(blocks(INTRO + f"<ul><li>{text}</li></ul>"), [])
    assert placed is not None
    assert placed.anchor_text == "furniture market"


def test_a_long_unpunctuated_block_is_still_prose() -> None:
    """Punctuation alone was too blunt: a long paragraph without a full stop is not a
    title. Only short, unpunctuated blocks are labels."""
    long_text = " ".join(f"word{n:03d}" for n in range(MAX_LABEL_WORDS + 5))
    assert block_is_prose(long_text)
    assert not block_is_prose("Short Unpunctuated Label")


def test_corpus_title_phrases_normalises_titles() -> None:
    phrases = corpus_title_phrases(
        [
            "Edible Oil Manufacturing &amp; Refining Market",  # entities decoded
            "India EV Market: Outlook to 2030",  # cut at the separator
            "  Spaced   Out   Title  ",  # whitespace collapsed
            None,
            "",
            "Edible Oil Manufacturing & Refining Market",  # duplicate of the first
        ]
    )
    assert "Edible Oil Manufacturing & Refining Market" in phrases
    assert not any("&amp;" in p for p in phrases)
    assert "India EV Market" in phrases
    assert "Spaced Out Title" in phrases
    assert len(phrases) == len(set(phrases)), phrases
