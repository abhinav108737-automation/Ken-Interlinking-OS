"""SQL implementation of :class:`ken_contracts.intelligence.IntelligenceReader` (read-only).

The API resolves this module by name (``ken_api.deps.INTELLIGENCE_READER``) and
builds ``SqlIntelligenceReader(sessionmaker=, model_provider=, settings=)`` per
request. Nothing here writes; ``semantic_search`` embeds the query and stores
nothing.

Read-time rules (ADR-0010):

* **Fresh analysis** = a row for the page's CURRENT version and the current
  ``analysis_version`` (from the configured taxonomy policy).
* **Fresh embedding** = a row for the current version and the CURRENT embedding
  identity (``embedding_identity``: the provider-reported model), recipe version
  and recipe fingerprint (``current_recipe_fingerprint``: from the settings).
  When the provider cannot be asked, nothing is fresh and similarity lists are
  empty; ``semantic_search`` raises :class:`EmbeddingUnavailableError` (503).
* **Similarity** is exact cosine KNN (``<=>``; score = 1 - distance) over
  current-version embeddings of the same (model, dimensions, recipe version,
  recipe fingerprint), gone pages excluded. Vectors of two models or two text
  recipes are never compared.
* **Relationships** are served only when NOT stale: both endpoint versions
  recorded on the edge equal the endpoints' current versions. Only STORED types
  are served (``STORED_RELATIONSHIP_TYPES``): the coarse derived-only types
  (same_geography, regional_parent, industry_adjacency) are never materialised,
  so filtering by one returns nothing, and any legacy row of one is ignored.
* **Eligibility** is computed now, from live state, by the shared
  ``compute_eligibility`` (never stored).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, Literal

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    bindparam,
    case,
    cast,
    exists,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from ken_contracts.errors import ListResponse, PageInfo
from ken_contracts.intelligence import (
    EMBEDDING_RECIPE_VERSION,
    EmbeddingUnavailableError,
    IntelligenceCoverage,
    IntelligenceNotFoundError,
    LinkEligibility,
    PageAnalysis,
    PageEmbeddingInfo,
    PageIntelligenceView,
    PageProtectionView,
    PageRelationshipView,
    PageTypeView,
    ProtectionSource,
    RelationshipType,
    SimilarPage,
    TaxonomyNodeView,
    TaxonomyPolicy,
    TaxonomyView,
    analysis_version,
)
from ken_contracts.inventory import CanonicalStatus, PublicationState
from ken_contracts.model import ModelProvider, ModelRole
from ken_core.config import Settings
from ken_core.db.models.intelligence import PageAnalysis as PageAnalysisRow
from ken_core.db.models.intelligence import PageEmbedding, PageProtection, PageRelationship
from ken_core.db.models.inventory import Page, PageVersion
from ken_core.db.repositories.intelligence import (
    analysis_from_row,
    cosine_distance,
    embedding_identity_filter,
    fresh_analysis_join,
)
from ken_core.db.types import Vector
from ken_core.logging import get_logger
from ken_services.intelligence.eligibility import EligibilityState, compute_eligibility
from ken_services.intelligence.embeddings import (
    EmbeddingIdentity,
    current_recipe_fingerprint,
    embedding_identity,
)
from ken_services.intelligence.relationships import STORED_RELATIONSHIP_TYPES
from ken_services.intelligence.taxonomy import load_taxonomy

__all__ = ["EligibilityFn", "SqlIntelligenceReader"]

logger = get_logger("ken_services.intelligence.reader")

MAX_LIMIT: Final = 200
NO_EMBEDDING_MODEL: Final = "unavailable"
"""``IntelligenceCoverage.embedding_model`` when the provider cannot report one."""

_NODE_COUNT_SQL: Final = {
    column: text(
        f"SELECT e.value ->> '{key}' AS node, count(DISTINCT a.page_id) "  # noqa: S608 - fixed
        "FROM page_analyses a JOIN pages p ON p.id = a.page_id "
        "AND p.current_version = a.page_version AND a.analysis_version = :av "
        f"CROSS JOIN LATERAL jsonb_array_elements(a.{column}) AS e(value) GROUP BY 1"
    )
    for column, key in (("topics", "node_id"), ("geographies", "geo_id"))
}
"""Pages per node id over FRESH analyses (column names are constants, never input)."""

_STORED_TYPES: Final = tuple(kind.value for kind in STORED_RELATIONSHIP_TYPES)


def _stored_type() -> ColumnElement[bool]:
    """Edges of a type the analysis job stores (legacy coarse rows are never served)."""
    return PageRelationship.relationship_type.in_(_STORED_TYPES)


def _semantic_matches(identity: EmbeddingIdentity | None, fingerprint: str) -> ColumnElement[bool]:
    """Serve a ``semantic_similar`` edge only if it came from the CURRENT embedding identity.

    Fails closed: with no resolvable identity, no semantic edge is served.
    """
    is_semantic = PageRelationship.relationship_type == RelationshipType.SEMANTIC_SIMILAR.value
    if identity is None:
        return ~is_semantic
    return or_(
        ~is_semantic,
        and_(
            PageRelationship.evidence["model"].astext == identity.model,
            PageRelationship.evidence["recipe_fingerprint"].astext == fingerprint,
        ),
    )


EligibilityFn = Callable[[Mapping[str, Any], TaxonomyPolicy], LinkEligibility]
"""``(EligibilityState fields, taxonomy) -> LinkEligibility``."""


def _default_eligibility() -> EligibilityFn:
    def evaluate(fields: Mapping[str, Any], taxonomy: TaxonomyPolicy) -> LinkEligibility:
        return compute_eligibility(EligibilityState(**fields), taxonomy=taxonomy)

    return evaluate


def _load_taxonomy(path: str) -> TaxonomyPolicy:
    return load_taxonomy(path)


def _check_paging(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        raise ValueError("offset must be >= 0")


def _score(distance: float | None) -> float | None:
    if distance is None or not math.isfinite(distance):
        return None
    return min(1.0, max(-1.0, 1.0 - distance))


def _depths(parents: Mapping[str, str | None]) -> dict[str, int]:
    depths: dict[str, int] = {}
    for node in parents:
        depth, current, seen = 0, parents[node], {node}
        while current is not None and current not in seen:
            seen.add(current)
            depth += 1
            current = parents.get(current)
        depths[node] = depth
    return depths


def _current_analysis(av: str) -> Any:
    """Correlated: the analysis row id of the page's current version, fresh one preferred."""
    return (
        select(PageAnalysisRow.id)
        .where(
            PageAnalysisRow.page_id == Page.id,
            PageAnalysisRow.page_version == Page.current_version,
        )
        .order_by(
            (PageAnalysisRow.analysis_version == av).desc(),
            PageAnalysisRow.created_at.desc(),
            PageAnalysisRow.id,
        )
        .limit(1)
        .correlate(Page)
        .scalar_subquery()
    )


def _active_protection() -> ColumnElement[bool]:
    return exists().where(PageProtection.page_id == Page.id, PageProtection.active.is_(True))


def _any_analysis() -> ColumnElement[bool]:
    return exists().where(PageAnalysisRow.page_id == Page.id)


class SqlIntelligenceReader:
    """Read-only intelligence views. ``taxonomy`` / ``eligibility`` are injectable for tests."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        model_provider: ModelProvider,
        settings: Settings,
        taxonomy: TaxonomyPolicy | None = None,
        eligibility: EligibilityFn | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._provider = model_provider
        self._settings = settings
        self._taxonomy_override = taxonomy
        self._eligibility_override = eligibility

    # ------------------------------------------------------------ plumbing
    @property
    def _taxonomy(self) -> TaxonomyPolicy:
        if self._taxonomy_override is not None:
            return self._taxonomy_override
        return _load_taxonomy(self._settings.taxonomy_path)  # cached by path + mtime

    def _eligibility(self, fields: Mapping[str, Any], taxonomy: TaxonomyPolicy) -> LinkEligibility:
        evaluate = self._eligibility_override or _default_eligibility()
        return evaluate(fields, taxonomy)

    @property
    def _fingerprint(self) -> str:
        return current_recipe_fingerprint(self._settings)

    def _fresh_embedding(self, model: str) -> ColumnElement[bool]:
        return embedding_identity_filter(
            model=model,
            recipe_version=EMBEDDING_RECIPE_VERSION,
            recipe_fingerprint=self._fingerprint,
        )

    async def _identity(self) -> EmbeddingIdentity | None:
        try:
            return await embedding_identity(self._provider)
        except Exception as exc:  # unreachable or no model: nothing is fresh
            logger.info("embedding_identity_unavailable", error_class=type(exc).__name__)
            return None

    @staticmethod
    def _state(
        *,
        publication_state: str,
        gone: bool,
        protected: bool,
        robots: Sequence[str] | None,
        canonical_status: str | None,
        http_status: int | None,
        word_count: int,
        page_type: str | None,
        has_analysis: bool,
        analysis_fresh: bool,
    ) -> dict[str, Any]:
        return {
            "publication_state": PublicationState(publication_state),
            "gone": gone,
            "protected": protected,
            "robots_directives": tuple(str(d) for d in robots or ()),
            "canonical_status": CanonicalStatus(canonical_status or CanonicalStatus.SELF.value),
            "http_status": http_status,
            "word_count": word_count,
            "page_type": page_type,
            "has_analysis": has_analysis,
            "analysis_fresh": analysis_fresh,
        }

    async def _page(self, session: AsyncSession, page_id: uuid.UUID) -> Page:
        page = await session.get(Page, page_id)
        if page is None:
            raise IntelligenceNotFoundError("page")
        return page

    def _similar_select(
        self,
        *,
        distance: ColumnElement[float],
        model: str,
        dimensions: int,
        av: str,
        exclude: uuid.UUID | None,
        limit: int,
    ) -> Select[*tuple[Any, ...]]:
        fresh = aliased(PageAnalysisRow)
        statement = (
            select(
                Page.id,
                Page.canonical_url,
                Page.title,
                distance.label("distance"),
                fresh.page_type,
                fresh.market_id,
            )
            .select_from(PageEmbedding)
            .join(
                Page,
                and_(
                    Page.id == PageEmbedding.page_id,
                    Page.current_version == PageEmbedding.page_version,
                ),
            )
            .outerjoin(
                fresh,
                and_(
                    fresh.page_id == Page.id,
                    fresh.page_version == Page.current_version,
                    fresh.analysis_version == av,
                ),
            )
            .where(
                self._fresh_embedding(model),
                PageEmbedding.dimensions == dimensions,
                Page.gone_at.is_(None),
            )
            .order_by(distance, Page.id)
            .limit(limit)
        )
        if exclude is not None:
            statement = statement.where(Page.id != exclude)
        return statement

    async def _similar(
        self, session: AsyncSession, statement: Select[*tuple[Any, ...]], limit: int
    ) -> ListResponse[SimilarPage]:
        items: list[SimilarPage] = []
        for row in (await session.execute(statement)).all():
            score = _score(row.distance)
            if score is None:  # a zero vector has no direction
                continue
            items.append(
                SimilarPage(
                    page_id=row.id,
                    canonical_url=row.canonical_url,
                    title=row.title,
                    score=score,
                    page_type=row.page_type,
                    market_id=row.market_id,
                )
            )
        return ListResponse[SimilarPage](
            items=items, page=PageInfo(total=len(items), limit=limit, offset=0)
        )

    # ----------------------------------------------------- page intelligence
    async def get_page_intelligence(self, *, page_id: uuid.UUID) -> PageIntelligenceView:
        taxonomy = self._taxonomy
        av = analysis_version(taxonomy.version)
        identity = await self._identity()
        async with self._sessionmaker() as session:
            page = await self._page(session, page_id)
            version = (
                await session.execute(
                    select(
                        PageVersion.robots_directives,
                        PageVersion.canonical_status,
                        PageVersion.http_status,
                    ).where(
                        PageVersion.page_id == page.id,
                        PageVersion.version == page.current_version,
                    )
                )
            ).one_or_none()
            analysis_row = (
                await session.scalars(
                    select(PageAnalysisRow)
                    .where(
                        PageAnalysisRow.page_id == page.id,
                        PageAnalysisRow.page_version == page.current_version,
                    )
                    .order_by(
                        (PageAnalysisRow.analysis_version == av).desc(),
                        PageAnalysisRow.created_at.desc(),
                        PageAnalysisRow.id,
                    )
                    .limit(1)
                )
            ).first()
            has_analysis = bool(
                await session.scalar(select(exists().where(PageAnalysisRow.page_id == page.id)))
            )
            embedding_row = await self._embedding_row(session, page, identity)
            protections = (
                await session.scalars(
                    select(PageProtection)
                    .where(PageProtection.page_id == page.id)
                    .order_by(PageProtection.id.desc())  # insertion order: total and stable
                )
            ).all()
            counts = await self._relationship_counts(session, page)

        analysis: PageAnalysis | None = (
            analysis_from_row(analysis_row) if analysis_row is not None else None
        )
        analysis_fresh = analysis is not None and analysis.analysis_version == av
        embedding_fresh = (
            embedding_row is not None
            and identity is not None
            and embedding_row.model == identity.model
            and embedding_row.recipe_version == EMBEDDING_RECIPE_VERSION
            and embedding_row.recipe_fingerprint == self._fingerprint
        )
        protection_views = [
            PageProtectionView(
                id=row.id,
                page_id=row.page_id,
                source=ProtectionSource(row.source),
                rule_id=row.rule_id,
                reason=row.reason,
                active=row.active,
                created_by=row.created_by,
                created_at=row.created_at,
            )
            for row in protections
        ]
        eligibility = self._eligibility(
            self._state(
                publication_state=page.publication_state,
                gone=page.gone_at is not None,
                protected=any(p.active for p in protection_views),
                robots=None if version is None else version.robots_directives,
                canonical_status=None if version is None else version.canonical_status,
                http_status=None if version is None else version.http_status,
                word_count=page.word_count,
                page_type=None if analysis is None else analysis.page_type,
                has_analysis=has_analysis,
                analysis_fresh=analysis_fresh,
            ),
            taxonomy,
        )
        return PageIntelligenceView(
            page_id=page.id,
            canonical_url=page.canonical_url,
            title=page.title,
            current_version=page.current_version,
            analysis=analysis,
            analysis_fresh=analysis_fresh,
            embedding=None
            if embedding_row is None
            else PageEmbeddingInfo(
                page_id=embedding_row.page_id,
                page_version=embedding_row.page_version,
                model=embedding_row.model,
                provider=embedding_row.provider,
                dimensions=embedding_row.dimensions,
                recipe_version=embedding_row.recipe_version,
                recipe_fingerprint=embedding_row.recipe_fingerprint,
                input_hash=embedding_row.input_hash,
                created_at=embedding_row.created_at,
            ),
            embedding_fresh=embedding_fresh,
            protections=protection_views,
            eligibility=eligibility,
            relationship_counts=counts,
        )

    async def _embedding_row(
        self, session: AsyncSession, page: Page, identity: EmbeddingIdentity | None
    ) -> PageEmbedding | None:
        """The current version's embedding: the fresh identity's first, else the newest."""
        preferred: list[Any] = []
        if identity is not None:
            preferred.append(case((self._fresh_embedding(identity.model), 0), else_=1))
        return (
            await session.scalars(
                select(PageEmbedding)
                .where(
                    PageEmbedding.page_id == page.id,
                    PageEmbedding.page_version == page.current_version,
                )
                .order_by(*preferred, PageEmbedding.created_at.desc(), PageEmbedding.id)
                .limit(1)
            )
        ).first()

    async def _relationship_counts(self, session: AsyncSession, page: Page) -> dict[str, int]:
        target = aliased(Page)
        rows = (
            await session.execute(
                select(PageRelationship.relationship_type, func.count())
                .join(
                    target,
                    and_(
                        target.id == PageRelationship.target_page_id,
                        target.current_version == PageRelationship.target_page_version,
                    ),
                )
                .where(
                    PageRelationship.source_page_id == page.id,
                    PageRelationship.source_page_version == page.current_version,
                    _stored_type(),
                    _semantic_matches(await self._identity(), self._fingerprint),
                )
                .group_by(PageRelationship.relationship_type)
                .order_by(PageRelationship.relationship_type)
            )
        ).all()
        return {str(row[0]): int(row[1]) for row in rows}

    # ------------------------------------------------------------ similarity
    async def list_similar(self, *, page_id: uuid.UUID, limit: int) -> ListResponse[SimilarPage]:
        _check_paging(limit, 0)
        av = analysis_version(self._taxonomy.version)
        identity = await self._identity()
        async with self._sessionmaker() as session:
            page = await self._page(session, page_id)
            empty = ListResponse[SimilarPage](
                items=[], page=PageInfo(total=0, limit=limit, offset=0)
            )
            if identity is None:
                return empty
            source = (
                await session.execute(
                    select(PageEmbedding.id, PageEmbedding.dimensions).where(
                        PageEmbedding.page_id == page.id,
                        PageEmbedding.page_version == page.current_version,
                        self._fresh_embedding(identity.model),
                    )
                )
            ).one_or_none()
            if source is None:
                return empty
            vector = (
                select(PageEmbedding.embedding)
                .where(PageEmbedding.id == source.id)
                .scalar_subquery()
            )
            statement = self._similar_select(
                distance=cosine_distance(PageEmbedding.embedding, vector),
                model=identity.model,
                dimensions=source.dimensions,
                av=av,
                exclude=page.id,
                limit=limit,
            )
            return await self._similar(session, statement, limit)

    async def semantic_search(self, *, query: str, limit: int) -> ListResponse[SimilarPage]:
        _check_paging(limit, 0)
        av = analysis_version(self._taxonomy.version)
        try:
            identity = await embedding_identity(self._provider)
            result = await self._provider.embed(
                texts=[self._settings.embedding_query_prefix + query], role=ModelRole.EMBEDDING
            )
        except Exception as exc:
            # The class name only: a provider message may name its host.
            logger.warning("semantic_search_embedding_failed", error_class=type(exc).__name__)
            raise EmbeddingUnavailableError("the embedding model is unavailable") from None
        vectors = result.vectors
        if (
            result.model != identity.model
            or len(vectors) != 1
            or len(vectors[0]) != result.dimensions
            or not all(math.isfinite(v) for v in vectors[0])
        ):
            # Never compare a vector from another model with the stored ones.
            logger.warning("semantic_search_result_rejected")
            raise EmbeddingUnavailableError("the embedding model returned an unusable result")
        query_vector = cast(bindparam("query_vector", list(vectors[0]), type_=Vector()), Vector())
        statement = self._similar_select(
            distance=cosine_distance(PageEmbedding.embedding, query_vector),
            model=identity.model,
            dimensions=result.dimensions,
            av=av,
            exclude=None,
            limit=limit,
        )
        async with self._sessionmaker() as session:
            return await self._similar(session, statement, limit)

    # --------------------------------------------------------- relationships
    async def list_relationships(
        self,
        *,
        page_id: uuid.UUID,
        limit: int,
        offset: int,
        relationship_type: RelationshipType | None = None,
    ) -> ListResponse[PageRelationshipView]:
        _check_paging(limit, offset)
        target = aliased(Page)
        async with self._sessionmaker() as session:
            page = await self._page(session, page_id)
            conditions: list[ColumnElement[bool]] = [
                PageRelationship.source_page_id == page.id,
                PageRelationship.source_page_version == page.current_version,
                _stored_type(),
                _semantic_matches(await self._identity(), self._fingerprint),
            ]
            if relationship_type is not None:
                conditions.append(
                    PageRelationship.relationship_type == RelationshipType(relationship_type).value
                )
            joined = and_(
                target.id == PageRelationship.target_page_id,
                target.current_version == PageRelationship.target_page_version,
            )
            total = int(
                await session.scalar(
                    select(func.count())
                    .select_from(PageRelationship)
                    .join(target, joined)
                    .where(*conditions)
                )
                or 0
            )
            rows = (
                await session.execute(
                    select(PageRelationship, target.canonical_url, target.title)
                    .join(target, joined)
                    .where(*conditions)
                    .order_by(
                        PageRelationship.weight.desc(),
                        PageRelationship.target_page_id,
                        PageRelationship.relationship_type,
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        items = [
            PageRelationshipView(
                source_page_id=edge.source_page_id,
                source_page_version=edge.source_page_version,
                target_page_id=edge.target_page_id,
                target_page_version=edge.target_page_version,
                target_canonical_url=url,
                target_title=title,
                relationship_type=RelationshipType(edge.relationship_type),
                weight=edge.weight,
                evidence=dict(edge.evidence),
                analysis_version=edge.analysis_version,
                computed_at=edge.computed_at,
            )
            for edge, url, title in rows
        ]
        return ListResponse[PageRelationshipView](
            items=items, page=PageInfo(total=total, limit=limit, offset=offset)
        )

    # -------------------------------------------------------------- taxonomy
    async def get_taxonomy(self) -> TaxonomyView:
        taxonomy = self._taxonomy
        av = analysis_version(taxonomy.version)
        async with self._sessionmaker() as session:
            topic_counts = await self._node_counts(session, av, "topics")
            geo_counts = await self._node_counts(session, av, "geographies")
            type_rows = (
                await session.execute(
                    select(PageAnalysisRow.page_type, func.count())
                    .join(Page, fresh_analysis_join(av))
                    .group_by(PageAnalysisRow.page_type)
                )
            ).all()
        type_counts = {str(row[0]): int(row[1]) for row in type_rows}
        market_depth = _depths({m.id: m.parent for m in taxonomy.markets})
        geo_depth = _depths({g.id: g.parent for g in taxonomy.geographies})
        return TaxonomyView(
            version=taxonomy.version,
            status=taxonomy.status,
            source=taxonomy.source,
            analysis_version=av,
            markets=[
                TaxonomyNodeView(
                    id=m.id,
                    label=m.label,
                    kind="market",
                    parent=m.parent,
                    depth=market_depth[m.id],
                    page_count=topic_counts.get(m.id, 0),
                )
                for m in taxonomy.markets
            ],
            geographies=[
                TaxonomyNodeView(
                    id=g.id,
                    label=g.label,
                    kind=g.kind,
                    parent=g.parent,
                    depth=geo_depth[g.id],
                    page_count=geo_counts.get(g.id, 0),
                )
                for g in taxonomy.geographies
            ],
            page_types=[
                PageTypeView(
                    id=rule.id,
                    label=rule.label,
                    intent=rule.intent,
                    commercial_importance=rule.commercial_importance,
                    linkable_as_source=rule.linkable_as_source,
                    linkable_as_target=rule.linkable_as_target,
                    page_count=type_counts.get(rule.id, 0),
                )
                for rule in taxonomy.page_types
            ],
        )

    @staticmethod
    async def _node_counts(
        session: AsyncSession, av: str, column: Literal["topics", "geographies"]
    ) -> dict[str, int]:
        """Distinct pages per node id listed in the fresh analyses' ``column`` array."""
        rows = (await session.execute(_NODE_COUNT_SQL[column], {"av": av})).all()
        return {str(row[0]): int(row[1]) for row in rows if row[0] is not None}

    async def list_topic_pages(
        self, *, node_id: str, limit: int, offset: int
    ) -> ListResponse[SimilarPage]:
        _check_paging(limit, offset)
        taxonomy = self._taxonomy
        markets = {m.id for m in taxonomy.markets}
        geographies = {g.id for g in taxonomy.geographies}
        if node_id not in markets and node_id not in geographies:
            raise IntelligenceNotFoundError("taxonomy_node")
        av = analysis_version(taxonomy.version)
        membership: list[ColumnElement[bool]] = []
        if node_id in markets:
            membership.append(PageAnalysisRow.topics.contains([{"node_id": node_id}]))
        if node_id in geographies:
            membership.append(PageAnalysisRow.geographies.contains([{"geo_id": node_id}]))
        base = select(PageAnalysisRow).join(Page, fresh_analysis_join(av)).where(or_(*membership))
        async with self._sessionmaker() as session:
            total = int(
                await session.scalar(select(func.count()).select_from(base.subquery())) or 0
            )
            rows = (
                await session.execute(
                    select(
                        Page.id,
                        Page.canonical_url,
                        Page.title,
                        PageAnalysisRow.page_type,
                        PageAnalysisRow.market_id,
                    )
                    .join(Page, fresh_analysis_join(av))
                    .where(or_(*membership))
                    .order_by(Page.canonical_url, Page.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        items = [
            SimilarPage(
                page_id=row.id,
                canonical_url=row.canonical_url,
                title=row.title,
                score=1.0,
                page_type=row.page_type,
                market_id=row.market_id,
            )
            for row in rows
        ]
        return ListResponse[SimilarPage](
            items=items, page=PageInfo(total=total, limit=limit, offset=offset)
        )

    # -------------------------------------------------------------- coverage
    async def get_coverage(self) -> IntelligenceCoverage:
        taxonomy = self._taxonomy
        av = analysis_version(taxonomy.version)
        identity = await self._identity()
        current = aliased(PageAnalysisRow)
        fresh_exists = (
            exists()
            .where(
                PageAnalysisRow.page_id == Page.id,
                PageAnalysisRow.page_version == Page.current_version,
                PageAnalysisRow.analysis_version == av,
            )
            .label("fresh")
        )
        async with self._sessionmaker() as session:
            states = (
                await session.execute(
                    select(
                        Page.id,
                        Page.publication_state,
                        Page.gone_at,
                        Page.word_count,
                        PageVersion.robots_directives,
                        PageVersion.canonical_status,
                        PageVersion.http_status,
                        current.page_type,
                        fresh_exists,
                        _any_analysis().label("any_analysis"),
                        _active_protection().label("protected"),
                    )
                    .select_from(Page)
                    .join(
                        PageVersion,
                        and_(
                            PageVersion.page_id == Page.id,
                            PageVersion.version == Page.current_version,
                        ),
                    )
                    .outerjoin(current, current.id == _current_analysis(av))
                    .order_by(Page.id)
                )
            ).all()
            embedded_fresh = 0
            if identity is not None:
                embedded_fresh = int(
                    await session.scalar(
                        select(func.count(func.distinct(PageEmbedding.page_id)))
                        .join(
                            Page,
                            and_(
                                Page.id == PageEmbedding.page_id,
                                Page.current_version == PageEmbedding.page_version,
                            ),
                        )
                        .where(self._fresh_embedding(identity.model))
                    )
                    or 0
                )
            source, target = aliased(Page), aliased(Page)
            relationships = int(
                await session.scalar(
                    select(func.count())
                    .select_from(PageRelationship)
                    .where(_stored_type())
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
                )
                or 0
            )
            by_type = (
                await session.execute(
                    select(PageAnalysisRow.page_type, func.count())
                    .join(Page, fresh_analysis_join(av))
                    .group_by(PageAnalysisRow.page_type)
                    .order_by(PageAnalysisRow.page_type)
                )
            ).all()
            by_industry = (
                await session.execute(
                    select(PageAnalysisRow.industry_id, func.count())
                    .join(Page, fresh_analysis_join(av))
                    .where(PageAnalysisRow.industry_id.is_not(None))
                    .group_by(PageAnalysisRow.industry_id)
                    .order_by(PageAnalysisRow.industry_id)
                )
            ).all()

        total = len(states)
        fresh = sum(1 for row in states if row.fresh)
        analysed_any = sum(1 for row in states if row.any_analysis)
        eligible_sources = eligible_targets = 0
        for row in states:
            result = self._eligibility(
                self._state(
                    publication_state=row.publication_state,
                    gone=row.gone_at is not None,
                    protected=bool(row.protected),
                    robots=row.robots_directives,
                    canonical_status=row.canonical_status,
                    http_status=row.http_status,
                    word_count=row.word_count,
                    page_type=row.page_type,
                    has_analysis=bool(row.any_analysis),
                    analysis_fresh=bool(row.fresh),
                ),
                taxonomy,
            )
            eligible_sources += int(result.as_source)
            eligible_targets += int(result.as_target)
        return IntelligenceCoverage(
            total_pages=total,
            analysis_version=av,
            analyzed_fresh=fresh,
            analyzed_stale=analysed_any - fresh,
            never_analyzed=total - analysed_any,
            embedding_model=identity.model if identity is not None else NO_EMBEDDING_MODEL,
            embedded_fresh=embedded_fresh,
            embedded_stale_or_missing=total - embedded_fresh,
            protected=sum(1 for row in states if row.protected),
            eligible_sources=eligible_sources,
            eligible_targets=eligible_targets,
            relationships=relationships,
            by_page_type={str(row[0]): int(row[1]) for row in by_type},
            by_industry={str(row[0]): int(row[1]) for row in by_industry},
        )
