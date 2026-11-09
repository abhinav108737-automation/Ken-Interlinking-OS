"""Hard GATES: a pair that fails one is REFUSED, never merely down-scored.

Phase 3's first cut treated geography as a score contribution, so a reader researching
Indonesia could still be sent to an India report because the rest of the score carried the
pair. 89% of the first real link plans crossed a geography boundary. Two gates run after the
cheap pair filters (:mod:`ken_services.recommendations.filters`) and BEFORE scoring; both are
pure, deterministic and need no model.

**Primary geography** (:func:`primary_geography`). A page's geography is the HIGHEST-SCORED
entry of ``page_analyses.geographies`` (ties broken by ``geo_id``), never any country that
merely happens to be mentioned: a West-Africa cashew report that cites India once is not an
India page. Analysis already weights the title and headings, so the primary entry is the page's
own subject.

**Geography gate** (:func:`geography_gate`), source S -> target T, on the primary geography
of each (``None`` or ``kind == "global"`` = global):

===================================================  ==========================================
S, T                                                 decision
===================================================  ==========================================
same country (or the same region node)               ALLOW ``same_country`` / ``same_region``
country -> a region containing it (taxonomy)         ALLOW ``target_region_contains_source``
region -> a country (or region) inside it            ALLOW ``source_region_contains_target``
any -> global target                                 ALLOW ``global_target``
different nodes sharing a region                     ALLOW ``cross_geography_mentioned`` ONLY
                                                     if the source text names T's geography
global source -> a specific geography                ALLOW ``global_source_mentioned`` ONLY if
                                                     the source text names it
different region                                     REFUSE
===================================================  ==========================================

"Sharing a region" ignores ``global``: India and Germany share only ``global`` and are
therefore in different regions, while India and Vietnam share ``asia-pacific``. A mention is a
whole-phrase, case-insensitive occurrence of the geography's label or of an alias of at least
:data:`MIN_MENTION_ALIAS_CHARS` characters -- short aliases ("sea", "us", "uk") are too
ambiguous to prove a page is about a place.

:attr:`GeographyVerdict.anchor_must_name_target_geography` is true whenever T's geography is
neither S's own nor a container of it: such a link must be labelled ("India cosmetics market"),
or a cross-geography link is indistinguishable from a local one.

**Market gate** (:func:`market_gate`). The pair survives only with a declared taxonomy
relation: same market, parent/child, supply chain (upstream / downstream) or complementary
(:data:`MARKET_GATE_TYPES`). Sibling markets ("related market"), industry adjacency, a missing
``market_id`` and -- above all -- shared words never qualify: edible "nut and seeds" and
agricultural "vegetable seed" share a word and are different markets.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ken_contracts.intelligence import (
    GeographyMention,
    GeographyNode,
    PageAnalysis,
    RelationshipType,
    TaxonomyPolicy,
)
from ken_contracts.recommendations import ReasonCode
from ken_services.intelligence.relationships import taxonomy_relationships
from ken_services.intelligence.taxonomy import TaxonomyIndex, taxonomy_index
from ken_services.intelligence.text import fold
from ken_services.recommendations.placement import phrase_occurrences

__all__ = [
    "GEOGRAPHY_SCORE_KEYS",
    "MARKET_GATE_TYPES",
    "MIN_MENTION_ALIAS_CHARS",
    "GeographyDecision",
    "GeographyVerdict",
    "geography_gate",
    "geography_terms",
    "market_gate",
    "mentions_geography",
    "primary_geography",
]

MIN_MENTION_ALIAS_CHARS: Final = 4
"""An alias shorter than this is never evidence of a mention (the label always counts)."""

MARKET_GATE_TYPES: Final[frozenset[RelationshipType]] = frozenset(
    {
        RelationshipType.SAME_MARKET,
        RelationshipType.PARENT_MARKET,
        RelationshipType.SUBMARKET,
        RelationshipType.SUPPLY_CHAIN_UPSTREAM,
        RelationshipType.SUPPLY_CHAIN_DOWNSTREAM,
        RelationshipType.COMPLEMENTARY_MARKET,
    }
)
"""The only relations that make two markets relatable (module docstring)."""


class GeographyDecision(StrEnum):
    """Which row of the geography gate allowed the pair."""

    SAME_COUNTRY = "same_country"
    SAME_REGION = "same_region"
    """The two pages carry the SAME region node."""
    TARGET_REGION_CONTAINS_SOURCE = "target_region_contains_source"
    SOURCE_REGION_CONTAINS_TARGET = "source_region_contains_target"
    GLOBAL_TARGET = "global_target"
    CROSS_GEOGRAPHY_MENTIONED = "cross_geography_mentioned"
    """Different geographies in one region, and the source text names the target's."""
    GLOBAL_SOURCE_MENTIONED = "global_source_mentioned"
    """A global source naming the target's specific geography."""
    REGIONAL_SIBLING = "regional_sibling"
    """Different REGIONS, but the SAME market: "Germany EV market" -> "India EV market".
    Allowed without any mention in the source text, because :data:`_LABELLED` obliges the
    anchor to name the target's geography. ADR-0027 §1 amendment (owner, 2026-11-01)."""


GEOGRAPHY_SCORE_KEYS: Final[Mapping[GeographyDecision, str]] = {
    GeographyDecision.SAME_COUNTRY: "same_country",
    GeographyDecision.SAME_REGION: "same_region",
    GeographyDecision.TARGET_REGION_CONTAINS_SOURCE: "region_contains_source",
    GeographyDecision.SOURCE_REGION_CONTAINS_TARGET: "same_region",
    GeographyDecision.GLOBAL_TARGET: "global_target",
    GeographyDecision.CROSS_GEOGRAPHY_MENTIONED: "same_region",
    GeographyDecision.GLOBAL_SOURCE_MENTIONED: "global_target",
    GeographyDecision.REGIONAL_SIBLING: "global_target",
}
"""Decision -> ``policy.geography_scores`` key, so the (now purely informational) geography
component keeps reporting a value the policy owns. A global source reaching into one country
is the weakest allowed affinity and shares the ``global_target`` value."""

_DECISION_CODES: Final[Mapping[GeographyDecision, tuple[ReasonCode, ...]]] = {
    GeographyDecision.SAME_COUNTRY: (ReasonCode.SAME_GEOGRAPHY,),
    GeographyDecision.SAME_REGION: (ReasonCode.REGIONAL_CONTEXT,),
    GeographyDecision.TARGET_REGION_CONTAINS_SOURCE: (ReasonCode.REGIONAL_CONTEXT,),
    GeographyDecision.SOURCE_REGION_CONTAINS_TARGET: (ReasonCode.REGIONAL_CONTEXT,),
    GeographyDecision.GLOBAL_TARGET: (ReasonCode.GLOBAL_TARGET,),
    GeographyDecision.CROSS_GEOGRAPHY_MENTIONED: (ReasonCode.CROSS_GEOGRAPHY_SUPPORTED,),
    GeographyDecision.GLOBAL_SOURCE_MENTIONED: (ReasonCode.CROSS_GEOGRAPHY_SUPPORTED,),
    GeographyDecision.REGIONAL_SIBLING: (ReasonCode.REGIONAL_SIBLING_MARKET,),
}

_LABELLED: Final[frozenset[GeographyDecision]] = frozenset(
    {
        GeographyDecision.SOURCE_REGION_CONTAINS_TARGET,
        GeographyDecision.CROSS_GEOGRAPHY_MENTIONED,
        GeographyDecision.GLOBAL_SOURCE_MENTIONED,
        GeographyDecision.REGIONAL_SIBLING,
    }
)
"""Decisions whose target is NARROWER than, or outside, the source's geography: the anchor
must name the target's geography.

For :attr:`GeographyDecision.REGIONAL_SIBLING` this is the ONLY protection, and it is what
makes the ADR-0027 §1 amendment safe. The owner's Brazil case: a page whose text reads
"Brazil Niche Fragrance Market" linked to the GLOBAL fragrance report on the bare anchor
"fragrance market", so the reader was promised Brazil and given the world. Membership here
forces "global fragrance market" instead, or no link at all."""


@dataclass(frozen=True, slots=True)
class GeographyVerdict:
    """Why the geography gate allowed one pair, and what the anchor must do about it."""

    decision: GeographyDecision
    source_geography: GeographyNode | None
    """``None`` = the source page carries no specific geography (global)."""
    target_geography: GeographyNode | None
    reason_codes: tuple[ReasonCode, ...]
    geography_score_key: str
    """The ``policy.geography_scores`` key this decision scores under."""

    @property
    def anchor_must_name_target_geography(self) -> bool:
        return self.decision in _LABELLED


# --------------------------------------------------------------------------- #
# Primary geography
# --------------------------------------------------------------------------- #
def _best_mention(mentions: Iterable[GeographyMention]) -> GeographyMention | None:
    ranked = sorted(mentions, key=lambda m: (-m.score, m.geo_id))
    return ranked[0] if ranked else None


def primary_geography(analysis: PageAnalysis, *, taxonomy: TaxonomyPolicy) -> GeographyNode | None:
    """The page's own geography: the highest-scored mention known to ``taxonomy``.

    Ties break on ``geo_id`` (deterministic). ``None`` when the page lists none, when none is
    in this taxonomy, or when the best one is ``global``: all three mean "no specific
    geography", which is how a global page looks. Incidental mentions never win.
    """
    index = taxonomy_index(taxonomy)
    best = _best_mention(m for m in analysis.geographies if index.has_geography(m.geo_id))
    if best is None:
        return None
    node = index.geography(best.geo_id)
    return None if node.kind == "global" else node


def geography_terms(node: GeographyNode) -> tuple[str, ...]:
    """The phrases that name ``node`` in prose: its LABEL first, then its longer aliases.

    The label leads because these terms also build the geography-qualified anchor, and the
    label is the node's editorial spelling ("India cosmetics market", not "indian cosmetics
    market"); aliases follow longest-first, then alphabetically (deterministic). Aliases
    shorter than :data:`MIN_MENTION_ALIAS_CHARS` are dropped: "sea" is not evidence of
    Southeast Asia.
    """
    label = node.label.strip()
    aliases = {
        alias.strip()
        for alias in node.aliases
        if len(alias.strip()) >= MIN_MENTION_ALIAS_CHARS and fold(alias) != fold(label)
    }
    ordered = sorted((a for a in aliases if a), key=lambda t: (-len(t), t.casefold()))
    return tuple(term for term in (label, *ordered) if term)


def mentions_geography(text: str, node: GeographyNode) -> bool:
    """``text`` contains a whole-phrase, case-insensitive occurrence of a term of ``node``."""
    return any(phrase_occurrences(term, text) for term in geography_terms(node))


# --------------------------------------------------------------------------- #
# Geography gate
# --------------------------------------------------------------------------- #
def _regions_of(index: TaxonomyIndex, node: GeographyNode) -> frozenset[str]:
    """The non-global regions ``node`` belongs to (a region includes itself)."""
    own = {node.id} if node.kind == "region" else set()
    return frozenset(
        {
            geo_id
            for geo_id in (*own, *index.geography_ancestors(node.id))
            if index.has_geography(geo_id) and index.geography(geo_id).kind == "region"
        }
    )


def _contains(index: TaxonomyIndex, container: GeographyNode, inner: GeographyNode) -> bool:
    return container.kind == "region" and container.id in index.geography_ancestors(inner.id)


def _verdict(
    decision: GeographyDecision,
    source: GeographyNode | None,
    target: GeographyNode | None,
) -> GeographyVerdict:
    return GeographyVerdict(
        decision=decision,
        source_geography=source,
        target_geography=target,
        reason_codes=_DECISION_CODES[decision],
        geography_score_key=GEOGRAPHY_SCORE_KEYS[decision],
    )


_CONDITIONAL: Final[frozenset[GeographyDecision]] = frozenset(
    {GeographyDecision.CROSS_GEOGRAPHY_MENTIONED, GeographyDecision.GLOBAL_SOURCE_MENTIONED}
)
"""Decisions that hold only while the source text names the target's geography."""


def geography_relation(
    source: PageAnalysis,
    target: PageAnalysis,
    *,
    taxonomy: TaxonomyPolicy,
    same_market_core: bool = False,
) -> GeographyDecision | None:
    """The STRUCTURAL relation of the two primary geographies, ignoring the source text.

    ``None`` = different regions AND different markets, which the gate refuses. The two
    :data:`_CONDITIONAL` decisions are returned as if their mention condition held:
    :func:`geography_gate` applies it. One structure, so the gate and the (informational)
    geography score cannot diverge.

    ``same_market_core`` says the two pages are the SAME market (their market cores match,
    e.g. "/germany-ev-market" and "/india-ev-market"). It is what makes a different-region
    pair a :attr:`GeographyDecision.REGIONAL_SIBLING` rather than a refusal.

    Why it defaults to false: the caller must have established market identity. A silent
    default of true would turn the amendment into "any cross-region link is fine", which is
    the pattern the owner rejected ("Indonesia natural personal care -> India cosmetics").
    """
    index = taxonomy_index(taxonomy)
    s = primary_geography(source, taxonomy=taxonomy)
    t = primary_geography(target, taxonomy=taxonomy)
    if t is None:
        return GeographyDecision.GLOBAL_TARGET
    if s is None:
        return GeographyDecision.GLOBAL_SOURCE_MENTIONED
    if s.id == t.id:
        if t.kind == "region":
            return GeographyDecision.SAME_REGION
        return GeographyDecision.SAME_COUNTRY
    if _contains(index, t, s):
        return GeographyDecision.TARGET_REGION_CONTAINS_SOURCE
    if _contains(index, s, t):
        return GeographyDecision.SOURCE_REGION_CONTAINS_TARGET
    # Before the conditional row: the SAME market elsewhere needs no mention, whether the
    # other place is in this region or not. Measured consequence of getting this order
    # wrong: 28 of 132 sibling pairs on a 10-source probe were refused as
    # ``geography_mismatch`` because two countries of one region fell into
    # CROSS_GEOGRAPHY_MENTIONED, which demands the source text name the target -- a
    # Malaysia car-rental report has no reason to write "Indonesia".
    if same_market_core:
        return GeographyDecision.REGIONAL_SIBLING
    if _regions_of(index, s) & _regions_of(index, t):
        return GeographyDecision.CROSS_GEOGRAPHY_MENTIONED
    return None


def geography_gate(
    source: PageAnalysis,
    target: PageAnalysis,
    *,
    taxonomy: TaxonomyPolicy,
    source_text: str,
    same_market_core: bool = False,
) -> GeographyVerdict | None:
    """The gate's decision for one pair, or ``None`` = REFUSE (module docstring).

    ``source_text`` is the source version's block text; it decides only the two conditional
    rows, and is read with whole-phrase matching, never substring matching.

    ``same_market_core`` admits a cross-region pair as a regional sibling: see
    :func:`geography_relation`.
    """
    decision = geography_relation(
        source, target, taxonomy=taxonomy, same_market_core=same_market_core
    )
    if decision is None:
        return None
    s = primary_geography(source, taxonomy=taxonomy)
    t = primary_geography(target, taxonomy=taxonomy)
    if decision in _CONDITIONAL and not (t is not None and mentions_geography(source_text, t)):
        return None
    return _verdict(decision, s, t)


# --------------------------------------------------------------------------- #
# Market gate
# --------------------------------------------------------------------------- #
def market_gate(
    source: PageAnalysis,
    target: PageAnalysis,
    *,
    taxonomy: TaxonomyPolicy,
    semantic_similarity: float | None = None,
    sibling_floor: float | None = None,
) -> tuple[RelationshipType, ...]:
    """The declared relations that relate the two markets, in ``RelationshipType`` order.

    Empty = REFUSE: the markets are unrelated (or at least one page has no market in this
    taxonomy). Only :data:`MARKET_GATE_TYPES` count outright.

    **Siblings** (``related_market``: two markets under one parent) are the owner's
    borderline case, 2026-10-30. "India sun protection" and "India skincare" are siblings and
    a good link; edible "nut and seeds" and agricultural "vegetable seed" are *also* siblings
    and a bad one. The taxonomy alone cannot tell them apart, so a sibling qualifies ONLY when
    the embeddings independently agree the two pages are about the same thing:
    ``semantic_similarity >= sibling_floor``. Both arguments must be supplied, or siblings are
    refused -- the gate fails closed, never on a missing signal.
    """
    relations = tuple(
        rel.relationship_type for rel in taxonomy_relationships(source, target, taxonomy=taxonomy)
    )
    allowed = tuple(rel for rel in relations if rel in MARKET_GATE_TYPES)
    if allowed:
        return allowed
    if RelationshipType.RELATED_MARKET not in relations:
        return ()
    if semantic_similarity is None or sibling_floor is None:
        return ()
    if semantic_similarity < sibling_floor:
        return ()
    return (RelationshipType.RELATED_MARKET,)
