"""Related-report modules: the owner's Priority 1, on the surface that can carry it.

ADR-0027 §1c found that inline contextual anchors CANNOT express a regional sibling link.
The anchor must name the target's geography (§4) and may only use text already on the page
(§3), and a Malaysia car-rental report does not contain the phrase "Indonesia car rental
market" -- 104 of 132 sibling pairs (79%) died exactly there. The constraint is structural,
not a threshold to tune.

A related-reports module has none of that problem: the link text is the target's own title,
so it names the geography by construction, and no sentence is rewritten. Ken's pages already
carry such a module -- the cross-sell list whose entries the engine was wrongly anchoring
inside (ADR-0027 consequences). The surface exists; it was being used for the wrong thing.

**This module proposes; it never writes.** It returns ranked items for review. Nothing here
touches a page, and no CMS contract is assumed: how a module is composed on the page is
still open (NEXT_STEPS item 12).

Scope now: **regional siblings only** (same market core, different geography). Adjacency --
"a reader of an EV report wants charging stations and batteries" -- needs the typed market
graph the taxonomy does not yet declare (NEXT_STEPS items 27 and 32) and is deliberately
absent rather than guessed at.

Ranking is deterministic and explainable, in tiers:

1. a region CONTAINING the source's geography (India -> Asia Pacific): a superset of what
   the reader already has, so it always serves them;
2. a global report: the same, one step broader;
3. another geography inside the source's own region (Malaysia -> Indonesia): a plausible
   neighbour for someone comparing entry markets;
4. anything else, i.e. a different region.

Within a tier: targets that need links first (orphan, then underlinked), then commercial
importance, then semantic similarity, then URL. Preferring link-poor targets is the one idea
worth borrowing from WebKnoGraph (arXiv 2606.06106): spread authority instead of piling it
onto pages that already have it. Its own authors report that automatic selection redistributes
authority better but scores worse on coherence, which is why coherence here is a hard gate
(same market core) rather than a score.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from ken_contracts.intelligence import GeographyNode, TaxonomyPolicy
from ken_contracts.recommendations import ReasonCode
from ken_services.intelligence.taxonomy import TaxonomyIndex, taxonomy_index

__all__ = [
    "DEFAULT_MODULE_SIZE",
    "ModuleCandidate",
    "ModuleItem",
    "ModuleSource",
    "regional_sibling_module",
]

DEFAULT_MODULE_SIZE: Final = 6
"""Items in one module. Six is an editorial judgement, not a measurement: enough to show a
reader the market exists elsewhere, few enough to stay a module rather than a sitemap."""

_TIER_CONTAINING_REGION: Final = 0
_TIER_GLOBAL: Final = 1
_TIER_SAME_REGION: Final = 2
_TIER_OTHER: Final = 3

_TIER_CODES: Final[dict[int, tuple[ReasonCode, ...]]] = {
    _TIER_CONTAINING_REGION: (ReasonCode.REGIONAL_SIBLING_MARKET, ReasonCode.REGIONAL_CONTEXT),
    _TIER_GLOBAL: (ReasonCode.REGIONAL_SIBLING_MARKET, ReasonCode.GLOBAL_TARGET),
    _TIER_SAME_REGION: (ReasonCode.REGIONAL_SIBLING_MARKET, ReasonCode.REGIONAL_CONTEXT),
    _TIER_OTHER: (ReasonCode.REGIONAL_SIBLING_MARKET,),
}


@dataclass(frozen=True, slots=True)
class ModuleSource:
    """The page a module is being built for."""

    page_id: uuid.UUID
    market_core: str
    """From ``placement.market_core``: the market identity the siblings must share."""
    geography: GeographyNode | None
    """``None`` = the page carries no specific geography (a global page)."""


@dataclass(frozen=True, slots=True)
class ModuleCandidate:
    """One page that might appear in a module. Inputs only -- no ranking state."""

    page_id: uuid.UUID
    url: str
    title: str | None
    market_core: str | None
    geography: GeographyNode | None
    inbound_links: int = 0
    commercial_importance: int = 0
    similarity: float | None = None
    """Cosine to the source, when an embedding pair was available; ``None`` never ranks
    above a real value."""


@dataclass(frozen=True, slots=True)
class ModuleItem:
    """One ranked module entry, with the reason it is there."""

    page_id: uuid.UUID
    url: str
    title: str | None
    geography_label: str | None
    rank: int
    reason_codes: tuple[ReasonCode, ...]


def _tier(
    source: GeographyNode | None, target: GeographyNode | None, *, taxonomy: TaxonomyPolicy
) -> int:
    if target is None:
        return _TIER_GLOBAL
    if source is None:
        # A global source: any specific geography is equally a narrowing, so one tier.
        return _TIER_OTHER
    index = taxonomy_index(taxonomy)
    if target.kind == "region" and target.id in _ancestry(index, source):
        return _TIER_CONTAINING_REGION
    if _ancestry(index, source) & _ancestry(index, target):
        return _TIER_SAME_REGION
    return _TIER_OTHER


def _ancestry(index: TaxonomyIndex, node: GeographyNode) -> set[str]:
    """``node`` and its region ancestors, WITHOUT the global root.

    The root is an ancestor of every geography, so leaving it in makes the shared-region
    test true for every pair: an Oman page and an Asia Pacific page "share a region", every
    candidate collapses into one tier, and the module degenerates to alphabetical order.
    That is exactly what the first corpus run produced.
    """
    return {
        geo_id
        for geo_id in (node.id, *index.geography_ancestors(node.id))
        if index.has_geography(geo_id) and index.geography(geo_id).kind != "global"
    }


def regional_sibling_module(
    source: ModuleSource,
    candidates: Iterable[ModuleCandidate],
    *,
    taxonomy: TaxonomyPolicy,
    limit: int = DEFAULT_MODULE_SIZE,
) -> tuple[ModuleItem, ...]:
    """The source's regional siblings, ranked, at most ``limit``. Pure and deterministic.

    A candidate qualifies only if its market core EQUALS the source's and its geography
    differs. Equal geography is not a sibling -- it is a duplicate of what the reader is
    already on -- and a different core is a different market, which is the distinction the
    owner's original rejections turned on.
    """
    eligible: list[tuple[tuple[int, int, int, float, str], ModuleCandidate, int]] = []
    for candidate in candidates:
        if candidate.page_id == source.page_id:
            continue
        if candidate.market_core is None or candidate.market_core != source.market_core:
            continue
        source_id = None if source.geography is None else source.geography.id
        target_id = None if candidate.geography is None else candidate.geography.id
        if source_id == target_id:
            continue
        tier = _tier(source.geography, candidate.geography, taxonomy=taxonomy)
        # Negated so that "needs links most" and "most valuable" sort first under a plain
        # ascending sort; the URL is the final tiebreak, so the order never depends on the
        # candidate iteration order.
        key = (
            tier,
            0 if candidate.inbound_links == 0 else 1,
            -candidate.commercial_importance,
            -(candidate.similarity if candidate.similarity is not None else -1.0),
            candidate.url,
        )
        eligible.append((key, candidate, tier))

    eligible.sort(key=lambda item: item[0])
    items: list[ModuleItem] = []
    for rank, (_, candidate, tier) in enumerate(eligible[:limit], start=1):
        codes = _TIER_CODES[tier]
        if candidate.inbound_links == 0:
            codes = (*codes, ReasonCode.ORPHAN_TARGET)
        items.append(
            ModuleItem(
                page_id=candidate.page_id,
                url=candidate.url,
                title=candidate.title,
                geography_label=None if candidate.geography is None else candidate.geography.label,
                rank=rank,
                reason_codes=codes,
            )
        )
    return tuple(items)


def module_sizes(modules: Sequence[Sequence[ModuleItem]]) -> dict[str, int]:
    """Counts for a run summary: how many pages got a module, and how full they were."""
    filled = [m for m in modules if m]
    return {
        "pages_considered": len(modules),
        "pages_with_a_module": len(filled),
        "items_total": sum(len(m) for m in filled),
        "pages_with_a_full_module": sum(1 for m in filled if len(m) >= DEFAULT_MODULE_SIZE),
    }
