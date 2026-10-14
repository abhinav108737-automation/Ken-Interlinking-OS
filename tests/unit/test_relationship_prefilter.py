"""The analysis job's pair prefilter never drops a pair the relationship rules would relate.

``ken_services.intelligence.pipeline.taxonomy_relationship_keys`` mirrors the
rules in ``ken_services.intelligence.relationships``. This guard evaluates the
REAL rules exhaustively over synthetic analyses spanning the real taxonomy
policy, and checks every related ordered pair passes the prefilter.
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


def test_every_related_pair_passes_the_prefilter(taxonomy: TaxonomyPolicy) -> None:
    from ken_services.intelligence.relationships import taxonomy_relationships

    analyses = _analyses(taxonomy)
    keys = {a.page_id: taxonomy_relationship_keys(a, taxonomy) for a in analyses}
    related = skipped = 0
    for source, target in itertools.permutations(analyses, 2):
        is_s, relates_s = keys[source.page_id]
        is_t, relates_t = keys[target.page_id]
        candidate = bool(relates_s & is_t or relates_t & is_s)
        if taxonomy_relationships(source, target, taxonomy=taxonomy):
            related += 1
            assert candidate, (source.market_id, source.geographies, target.market_id)
        elif not candidate:
            skipped += 1
    assert related > 0
    assert skipped > 0  # the prefilter actually prunes
