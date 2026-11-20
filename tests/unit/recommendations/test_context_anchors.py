"""ADR-0029 context anchors: the fallback when the target's market keyword is absent.

The decisive property is not that more links appear -- it is that a context anchor can never
tell the reader a link is local when it is not, and can never slice through somebody's name.
"""

from __future__ import annotations

from typing import Any

import pytest

from ken_contracts.intelligence import TaxonomyPolicy
from ken_contracts.recommendations import RecommendationPolicy
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.recommendations.pipeline import load_policies
from ken_services.recommendations.placement import (
    CONTEXT_ANCHOR_RANK,
    AnchorPhrase,
    _names_a_longer_entity,
    market_keyword_anchors,
    market_subphrases,
)
from tests.conftest import make_settings

Fixture = tuple[RecommendationPolicy, TaxonomyPolicy]

TAXONOMY = "policies/taxonomy/taxonomy.v1.yaml"
QATAR_URL = "https://x.test/industry-reports/qatar-tire-distribution-and-retail-service-ecosystem"
QATAR_TITLE = "Qatar Tire Distribution and Retail Service Ecosystem Market"


@pytest.fixture(scope="module")
def policy_and_taxonomy() -> tuple[RecommendationPolicy, TaxonomyPolicy]:
    settings = make_settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/x",
        model_provider="mock",
        taxonomy_path=TAXONOMY,
    )
    policy, _ranking = load_policies(settings)
    return policy, load_taxonomy(TAXONOMY)


def anchors(fixture: Fixture, **overrides: Any) -> tuple[AnchorPhrase, ...]:
    policy, taxonomy = fixture
    kwargs: dict[str, Any] = {
        "target_url": QATAR_URL,
        "target_title": QATAR_TITLE,
        "taxonomy": taxonomy,
        "policy": policy,
        "geography_terms": ("Qatar",),
    }
    return market_keyword_anchors(**{**kwargs, **overrides})


# --------------------------------------------------------------------------- #
# The sub-phrases themselves
# --------------------------------------------------------------------------- #
def test_subphrases_are_runs_of_the_phrase_longest_first() -> None:
    runs = market_subphrases("Commercial Vehicle Leasing and Fleet Management")
    assert "Commercial Vehicle Leasing" in runs
    assert "Fleet Management" in runs
    assert list(runs) == sorted(runs, key=lambda r: -len(r.split())), "longest first"


def test_the_whole_phrase_is_not_one_of_its_own_subphrases() -> None:
    phrase = "Commercial Vehicle Leasing and Fleet Management"
    assert phrase not in market_subphrases(phrase)


def test_a_run_never_begins_or_ends_on_a_stop_word() -> None:
    runs = market_subphrases("Commercial Vehicle Leasing and Fleet Management")
    assert not any(r.lower().startswith(("and ", "the ", "of ")) for r in runs)
    assert not any(r.lower().endswith((" and", " the", " of")) for r in runs)


def test_single_words_are_never_offered() -> None:
    assert all(len(r.split()) >= 2 for r in market_subphrases("Tire Distribution Ecosystem"))


def test_a_two_word_phrase_yields_nothing() -> None:
    assert market_subphrases("Tire Distribution") == ()


# --------------------------------------------------------------------------- #
# Ranking: the market keyword still wins
# --------------------------------------------------------------------------- #
def test_the_market_keyword_outranks_every_context_anchor(policy_and_taxonomy: Fixture) -> None:
    found = anchors(policy_and_taxonomy)
    keyword = [a for a in found if a.rank < CONTEXT_ANCHOR_RANK]
    context = [a for a in found if a.rank >= CONTEXT_ANCHOR_RANK]
    assert keyword and context
    assert max(a.rank for a in keyword) < min(a.rank for a in context)


def test_the_approved_deployment_9_anchor_is_offered(policy_and_taxonomy: Fixture) -> None:
    """The link a human reviewed and kept live, which the engine could not previously propose."""
    found = {a.text for a in anchors(policy_and_taxonomy, require_geography=True)}
    assert "Qatar Tire Distribution market" in found


def test_allow_context_false_restores_the_old_behaviour(policy_and_taxonomy: Fixture) -> None:
    found = anchors(policy_and_taxonomy, allow_context=False)
    assert found, "the market-keyword anchors are still offered"
    assert all(a.rank < CONTEXT_ANCHOR_RANK for a in found)
    assert all(a.context_terms == () for a in found)


# --------------------------------------------------------------------------- #
# Geography honesty -- the owner's boundary, 2026-11-19
# --------------------------------------------------------------------------- #
def test_every_cross_geography_context_anchor_is_labelled_or_block_gated(
    policy_and_taxonomy: Fixture,
) -> None:
    """THE load-bearing property. With require_geography, a context anchor must either name
    the target's geography itself, or carry context_terms so its block has to."""
    for anchor in anchors(policy_and_taxonomy, require_geography=True):
        if anchor.rank < CONTEXT_ANCHOR_RANK:
            continue
        names_it = "qatar" in anchor.text.casefold()
        assert names_it or anchor.context_terms == ("Qatar",), anchor


def test_with_no_geography_terms_no_context_anchor_survives_require_geography(
    policy_and_taxonomy: Fixture,
) -> None:
    """Nothing can name a geography we were given no word for, so nothing is offered."""
    found = anchors(policy_and_taxonomy, geography_terms=(), require_geography=True)
    assert [a for a in found if a.rank >= CONTEXT_ANCHOR_RANK] == []


def test_same_geography_context_anchors_need_no_label(policy_and_taxonomy: Fixture) -> None:
    found = anchors(policy_and_taxonomy, require_geography=False)
    bare = [a for a in found if a.rank >= CONTEXT_ANCHOR_RANK and not a.context_terms]
    assert bare, "a local link may be anchored on the plain phrase"


# --------------------------------------------------------------------------- #
# Proper nouns -- the defect found on the Saudi corpus, 2026-11-19
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "anchor"),
    [
        (
            "The National Strategy for Data and Artificial Intelligence was issued in 2020.",
            "Data and Artificial Intelligence",
        ),
        (
            "issued by the Saudi Data and Artificial Intelligence Authority (SDAIA) in 2020",
            "Saudi Data and Artificial Intelligence",
        ),
        ("the Qatar Tire Distribution Market Report covers", "Qatar Tire Distribution Market"),
    ],
)
def test_a_slice_of_a_proper_name_is_refused(text: str, anchor: str) -> None:
    start = text.index(anchor)
    assert _names_a_longer_entity(text, start, start + len(anchor))


@pytest.mark.parametrize(
    ("text", "anchor"),
    [
        (
            "technologies such as artificial intelligence and cloud computing safeguard data",
            "artificial intelligence",
        ),
        ("growth in the reverse logistics segment continued", "reverse logistics"),
        (
            "Key cities dominating the Qatar Tire Distribution market include",
            "Qatar Tire Distribution market",
        ),
    ],
)
def test_an_ordinary_mention_is_allowed(text: str, anchor: str) -> None:
    start = text.index(anchor)
    assert not _names_a_longer_entity(text, start, start + len(anchor))


def test_sentence_initial_capitalisation_is_not_evidence_of_a_name() -> None:
    text = "Artificial intelligence adoption is rising across the sector."
    assert not _names_a_longer_entity(text, 0, len("Artificial intelligence"))
