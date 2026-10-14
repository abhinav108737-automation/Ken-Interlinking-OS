"""Deterministic rationale rendering and sanitisation."""

from __future__ import annotations

import pytest

from ken_contracts.intelligence import RelationshipType
from ken_contracts.recommendations import ReasonCode
from ken_services.recommendations.rationale import (
    ANCHOR_MAX,
    REASON_TEXT,
    RELATIONSHIP_TEXT,
    TITLE_MAX,
    render_rationale,
    sanitise,
)

C = ReasonCode


def test_exact_rendering() -> None:
    text = render_rationale(
        reason_codes=[C.ORPHAN_TARGET, C.SAME_MARKET, C.ANCHOR_MATCHES_TARGET_TOPIC],
        target_title="India Solar Power Market Report",
        relationship_type=RelationshipType.SAME_MARKET,
        anchor_text="solar power",
    )
    assert text == (
        'Link to "India Solar Power Market Report" (same market) on the existing phrase '
        '"solar power". Why: both pages cover the same market; no page links to the target '
        "yet; the anchor names the target's topic."
    )


def test_order_and_duplicates_never_change_the_output() -> None:
    codes = [C.UNDERLINKED_TARGET, C.SEMANTICALLY_SIMILAR, C.UNDERLINKED_TARGET]
    a = render_rationale(
        reason_codes=codes, target_title="T", relationship_type=None, anchor_text="x y"
    )
    b = render_rationale(
        reason_codes=list(reversed(codes)),
        target_title="T",
        relationship_type=None,
        anchor_text="x y",
    )
    assert a == b
    assert a.endswith("Why: the pages are semantically similar; the target has few internal links.")


def test_missing_title_relationship_and_codes() -> None:
    text = render_rationale(
        reason_codes=(), target_title=None, relationship_type=None, anchor_text="cold chain"
    )
    assert text == 'Link to the target page on the existing phrase "cold chain".'
    blank = render_rationale(
        reason_codes=(), target_title="  \n ", relationship_type=None, anchor_text="cold chain"
    )
    assert blank == text


def test_page_text_is_sanitised() -> None:
    text = render_rationale(
        reason_codes=[C.SAME_MARKET],
        target_title='Evil "quoted"\x00 title\n\twith​ breaks',
        relationship_type=RelationshipType.SUBMARKET,
        anchor_text="  solar\n power ",
    )
    assert text.startswith(
        "Link to \"Evil 'quoted' title with breaks\" (submarket) on the existing phrase "
        '"solar power".'
    )
    assert "\x00" not in text and "\n" not in text and "​" not in text


def test_long_page_text_is_capped_at_a_word_boundary() -> None:
    long_title = " ".join(["word"] * 100)
    text = render_rationale(
        reason_codes=(), target_title=long_title, relationship_type=None, anchor_text="x" * 500
    )
    quoted_title = text.split('"')[1]
    assert len(quoted_title) <= TITLE_MAX
    assert quoted_title.endswith("…")
    assert len(text.split('"')[3]) <= ANCHOR_MAX


@pytest.mark.parametrize(
    ("raw", "limit", "expected"),
    [
        (None, 10, ""),
        ("", 10, ""),
        ("a“b”c", 10, "a'b'c"),
        ("Café", 10, "Café"),
        ("one two three", 7, "one…"),
    ],
)
def test_sanitise(raw: str | None, limit: int, expected: str) -> None:
    assert sanitise(raw, limit=limit) == expected


def test_every_code_and_relationship_has_a_template() -> None:
    assert set(REASON_TEXT) == set(ReasonCode)
    assert set(RELATIONSHIP_TEXT) == set(RelationshipType)
    for relationship in RelationshipType:
        text = render_rationale(
            reason_codes=list(ReasonCode),
            target_title="T",
            relationship_type=relationship,
            anchor_text="a b",
        )
        assert f"({RELATIONSHIP_TEXT[relationship]})" in text
        assert text.count(";") == len(ReasonCode) - 1
