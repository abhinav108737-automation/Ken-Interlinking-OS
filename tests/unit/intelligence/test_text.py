"""Folding, whole-phrase matching, keyword tokens and truncation."""

from __future__ import annotations

import unicodedata

import pytest

from ken_services.intelligence.taxonomy import taxonomy_index
from ken_services.intelligence.text import (
    ELLIPSIS,
    STOPWORDS,
    PhraseMatcher,
    fold,
    tokens,
    truncate_words,
)
from tests.unit.intelligence._support import v1


def _ids(matcher: PhraseMatcher, text: str) -> list[str]:
    return [m.node_id for m in matcher.matches(fold(text))]


def test_fold_is_nfc_casefolded_and_collapsed() -> None:
    decomposed = unicodedata.normalize("NFD", "Café\t  MARKET\n")
    assert fold(decomposed) == "café market"
    assert fold("STRASSE") == fold("straße")  # casefold, not lower


def test_longest_match_first_does_not_double_count() -> None:
    markets = taxonomy_index(v1()).market_matcher
    # "electric two-wheelers" is one two-wheeler mention, never also an EV mention.
    assert _ids(markets, "Electric two-wheelers are growing") == ["electric-two-wheelers"]
    assert _ids(markets, "electric vehicles and electric two-wheeler sales") == [
        "electric-vehicles",
        "electric-two-wheelers",
    ]
    # "lithium-ion batteries" is one mention, not also "li-ion"/"battery cells" pieces.
    assert _ids(markets, "lithium-ion batteries") == ["lithium-ion-batteries"]


def test_longest_wins_even_when_a_shorter_alias_starts_first() -> None:
    matcher = PhraseMatcher({"short": ["a b"], "long": ["b c d"]})
    assert _ids(matcher, "a b c d") == ["long"]


def test_word_boundaries() -> None:
    markets = taxonomy_index(v1()).market_matcher
    assert _ids(markets, "devs love it") == []  # "evs" inside "devs"
    assert _ids(markets, "EVs, evs; (EVS)") == ["electric-vehicles"] * 3
    assert _ids(markets, "fintechs") == []  # plural is not declared
    assert _ids(markets, "pharma's rise") == ["pharmaceuticals"]


def test_hyphen_and_space_variants_only_via_declared_aliases() -> None:
    markets = taxonomy_index(v1()).market_matcher
    assert _ids(markets, "e-commerce") == ["ecommerce"]
    assert _ids(markets, "ecommerce") == ["ecommerce"]
    assert _ids(markets, "e commerce") == []  # not declared
    assert _ids(markets, "electric two wheelers") == []  # only the hyphenated form is declared
    geos = taxonomy_index(v1()).geography_matcher
    assert _ids(geos, "India-based firms") == ["india"]  # a hyphen separates words


def test_case_and_unicode_normalisation() -> None:
    matcher = PhraseMatcher({"cafe": ["Café Market"]})
    assert _ids(matcher, unicodedata.normalize("NFD", "the CAFÉ market")) == ["cafe"]
    assert _ids(matcher, "café\n   market") == ["cafe"]


def test_label_is_an_implicit_alias() -> None:
    markets = taxonomy_index(v1()).market_matcher
    assert _ids(markets, "EV Charging Infrastructure") == ["ev-charging"]
    assert _ids(markets, "Energy & Utilities") == ["energy"]


def test_duplicate_phrase_goes_to_lowest_node_id() -> None:
    matcher = PhraseMatcher({"zeta": ["shared"], "alpha": ["shared"]})
    assert _ids(matcher, "shared") == ["alpha"]


def test_count_and_empty_text() -> None:
    matcher = PhraseMatcher({"x": ["foo"]})
    assert matcher.count("Foo foo FOO") == {"x": 3}
    assert matcher.matches("") == ()
    assert PhraseMatcher({}).count("anything") == {}


def test_tokens() -> None:
    assert tokens("The EV market in 2025: e2w_growth, R&D and AI!") == [
        "market",
        "e2w",
        "growth",
    ]
    assert "the" in STOPWORDS
    assert tokens("") == []


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("short", 10, "short"),
        ("alpha beta gamma", 12, "alpha beta" + ELLIPSIS),
        ("alpha beta gamma", 11, "alpha beta" + ELLIPSIS),
        ("alpha beta gamma", 10, "alpha" + ELLIPSIS),
        ("supercalifragilistic", 6, "super" + ELLIPSIS),
        ("abc", 0, ""),
    ],
)
def test_truncate_words(text: str, limit: int, expected: str) -> None:
    out = truncate_words(text, limit)
    assert out == expected
    assert len(out) <= limit


def test_truncate_words_without_ellipsis() -> None:
    assert truncate_words("alpha beta gamma", 12, ellipsis=False) == "alpha beta"
    assert truncate_words("alpha beta\ngamma", 13, ellipsis=False) == "alpha beta"
