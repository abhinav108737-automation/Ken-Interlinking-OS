"""Shared builders for the deterministic intelligence unit tests (no DB, no network)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from ken_contracts.intelligence import (
    GeographyMention,
    PageAnalysis,
    TaxonomyPolicy,
    analysis_version,
)
from ken_services.intelligence.analyze import AnalysisInput
from ken_services.intelligence.taxonomy import load_taxonomy, taxonomy_index

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
V1_PATH: Final = REPO_ROOT / "policies" / "taxonomy" / "taxonomy.v1.yaml"


def v1() -> TaxonomyPolicy:
    return load_taxonomy(V1_PATH)


def mini_policy_data() -> dict[str, Any]:
    """A tiny synthetic taxonomy whose scores are easy to reason about."""
    return {
        "policy": "taxonomy",
        "version": 7,
        "status": "test",
        "source": "fixture",
        "markets": [
            {"id": "tech-ind", "label": "Tech Industry", "aliases": ["tech industry"]},
            {"id": "widgets", "label": "Widgets", "parent": "tech-ind", "aliases": ["widget"]},
            {"id": "gadgets", "label": "Gadgets", "parent": "tech-ind", "aliases": ["gadget"]},
            {
                "id": "smart-widgets",
                "label": "Smart Widgets",
                "parent": "widgets",
                "aliases": ["smart widget"],
                "complementary": ["gadgets"],
            },
            {
                "id": "food-ind",
                "label": "Food Industry",
                "aliases": ["food industry"],
                "adjacent": ["tech-ind"],
            },
            {"id": "snacks", "label": "Snacks", "parent": "food-ind"},
        ],
        "geographies": [
            {"id": "global", "label": "Global", "kind": "global", "aliases": ["worldwide"]},
            {"id": "xregion", "label": "Xregion", "kind": "region", "parent": "global"},
            {"id": "aland", "label": "Aland", "kind": "country", "parent": "xregion"},
            {"id": "bland", "label": "Bland", "kind": "country", "parent": "xregion"},
        ],
        "entities": [
            {"id": "acme", "label": "Acme Corp", "type": "company", "aliases": ["acme"]},
        ],
        "page_types": [
            {
                "id": "report",
                "label": "Report",
                "path_patterns": ["^/reports/"],
                "intent": "commercial_research",
                "commercial_importance": 3,
            },
            {
                "id": "misc",
                "label": "Misc",
                "path_patterns": ["^/never-matches$"],
                "intent": "informational",
                "commercial_importance": 1,
            },
        ],
        "default_page_type": "misc",
    }


def mini() -> TaxonomyPolicy:
    return _MINI


_MINI: Final = TaxonomyPolicy.model_validate(mini_policy_data())


def item(
    *,
    url: str = "https://site.test/reports/x",
    title: str | None = None,
    meta_description: str | None = None,
    headings: Sequence[tuple[int, str]] = (),
    body_text: str = "",
    body_html: str = "",
    metadata: Mapping[str, str] | None = None,
    page_id: uuid.UUID | None = None,
) -> AnalysisInput:
    return AnalysisInput(
        page_id=page_id or uuid.UUID(int=1),
        page_version=1,
        url=url,
        title=title,
        meta_description=meta_description,
        headings=tuple(headings),
        body_html=body_html,
        body_text=body_text,
        metadata=dict(metadata or {}),
    )


def analysis(
    policy: TaxonomyPolicy,
    *,
    market_id: str | None,
    geographies: Sequence[str] = (),
    page: int = 1,
    industry_id: str | None = None,
) -> PageAnalysis:
    """A hand-built PageAnalysis; industry defaults to the root of ``market_id``."""
    index = taxonomy_index(policy)
    if industry_id is None and market_id is not None and index.has_market(market_id):
        industry_id = index.market_root(market_id)
    geos = []
    for geo_id in geographies:
        node = index.geography(geo_id)
        geos.append(GeographyMention(geo_id=geo_id, label=node.label, kind=node.kind, score=1.0))
    return PageAnalysis(
        page_id=uuid.UUID(int=page),
        page_version=1,
        analysis_version=analysis_version(policy.version),
        page_type="report",
        intent="commercial_research",
        commercial_importance=3,
        industry_id=industry_id,
        market_id=market_id,
        geographies=tuple(geos),
    )
