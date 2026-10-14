"""The analysis job's pair prefilter never drops a pair a MATERIALISED type would relate.

``ken_services.intelligence.pipeline.taxonomy_relationship_keys`` mirrors the
materialised rules in ``ken_services.intelligence.relationships`` (the coarse
derived-only types are never stored, so the prefilter ignores them). This guard
evaluates the REAL rules exhaustively over synthetic analyses spanning the real
taxonomy policy, and checks every ordered pair related by a materialised type
passes the prefilter, while pairs related ONLY by coarse types are pruned.
"""

from __future__ import annotations

import itertools
import uuid
from pathlib import Path

import pytest

from ken_contracts.intelligence import (
    GeographyMention,
    Intent,
    PageAnalysis,
    TaxonomyPolicy,
    analysis_version,
)
from ken_services.intelligence.pipeline import taxonomy_relationship_keys

POLICY = Path(__file__).resolve().parents[2] / "policies" / "taxonomy" / "taxonomy.v1.yaml"


@pytest.fixture(scope="module")
def taxonomy() -> TaxonomyPolicy:
    from ken_services.intelligence.taxonomy import load_taxonomy

    return load_taxonomy(POLICY)


def _analyses(taxonomy: TaxonomyPolicy) -> list[PageAnalysis]:
    parents = {m.id: m.parent for m in taxonomy.markets}

    def root(node: str) -> str:
        while parents[node] is not None:
            node = parents[node]  # type: ignore[assignment]
        return node

    geos = [g for g in taxonomy.geographies if g.kind in ("country", "region")]
    geo_options: list[tuple[GeographyMention, ...]] = [()]
    geo_options += [
        (GeographyMention(geo_id=g.id, label=g.label, kind=g.kind, score=1.0),) for g in geos
    ]
    out: list[PageAnalysis] = []
    markets: list[str | None] = [None, *(m.id for m in taxonomy.markets)]
    for n, (market, geographies) in enumerate(itertools.product(markets, geo_options)):
        out.append(
            PageAnalysis(
                page_id=uuid.UUID(int=n + 1),
                page_version=1,
                analysis_version=analysis_version(taxonomy.version),
                page_type="report",
                intent=Intent.COMMERCIAL_RESEARCH,
                commercial_importance=1,
                market_id=market,
                industry_id=None if market is None else root(market),
                geographies=geographies,
            )
        )
    return out


def test_the_prefilter_pairs_exactly_the_materialised_relations(
    taxonomy: TaxonomyPolicy,
) -> None:
    from ken_services.intelligence.relationships import (
        DERIVED_ONLY_RELATIONSHIP_TYPES,
        MATERIALISED_RELATIONSHIP_TYPES,
        taxonomy_relationships,
    )

    materialised = set(MATERIALISED_RELATIONSHIP_TYPES)
    analyses = _analyses(taxonomy)
    keys = {a.page_id: taxonomy_relationship_keys(a, taxonomy) for a in analyses}
    related = skipped = coarse_only = 0
    seen: set[str] = set()
    for source, target in itertools.permutations(analyses, 2):
        is_s, relates_s = keys[source.page_id]
        is_t, relates_t = keys[target.page_id]
        candidate = bool(relates_s & is_t or relates_t & is_s)
        found = taxonomy_relationships(source, target, taxonomy=taxonomy)
        back = taxonomy_relationships(target, source, taxonomy=taxonomy)
        kinds = {c.relationship_type for c in (*found, *back)}
        where = (source.market_id, source.geographies, target.market_id, target.geographies)
        if kinds & materialised:
            related += 1
            seen |= {k.value for k in kinds & materialised}
            assert candidate, where  # never drops a pair a stored type relates
        else:
            # Never pairs pages that only geography / industry adjacency relate:
            # those types are not stored, so evaluating the pair would be waste.
            assert not candidate, where
            skipped += 1
            if kinds:
                assert kinds <= DERIVED_ONLY_RELATIONSHIP_TYPES
                coarse_only += 1
    assert related > 0
    assert skipped > 0
    assert coarse_only > 0  # the policy really has pairs related only by coarse types
    # Every market-tree type was exercised against the real policy.
    assert seen >= {"same_market", "parent_market", "submarket", "related_market"}
