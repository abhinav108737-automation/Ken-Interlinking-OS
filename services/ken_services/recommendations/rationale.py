"""Deterministic rationale sentences rendered from reason codes (ADR-0011 section 4).

The ONLY page-derived text in a rationale is the quoted anchor and the quoted target
title, both sanitised (:func:`sanitise`): control and format characters removed, double
quotes replaced, whitespace collapsed, length capped at a word boundary. Everything else
comes from fixed templates, so the same inputs always render the same string.

Shape::

    Link to "<title>" (<relationship>) on the existing phrase "<anchor>". Why: <code>; <code>.

The relationship clause is omitted without a relationship type; "the target page" stands in
for a missing title; the "Why" sentence is omitted without codes. Codes are deduplicated and
rendered in :class:`~ken_contracts.recommendations.ReasonCode` declaration order.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from typing import Final

from ken_contracts.intelligence import RelationshipType
from ken_contracts.recommendations import ReasonCode
from ken_services.intelligence.text import truncate_words

__all__ = [
    "ANCHOR_MAX",
    "REASON_TEXT",
    "RELATIONSHIP_TEXT",
    "TITLE_MAX",
    "render_rationale",
    "sanitise",
]

TITLE_MAX: Final = 120
ANCHOR_MAX: Final = 120

RELATIONSHIP_TEXT: Final[Mapping[RelationshipType, str]] = {
    RelationshipType.SAME_MARKET: "same market",
    RelationshipType.PARENT_MARKET: "parent market",
    RelationshipType.SUBMARKET: "submarket",
    RelationshipType.RELATED_MARKET: "related market",
    RelationshipType.SAME_GEOGRAPHY: "same geography",
    RelationshipType.REGIONAL_PARENT: "regional context",
    RelationshipType.SUPPLY_CHAIN_UPSTREAM: "upstream in the supply chain",
    RelationshipType.SUPPLY_CHAIN_DOWNSTREAM: "downstream in the supply chain",
    RelationshipType.COMPLEMENTARY_MARKET: "complementary market",
    RelationshipType.INDUSTRY_ADJACENCY: "adjacent industry",
    RelationshipType.SEMANTIC_SIMILAR: "semantically similar",
}

REASON_TEXT: Final[Mapping[ReasonCode, str]] = {
    ReasonCode.SEMANTICALLY_SIMILAR: "the pages are semantically similar",
    ReasonCode.LEXICAL_OVERLAP: "the pages share key terms",
    ReasonCode.SAME_MARKET: "both pages cover the same market",
    ReasonCode.PARENT_MARKET: "the target covers the parent market",
    ReasonCode.SUBMARKET: "the target covers a submarket",
    ReasonCode.RELATED_MARKET: "the target covers a related market",
    ReasonCode.SUPPLY_CHAIN: "the markets are linked in the supply chain",
    ReasonCode.COMPLEMENTARY_MARKET: "the markets are complementary",
    ReasonCode.INDUSTRY_ADJACENCY: "the industries are adjacent",
    ReasonCode.SAME_GEOGRAPHY: "both pages cover the same country",
    ReasonCode.REGIONAL_CONTEXT: "the target adds regional context",
    ReasonCode.HIGH_COMMERCIAL_VALUE: "the target is a high-value commercial page",
    ReasonCode.UNDERLINKED_TARGET: "the target has few internal links",
    ReasonCode.ORPHAN_TARGET: "no page links to the target yet",
    ReasonCode.ANCHOR_MATCHES_TARGET_TOPIC: "the anchor names the target's topic",
    ReasonCode.ANCHOR_MATCHES_TARGET_TITLE: "the anchor matches the target's title",
}
_ORDER: Final = {code: n for n, code in enumerate(ReasonCode)}


def sanitise(text: str | None, *, limit: int) -> str:
    """Page text made safe to quote: NFC; control/format characters dropped; double quotes
    replaced by single quotes; whitespace collapsed; at most ``limit`` chars (word boundary,
    with an ellipsis when cut)."""
    if not text:
        return ""
    normalised = unicodedata.normalize("NFC", text)
    kept = "".join(
        " " if char.isspace() else char
        for char in normalised
        if char.isspace() or unicodedata.category(char)[0] != "C"
    )
    for quote in ('"', "“", "”"):
        kept = kept.replace(quote, "'")
    return truncate_words(" ".join(kept.split()), limit)


def render_rationale(
    *,
    reason_codes: Sequence[ReasonCode],
    target_title: str | None,
    relationship_type: RelationshipType | None,
    anchor_text: str,
) -> str:
    """The deterministic rationale of a plan (module docstring)."""
    title = sanitise(target_title, limit=TITLE_MAX)
    anchor = sanitise(anchor_text, limit=ANCHOR_MAX)
    target = f'"{title}"' if title else "the target page"
    relation = f" ({RELATIONSHIP_TEXT[relationship_type]})" if relationship_type else ""
    lead = f'Link to {target}{relation} on the existing phrase "{anchor}".'
    codes = sorted(set(reason_codes), key=_ORDER.__getitem__)
    if not codes:
        return lead
    reasons = "; ".join(REASON_TEXT[code] for code in codes)
    return f"{lead} Why: {reasons}."
