"""Independent candidate generators (final-completion directive section 6; ADR-0011 section 2).

Six generators, one per :class:`~ken_contracts.recommendations.RetrievalChannel`, each answering
ONE question for ONE source page and each ablatable on its own:

==============  =========================================================  ====================
generator       nominates                                                  separate signal(s)
==============  =========================================================  ====================
semantic        nearest current-version embeddings via ``VectorStore``      semantic_similarity
lexical         ``ts_rank_cd`` of the source's keywords over page text      lexical_similarity
topic           stored taxonomy relationships (market structure)            topic_relevance
graph           link-graph opportunity: low in-degree in the source's       graph_in_degree,
                industry (``page_graph_metrics``; SQL inbound if absent)    graph_pagerank,
                                                                            graph_authority
analytics       demand without supply from stored GSC / GA4 rows            gsc_opportunity,
                                                                            ga4_engagement
orphan_recovery orphan / nearly-orphan pages near the source (industry      orphan_recovery_value
                or topical cluster)
==============  =========================================================  ====================

**Signals stay separate.** Every candidate carries the raw value each generator measured,
under that generator's own name, plus a per-channel attribution (``CandidateAttribution``). They
are never folded into one opaque number here: the union ranks candidates for the cap only, and
the transparent, versioned ranker (``scoring.py``) produces the score.

**Analytics may surface and may block, but may never alone JUSTIFY** (ADR-0016 s16.7, ADR-0015
s12.3: first-party counts are spoofable). A candidate nominated ONLY by ``ANALYTICS`` needs a
non-analytics relevance signal; :class:`AnalyticsSupportGate` follows the Phase 8 ``BlockOnly``
discipline -- its whole public surface is one method that can only return BLOCKS -- so it
structurally cannot pass a candidate. ``analytics_k`` defaults to 0, so the generator is inert
until an operator enables it.

**Semantic retrieval goes through ``VectorStore.search``** (ADR-0017); there is no SQL distance
operator. Every hit is re-checked against PostgreSQL for staleness (a stale point is dropped,
never used to place a link).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from sqlalchemy import and_, bindparam, cast, distinct, func, literal, or_, select
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ken_contracts.intelligence import EMBEDDING_RECIPE_VERSION, RelationshipType
from ken_contracts.recommendations import RetrievalChannel, RetrievalPolicy
from ken_contracts.vectors import VectorQuery, VectorStore, point_id
from ken_core.db.models.analytics import PageMetricsDaily
from ken_core.db.models.intelligence import PageAnalysis as PageAnalysisRow
from ken_core.db.models.intelligence import PageEmbedding, PageGraphMetrics, PageRelationship
from ken_core.db.models.inventory import Page, PageVersion
from ken_core.db.repositories import graph as graph_repo
from ken_core.db.repositories import intelligence as intelligence_repo
from ken_core.db.repositories.intelligence import current_embedding_join, fresh_analysis_join
from ken_core.db.repositories.recommendations import inbound_count_expression
from ken_core.providers.vectors.memory import MemoryVectorStore
from ken_services.vectors.identity import VectorIdentity, point_from_embedding
from ken_services.vectors.retrieval import OVERFETCH, check_freshness

__all__ = [
    "ANALYTICS_WINDOW_DAYS",
    "MAX_LEXICAL_KEYWORDS",
    "AnalyticsBlockCode",
    "AnalyticsCandidateGenerator",
    "AnalyticsSupportGate",
    "CandidateGenerator",
    "EmbeddingKey",
    "GeneratedCandidate",
    "GenerationContext",
    "GraphCandidateGenerator",
    "LexicalCandidateGenerator",
    "OrphanRecoveryCandidateGenerator",
    "SemanticCandidateGenerator",
    "TopicCandidateGenerator",
    "build_exact_index",
    "default_generators",
    "destination_priorities",
    "graph_channel",
    "keyword_query",
    "lexical_channel",
    "semantic_channel",
    "source_embedding",
    "taxonomy_channel",
]

MAX_LEXICAL_KEYWORDS: Final = 25
"""Bound on the OR-query size (the analysis keeps far fewer)."""
ANALYTICS_WINDOW_DAYS: Final = 28
"""FALLBACK lookback only. The pipeline passes the versioned analytics policy's
``windows.comparison_days`` (``policies/analytics/analytics.v1.yaml``, also 28), so changing
the policy changes the window without touching code."""
_SEMANTIC: Final = RelationshipType.SEMANTIC_SIMILAR.value
_COMMERCIAL_MAX: Final = 3
_ORPHAN_FETCH_CAP: Final = 5000


# --------------------------------------------------------------------------- #
# Shared vocabulary
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class EmbeddingKey:
    """The CURRENT embedding identity: provider-reported model + recipe fingerprint."""

    model: str
    recipe_fingerprint: str
    recipe_version: int = EMBEDDING_RECIPE_VERSION

    def identity(self) -> VectorIdentity:
        return VectorIdentity(
            model=self.model,
            recipe_version=self.recipe_version,
            recipe_fingerprint=self.recipe_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class GenerationContext:
    """What every generator may read about the source. Immutable; nothing is mutated."""

    source_page_id: uuid.UUID
    retrieval: RetrievalPolicy
    embedding: EmbeddingKey | None
    keywords: Sequence[str]
    industry_id: str | None
    analysis_version: str
    vector_store: VectorStore | None = None
    """Shared index (built once per run by the caller). ``None`` falls back to an exact
    in-process index built from PostgreSQL for this call -- correct, but O(pages) per source."""
    analytics_window_days: int | None = None
    """From the versioned analytics policy; ``None`` = the ``ANALYTICS_WINDOW_DAYS`` fallback."""


@dataclass(frozen=True, slots=True)
class GeneratedCandidate:
    """One nomination by ONE generator, with the generator's own raw value and evidence."""

    target_page_id: uuid.UUID
    raw: float
    signals: Mapping[str, float]
    evidence: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class CandidateGenerator(Protocol):
    channel: RetrievalChannel

    def k(self, retrieval: RetrievalPolicy) -> int:
        """How many candidates this generator may nominate (0 = inert)."""
        ...

    async def generate(
        self, session: AsyncSession, ctx: GenerationContext
    ) -> list[GeneratedCandidate]:
        """Best first, deterministic: ties break on the target id."""
        ...


def _ranked(items: Sequence[GeneratedCandidate], k: int) -> list[GeneratedCandidate]:
    return sorted(items, key=lambda c: (-c.raw, str(c.target_page_id)))[:k]


# --------------------------------------------------------------------------- #
# semantic (VectorStore)
# --------------------------------------------------------------------------- #
async def source_embedding(
    session: AsyncSession, *, source_page_id: uuid.UUID, key: EmbeddingKey
) -> tuple[list[float], int] | None:
    """(vector, dimensions) of the source's current-version, current-identity embedding."""
    row = (
        await session.execute(
            select(PageEmbedding.embedding, PageEmbedding.dimensions)
            .join(
                Page, current_embedding_join(key.model, key.recipe_version, key.recipe_fingerprint)
            )
            .where(Page.id == source_page_id)
            .order_by(PageEmbedding.created_at.desc(), PageEmbedding.id)
            .limit(1)
        )
    ).first()
    return None if row is None else (list(row[0]), int(row[1]))


async def build_exact_index(session: AsyncSession, identity: VectorIdentity) -> MemoryVectorStore:
    """An exact in-process index of the CURRENT embeddings, built from PostgreSQL.

    The fallback when the caller supplies no shared store, and what a shadow pass builds ONCE
    for all its sources (it holds a session, not a sessionmaker, so it cannot run the
    indexer). It is derived data exactly like the real index (ADR-0017 s1).
    """
    store = MemoryVectorStore(collection="ken_fallback")
    rows = await intelligence_repo.current_embeddings(
        session,
        model=identity.model,
        recipe_version=identity.recipe_version,
        recipe_fingerprint=identity.recipe_fingerprint,
    )
    await store.rebuild_from(points=[point_from_embedding(r) for r in rows])
    return store


async def semantic_candidates(
    session: AsyncSession,
    *,
    source_page_id: uuid.UUID,
    key: EmbeddingKey,
    k: int,
    store: VectorStore | None,
) -> list[GeneratedCandidate]:
    if k <= 0:
        return []
    source = await source_embedding(session, source_page_id=source_page_id, key=key)
    if source is None:
        return []
    identity = key.identity()
    local = store if store is not None else await build_exact_index(session, identity)
    try:
        hits = await local.search(
            query=VectorQuery(
                vector=tuple(source[0]),
                limit=min(500, k * OVERFETCH),
                filters=identity.filters(exclude_page_ids=(source_page_id,)),
            )
        )
    finally:
        if store is None:
            await local.aclose()
    # The store cannot know what is current: PostgreSQL decides, and a stale hit is dropped.
    fresh = [h for h in await check_freshness(session, hits=hits, identity=identity) if not h.stale]
    out: list[GeneratedCandidate] = []
    for hit in fresh:
        score = max(-1.0, min(1.0, hit.score))
        out.append(
            GeneratedCandidate(
                target_page_id=hit.page_id,
                raw=score,
                signals={"semantic_similarity": score},
                evidence={
                    "point_id": str(
                        point_id(
                            page_version_id=hit.page_version_id,
                            chunk_id=hit.chunk_id,
                            embedding_model=identity.model,
                            embedding_version=identity.embedding_version,
                        )
                    ),
                    "content_hash": hit.content_hash,
                    "page_version_id": hit.page_version_id,
                },
            )
        )
    return _ranked(out, k)


async def semantic_channel(
    session: AsyncSession,
    *,
    source_page_id: uuid.UUID,
    key: EmbeddingKey,
    k: int,
    vector_store: VectorStore | None = None,
) -> dict[uuid.UUID, float]:
    """``{target: cosine}`` -- the dict view of :func:`semantic_candidates` (compat API)."""
    found = await semantic_candidates(
        session, source_page_id=source_page_id, key=key, k=k, store=vector_store
    )
    return {c.target_page_id: c.raw for c in found}


class SemanticCandidateGenerator:
    channel = RetrievalChannel.SEMANTIC

    def k(self, retrieval: RetrievalPolicy) -> int:
        return retrieval.semantic_k

    async def generate(
        self, session: AsyncSession, ctx: GenerationContext
    ) -> list[GeneratedCandidate]:
        if ctx.embedding is None:
            return []
        return await semantic_candidates(
            session,
            source_page_id=ctx.source_page_id,
            key=ctx.embedding,
            k=self.k(ctx.retrieval),
            store=ctx.vector_store,
        )


# --------------------------------------------------------------------------- #
# lexical
# --------------------------------------------------------------------------- #
def keyword_query(keywords: Sequence[str]) -> Any | None:
    """``plainto_tsquery('english', :kw0) || plainto_tsquery('english', :kw1) ...`` (bound)."""
    cleaned = list(dict.fromkeys(k.strip() for k in keywords if k and k.strip()))
    cleaned = cleaned[:MAX_LEXICAL_KEYWORDS]
    if not cleaned:
        return None
    english = cast(literal("english"), REGCONFIG)
    query: Any = None
    for index, keyword in enumerate(cleaned):
        part = func.plainto_tsquery(english, bindparam(f"lexical_kw_{index}", keyword))
        query = part if query is None else query.op("||")(part)
    return query


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


async def lexical_channel(
    session: AsyncSession, *, source_page_id: uuid.UUID, keywords: Sequence[str], k: int
) -> dict[uuid.UUID, float]:
    if k <= 0:
        return {}
    query = keyword_query(keywords)
    if query is None:
        return {}
    rank = func.ts_rank_cd(PageVersion.search_vector, query)
    rows = (
        await session.execute(
            select(Page.id, rank.label("rank"))
            .join(
                PageVersion,
                and_(PageVersion.page_id == Page.id, PageVersion.version == Page.current_version),
            )
            .where(
                Page.id != source_page_id,
                Page.gone_at.is_(None),
                PageVersion.search_vector.op("@@")(query),
            )
            # Ties on the rank are common (ts_rank_cd is coarse). Page.id is a random UUID per
            # install, so breaking them on it alone makes the k-th place differ between two
            # installs of the same corpus; the canonical URL first makes it reproducible.
            .order_by(rank.desc(), Page.canonical_url, Page.id)
            .limit(k)
        )
    ).all()
    out: dict[uuid.UUID, float] = {}
    for row in rows:
        value = _finite(row[1])
        if value is not None and value > 0.0:
            out[uuid.UUID(str(row[0]))] = value
    return out


class LexicalCandidateGenerator:
    channel = RetrievalChannel.LEXICAL

    def k(self, retrieval: RetrievalPolicy) -> int:
        return retrieval.lexical_k

    async def generate(
        self, session: AsyncSession, ctx: GenerationContext
    ) -> list[GeneratedCandidate]:
        found = await lexical_channel(
            session,
            source_page_id=ctx.source_page_id,
            keywords=ctx.keywords,
            k=self.k(ctx.retrieval),
        )
        return _ranked(
            [
                GeneratedCandidate(
                    target_page_id=target,
                    raw=rank,
                    signals={"lexical_similarity": rank},
                    evidence={"keywords": min(len(ctx.keywords), MAX_LEXICAL_KEYWORDS)},
                )
                for target, rank in found.items()
            ],
            self.k(ctx.retrieval) or len(found),
        )


# --------------------------------------------------------------------------- #
# topic (taxonomy)
# --------------------------------------------------------------------------- #
async def _topic_rows(
    session: AsyncSession, *, source_page_id: uuid.UUID
) -> list[tuple[uuid.UUID, float, list[str]]]:
    source = aliased(Page)
    target = aliased(Page)
    rows = (
        await session.execute(
            select(
                PageRelationship.target_page_id,
                func.max(PageRelationship.weight),
                func.array_agg(distinct(PageRelationship.relationship_type)),
            )
            .join(
                source,
                and_(
                    source.id == PageRelationship.source_page_id,
                    source.current_version == PageRelationship.source_page_version,
                ),
            )
            .join(
                target,
                and_(
                    target.id == PageRelationship.target_page_id,
                    target.current_version == PageRelationship.target_page_version,
                ),
            )
            .where(
                PageRelationship.source_page_id == source_page_id,
                PageRelationship.relationship_type != _SEMANTIC,
                target.gone_at.is_(None),
            )
            .group_by(PageRelationship.target_page_id)
            .order_by(func.max(PageRelationship.weight).desc(), PageRelationship.target_page_id)
        )
    ).all()
    return [(r[0], float(r[1]), sorted(r[2])) for r in rows if r[0] != source_page_id]


async def taxonomy_channel(
    session: AsyncSession, *, source_page_id: uuid.UUID
) -> dict[uuid.UUID, float]:
    return {t: w for t, w, _ in await _topic_rows(session, source_page_id=source_page_id)}


class TopicCandidateGenerator:
    channel = RetrievalChannel.TAXONOMY

    def k(self, retrieval: RetrievalPolicy) -> int:
        return 10**6  # the stored edges are already capped per type at materialisation

    async def generate(
        self, session: AsyncSession, ctx: GenerationContext
    ) -> list[GeneratedCandidate]:
        rows = await _topic_rows(session, source_page_id=ctx.source_page_id)
        return _ranked(
            [
                GeneratedCandidate(
                    target_page_id=target,
                    raw=weight,
                    signals={"topic_relevance": weight},
                    evidence={"relationship_types": types},
                )
                for target, weight, types in rows
            ],
            len(rows),
        )


# --------------------------------------------------------------------------- #
# graph + orphan recovery (page_graph_metrics)
# --------------------------------------------------------------------------- #
async def graph_channel(
    session: AsyncSession,
    *,
    source_page_id: uuid.UUID,
    industry_id: str | None,
    analysis_version: str,
    k: int,
) -> dict[uuid.UUID, float]:
    """SQL fallback: under-linked targets in the source's industry by counted inbound links."""
    if k <= 0 or industry_id is None:
        return {}
    inbound = inbound_count_expression(Page.id).label("inbound")
    rows = (
        await session.execute(
            select(Page.id, inbound)
            .join(PageAnalysisRow, fresh_analysis_join(analysis_version))
            .where(
                PageAnalysisRow.industry_id == industry_id,
                Page.id != source_page_id,
                Page.gone_at.is_(None),
            )
            .order_by(inbound.asc(), PageAnalysisRow.commercial_importance.desc(), Page.id)
            .limit(k)
        )
    ).all()
    return {r[0]: 1.0 / (1.0 + int(r[1] or 0)) for r in rows}


def _metrics_join(version: str) -> Any:
    return and_(
        PageGraphMetrics.page_id == Page.id,
        PageGraphMetrics.page_version == Page.current_version,
        PageGraphMetrics.metrics_version == version,
    )


class GraphCandidateGenerator:
    """Under-linked destinations in the source's industry.

    With ``page_graph_metrics`` it ranks by ``1 / (1 + in_degree)`` and reports PageRank and
    authority beside it as separate signals; without metrics it falls back to counted inbound
    links in SQL (the Phase 3 behaviour), so the channel works before ``make graph-metrics``.
    """

    channel = RetrievalChannel.GRAPH

    def k(self, retrieval: RetrievalPolicy) -> int:
        return retrieval.graph_k

    async def generate(
        self, session: AsyncSession, ctx: GenerationContext
    ) -> list[GeneratedCandidate]:
        k = self.k(ctx.retrieval)
        if k <= 0 or ctx.industry_id is None:
            return []
        version = await graph_repo.latest_metrics_version(session)
        if version is None:
            found = await graph_channel(
                session,
                source_page_id=ctx.source_page_id,
                industry_id=ctx.industry_id,
                analysis_version=ctx.analysis_version,
                k=k,
            )
            return _ranked(
                [
                    GeneratedCandidate(
                        target_page_id=t,
                        raw=v,
                        signals={"graph_opportunity": v},
                        evidence={"source": "sql_inbound"},
                    )
                    for t, v in found.items()
                ],
                k,
            )
        rows = (
            await session.execute(
                select(
                    Page.id,
                    PageGraphMetrics.in_degree,
                    PageGraphMetrics.internal_pagerank,
                    PageGraphMetrics.authority_score,
                )
                .join(PageGraphMetrics, _metrics_join(version))
                .join(PageAnalysisRow, fresh_analysis_join(ctx.analysis_version))
                .where(
                    PageAnalysisRow.industry_id == ctx.industry_id,
                    Page.id != ctx.source_page_id,
                    Page.gone_at.is_(None),
                )
                .order_by(
                    PageGraphMetrics.in_degree.asc(),
                    PageAnalysisRow.commercial_importance.desc(),
                    Page.id,
                )
                .limit(k)
            )
        ).all()
        return _ranked(
            [
                GeneratedCandidate(
                    target_page_id=r[0],
                    raw=1.0 / (1.0 + int(r[1])),
                    signals={
                        "graph_opportunity": 1.0 / (1.0 + int(r[1])),
                        "graph_in_degree": float(r[1]),
                        "graph_pagerank": float(r[2]),
                        "graph_authority": float(r[3]),
                    },
                    evidence={"source": "page_graph_metrics", "metrics_version": version},
                )
                for r in rows
            ],
            k,
        )


class OrphanRecoveryCandidateGenerator:
    """The explicit recovery pass: orphan / nearly-orphan pages near the source.

    "Near" is the source's industry OR its topical cluster (``page_graph_metrics.cluster_id``).
    The crawl entry page (depth 0) is never a target: it has no inbound link by construction.
    ``orphan_recovery_value = 1 / (1 + in_degree)`` scaled by destination priority
    (``0.5 + 0.5 x commercial_importance / 3``), so an orphan that matters more is recovered
    first. Kept separate from GRAPH so its yield and precision can be measured and ablated.
    """

    channel = RetrievalChannel.ORPHAN_RECOVERY

    def k(self, retrieval: RetrievalPolicy) -> int:
        return retrieval.orphan_recovery_k

    async def generate(
        self, session: AsyncSession, ctx: GenerationContext
    ) -> list[GeneratedCandidate]:
        k = self.k(ctx.retrieval)
        if k <= 0:
            return []
        version = await graph_repo.latest_metrics_version(session)
        if version is None:
            return []
        source_cluster = (
            await session.execute(
                select(PageGraphMetrics.cluster_id)
                .join(Page, _metrics_join(version))
                .where(Page.id == ctx.source_page_id)
            )
        ).scalar_one_or_none()
        near = []
        if ctx.industry_id is not None:
            near.append(PageAnalysisRow.industry_id == ctx.industry_id)
        if source_cluster is not None:
            near.append(PageGraphMetrics.cluster_id == source_cluster)
        if not near:
            return []
        rows = (
            await session.execute(
                select(
                    Page.id,
                    PageGraphMetrics.in_degree,
                    PageGraphMetrics.is_orphan,
                    PageAnalysisRow.commercial_importance,
                )
                .join(PageGraphMetrics, _metrics_join(version))
                .join(PageAnalysisRow, fresh_analysis_join(ctx.analysis_version))
                .where(
                    or_(PageGraphMetrics.is_orphan, PageGraphMetrics.is_nearly_orphan),
                    # The crawl entry page has no inbound link by construction (the schema
                    # ties is_orphan to in_degree = 0) but is not a page to RECOVER.
                    or_(PageGraphMetrics.crawl_depth.is_(None), PageGraphMetrics.crawl_depth > 0),
                    or_(*near),
                    Page.id != ctx.source_page_id,
                    Page.gone_at.is_(None),
                )
                .order_by(Page.id)
                .limit(_ORPHAN_FETCH_CAP)
            )
        ).all()
        out: list[GeneratedCandidate] = []
        for page_id, in_degree, is_orphan, importance in rows:
            priority = min(1.0, max(0.0, int(importance) / _COMMERCIAL_MAX))
            value = (1.0 / (1.0 + int(in_degree))) * (0.5 + 0.5 * priority)
            out.append(
                GeneratedCandidate(
                    target_page_id=page_id,
                    raw=value,
                    signals={
                        "orphan_recovery_value": value,
                        "destination_priority": priority,
                        "graph_in_degree": float(in_degree),
                    },
                    evidence={
                        "is_orphan": bool(is_orphan),
                        "metrics_version": version,
                        "in_degree": int(in_degree),
                    },
                )
            )
        return _ranked(out, k)


# --------------------------------------------------------------------------- #
# analytics (may surface, may block, never alone justify)
# --------------------------------------------------------------------------- #
class AnalyticsCandidateGenerator:
    """Destinations with search demand that is not converting: ``gsc_opportunity``.

    ``gsc_opportunity = (impressions / max impressions in the corpus) x (1 - clicks /
    impressions)`` over the newest ``analytics_window_days`` of imported data; ``ga4_engagement``
    is the page's share of the corpus maximum pageviews. Both are RECORDED as separate signals.
    Nothing here reaches a score component: analytics only SURFACES a destination, and
    :class:`AnalyticsSupportGate` then requires non-analytics support (see module docstring).
    Rows flagged ``synthetic`` are used (the demo corpus has no other analytics) and reported
    in the evidence so nothing downstream mistakes them for real traffic.
    """

    channel = RetrievalChannel.ANALYTICS

    def k(self, retrieval: RetrievalPolicy) -> int:
        return retrieval.analytics_k

    async def generate(
        self, session: AsyncSession, ctx: GenerationContext
    ) -> list[GeneratedCandidate]:
        k = self.k(ctx.retrieval)
        if k <= 0:
            return []
        end = (
            await session.execute(select(func.max(PageMetricsDaily.metric_date)))
        ).scalar_one_or_none()
        if end is None:
            return []
        window = ctx.analytics_window_days or ANALYTICS_WINDOW_DAYS
        start = end - timedelta(days=max(1, window) - 1)
        rows = (
            await session.execute(
                select(
                    PageMetricsDaily.page_id,
                    PageMetricsDaily.source,
                    func.sum(PageMetricsDaily.impressions),
                    func.sum(PageMetricsDaily.clicks),
                    func.sum(PageMetricsDaily.pageviews),
                    func.bool_or(PageMetricsDaily.synthetic),
                )
                .join(Page, Page.id == PageMetricsDaily.page_id)
                .where(
                    PageMetricsDaily.metric_date >= start,
                    PageMetricsDaily.metric_date <= end,
                    Page.gone_at.is_(None),
                )
                .group_by(PageMetricsDaily.page_id, PageMetricsDaily.source)
                .order_by(PageMetricsDaily.page_id, PageMetricsDaily.source)
            )
        ).all()
        gsc: dict[uuid.UUID, tuple[int, int, bool]] = {}
        ga4: dict[uuid.UUID, int] = {}
        for row in rows:
            page_id: uuid.UUID = row[0]
            if row[1] == "search_console":
                gsc[page_id] = (int(row[2] or 0), int(row[3] or 0), bool(row[5]))
            elif row[1] == "ga4":
                ga4[page_id] = int(row[4] or 0)
        max_impressions = max((i for i, _, _ in gsc.values()), default=0)
        max_views = max(ga4.values(), default=0)
        out: list[GeneratedCandidate] = []
        for page_id, (impressions, clicks, synthetic) in gsc.items():
            if page_id == ctx.source_page_id or impressions <= 0 or max_impressions <= 0:
                continue
            opportunity = (impressions / max_impressions) * (1.0 - min(1.0, clicks / impressions))
            if opportunity <= 0.0:
                continue
            engagement = (ga4.get(page_id, 0) / max_views) if max_views > 0 else 0.0
            out.append(
                GeneratedCandidate(
                    target_page_id=page_id,
                    raw=opportunity,
                    signals={"gsc_opportunity": opportunity, "ga4_engagement": engagement},
                    evidence={
                        "impressions": impressions,
                        "clicks": clicks,
                        "window_days": window,
                        "window_end": end.isoformat(),
                        "synthetic": synthetic,
                    },
                )
            )
        return _ranked(out, k)


class AnalyticsBlockCode(StrEnum):
    """The ONLY reason :class:`AnalyticsSupportGate` may return."""

    ANALYTICS_ONLY_WITHOUT_SUPPORT = "analytics_only_without_support"


@dataclass(frozen=True, slots=True)
class AnalyticsSupportGate:
    """The Phase 8 ``BlockOnly`` discipline applied to the analytics channel.

    First-party counts are spoofable (ADR-0015 s12.3), so analytics data can make a candidate
    FAIL a gate and can never make one pass. The public surface is exactly
    :meth:`blocking_reasons`: there is no method that returns an approval, so this type
    structurally cannot justify a candidate (a test asserts the surface stays one method).

    ``support`` maps a target to its best NON-analytics relevance (cosine at or above the
    semantic floor, or a stored taxonomy weight); a candidate nominated only by analytics with
    ``support <= 0`` is blocked.
    """

    analytics_only: frozenset[uuid.UUID]
    support: Mapping[uuid.UUID, float]

    def blocking_reasons(self) -> tuple[tuple[uuid.UUID, AnalyticsBlockCode], ...]:
        """Targets that analytics alone surfaced and nothing else supports, in id order."""
        return tuple(
            (target, AnalyticsBlockCode.ANALYTICS_ONLY_WITHOUT_SUPPORT)
            for target in sorted(self.analytics_only, key=str)
            if self.support.get(target, 0.0) <= 0.0
        )


# --------------------------------------------------------------------------- #
def default_generators() -> tuple[CandidateGenerator, ...]:
    """All six, in :class:`RetrievalChannel` declaration order (the union's order)."""
    return (
        SemanticCandidateGenerator(),
        LexicalCandidateGenerator(),
        TopicCandidateGenerator(),
        GraphCandidateGenerator(),
        AnalyticsCandidateGenerator(),
        OrphanRecoveryCandidateGenerator(),
    )


async def destination_priorities(
    session: AsyncSession, *, page_ids: Sequence[uuid.UUID], analysis_version: str
) -> dict[uuid.UUID, float]:
    """``commercial_importance / 3`` of each target's fresh analysis (destination priority)."""
    if not page_ids:
        return {}
    rows = (
        await session.execute(
            select(Page.id, PageAnalysisRow.commercial_importance)
            .join(PageAnalysisRow, fresh_analysis_join(analysis_version))
            .where(Page.id.in_(list(page_ids)))
        )
    ).all()
    return {r[0]: min(1.0, max(0.0, int(r[1]) / _COMMERCIAL_MAX)) for r in rows}
