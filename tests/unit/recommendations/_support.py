"""Shared, deterministic builders for the recommendation-core unit tests (no DB, no network)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import yaml

from ken_contracts.intelligence import (
    GeographyMention,
    Intent,
    PageAnalysis,
    SalientBlock,
    TaxonomyPolicy,
    TopicScore,
    analysis_version,
)
from ken_contracts.recommendations import RankingPolicy, RecommendationPolicy
from ken_services.intelligence.taxonomy import load_taxonomy, taxonomy_index
from ken_services.inventory.normalize import ContentBlock, extract_blocks
from ken_services.recommendations.placement import AnchorPhrase
from ken_services.recommendations.policy import load_ranking_policy, load_recommendation_policy

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
RECOMMENDATION_V1: Final = REPO_ROOT / "policies" / "recommendation" / "recommendation.v1.yaml"
RANKING_V1: Final = REPO_ROOT / "policies" / "ranking" / "ranking_weights.v1.yaml"
TAXONOMY_V1: Final = REPO_ROOT / "policies" / "taxonomy" / "taxonomy.v1.yaml"
TAXONOMY_V3: Final = REPO_ROOT / "policies" / "taxonomy" / "taxonomy.v3.yaml"

SOLAR: Final = AnchorPhrase(text="solar power", source="target_topic", rank=0)


def taxonomy() -> TaxonomyPolicy:
    return load_taxonomy(TAXONOMY_V1)


def taxonomy_v3() -> TaxonomyPolicy:
    """The LIVE taxonomy (every sitemap slug): what the owner's rejected plans ran on."""
    return load_taxonomy(TAXONOMY_V3)


def taxonomy_with(**market_aliases: Sequence[str]) -> TaxonomyPolicy:
    """The v1 taxonomy with extra aliases on some markets (``solar_power=["x"]``)."""
    raw: dict[str, Any] = yaml.safe_load(TAXONOMY_V1.read_text("utf-8"))
    for market in raw["markets"]:
        extra = market_aliases.get(market["id"].replace("-", "_"))
        if extra:
            market["aliases"] = [*market.get("aliases", []), *extra]
    return TaxonomyPolicy.model_validate(raw)


def policy() -> RecommendationPolicy:
    return load_recommendation_policy(RECOMMENDATION_V1)


def ranking() -> RankingPolicy:
    return load_ranking_policy(RANKING_V1)


def policy_with(**sections: Any) -> RecommendationPolicy:
    """The v1 recommendation policy with some sections partially overridden."""
    raw: dict[str, Any] = yaml.safe_load(RECOMMENDATION_V1.read_text("utf-8"))
    for name, override in sections.items():
        if isinstance(raw.get(name), dict):
            raw[name] = {**raw[name], **override}
        else:
            raw[name] = override
    return RecommendationPolicy.model_validate(raw)


def ranking_with(weights: dict[str, float]) -> RankingPolicy:
    base = dict.fromkeys(ranking().weights, 0.0)
    return RankingPolicy(
        policy="ranking", version=9, status="test", source="test", weights={**base, **weights}
    )


def analysis(
    *,
    page_id: uuid.UUID | None = None,
    page_type: str = "report",
    intent: Intent = Intent.COMMERCIAL_RESEARCH,
    commercial_importance: int = 3,
    market_id: str | None = None,
    industry_id: str | None = None,
    topics: Sequence[tuple[str, float]] = (),
    geographies: Sequence[str] = (),
    keywords: Sequence[str] = (),
    salient: Sequence[tuple[str, float]] = (),
    geography_scores: Sequence[float] = (),
    tax: TaxonomyPolicy | None = None,
) -> PageAnalysis:
    """A v1-taxonomy analysis; ``industry_id`` defaults to the root of ``market_id``.

    ``geographies`` are listed in the given order; ``geography_scores`` (optional, same
    length) sets each mention's score, so a test can make one geography the PRIMARY one and
    another an incidental mention. Without it every mention scores 1.
    """
    tax = tax if tax is not None else taxonomy()
    index = taxonomy_index(tax)
    if industry_id is None and market_id is not None and index.has_market(market_id):
        industry_id = index.market_root(market_id)
    return PageAnalysis(
        page_id=page_id or uuid.uuid4(),
        page_version=1,
        analysis_version=analysis_version(tax.version),
        page_type=page_type,
        intent=intent,
        commercial_importance=commercial_importance,
        industry_id=industry_id,
        market_id=market_id,
        topics=tuple(
            TopicScore(
                node_id=node,
                label=index.market(node).label,
                score=score,
                depth=index.market_depth(node),
            )
            for node, score in topics
        ),
        geographies=tuple(
            GeographyMention(
                geo_id=geo,
                label=index.geography(geo).label,
                kind=index.geography(geo).kind,
                score=geography_scores[n] if n < len(geography_scores) else 1,
            )
            for n, geo in enumerate(geographies)
        ),
        keywords=tuple(keywords),
        salient_blocks=tuple(
            SalientBlock(block_id=block_id, score=score, text="x") for block_id, score in salient
        ),
    )


def blocks(body: str) -> tuple[ContentBlock, ...]:
    """``extract_blocks`` of ``<body><main>{body}</main></body>``."""
    return extract_blocks(f"<body><main>{body}</main></body>")
