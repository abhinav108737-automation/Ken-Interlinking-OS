"""Content-intelligence repository: SQL primitives for analysis, embeddings,
relationships and protections (ADR-0010).

The caller owns the transaction: nothing here commits. There is deliberately no
function that updates or deletes a ``page_analyses`` or ``page_embeddings`` row
(migration 0003's trigger would refuse it anyway). Protections are deactivated,
never deleted. ``page_relationships`` is derived data and is replaced per run.

"Fresh" always means: for the page's CURRENT version (``pages.current_version``)
and, for analyses, the current ``analysis_version``; for embeddings, the current
(model, recipe version, recipe fingerprint) identity. Similarity queries
additionally filter on ``dimensions``, so vectors of two models or two text
recipes are never compared.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import ColumnElement, Float, and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.intelligence import PageAnalysis, RelationshipType
from ken_core.db.models.intelligence import PageAnalysis as PageAnalysisRow
from ken_core.db.models.intelligence import (
    PageEmbedding,
    PageProtection,
    PageRelationship,
)
from ken_core.db.models.inventory import Page, PageVersion

SEMANTIC: Final = RelationshipType.SEMANTIC_SIMILAR.value


# --------------------------------------------------------------------------- #
# Shared predicates
# --------------------------------------------------------------------------- #
def fresh_analysis_join(analysis_version: str) -> ColumnElement[bool]:
    """``page_analyses`` row of the page's CURRENT version and ``analysis_version``."""
    return and_(
        PageAnalysisRow.page_id == Page.id,
        PageAnalysisRow.page_version == Page.current_version,
        PageAnalysisRow.analysis_version == analysis_version,
    )


def embedding_identity_filter(
    *, model: str, recipe_version: int, recipe_fingerprint: str
) -> ColumnElement[bool]:
    """``page_embeddings`` rows of one (model, recipe version, recipe fingerprint)."""
    return and_(
        PageEmbedding.model == model,
        PageEmbedding.recipe_version == recipe_version,
        PageEmbedding.recipe_fingerprint == recipe_fingerprint,
    )


def current_embedding_join(
    model: str, recipe_version: int, recipe_fingerprint: str
) -> ColumnElement[bool]:
    """``page_embeddings`` row of the page's CURRENT version for (model, recipe, fingerprint)."""
    return and_(
        PageEmbedding.page_id == Page.id,
        PageEmbedding.page_version == Page.current_version,
        embedding_identity_filter(
            model=model, recipe_version=recipe_version, recipe_fingerprint=recipe_fingerprint
        ),
    )


def cosine_distance(left: Any, right: Any) -> ColumnElement[float]:
    """pgvector cosine distance (``<=>``); similarity is ``1 - distance``."""
    return left.op("<=>", return_type=Float())(right)  # type: ignore[no-any-return]


def analysis_from_row(row: PageAnalysisRow) -> PageAnalysis:
    """Re-validate a stored analysis through the contract on the way out."""
    return PageAnalysis.model_validate(
        {
            "page_id": row.page_id,
            "page_version": row.page_version,
            "analysis_version": row.analysis_version,
            "extractor": row.extractor,
            "page_type": row.page_type,
            "intent": row.intent,
            "commercial_importance": row.commercial_importance,
            "industry_id": row.industry_id,
            "market_id": row.market_id,
            "topics": row.topics,
            "geographies": row.geographies,
            "entities": row.entities,
            "keywords": row.keywords,
            "summary": row.summary,
            "salient_blocks": row.salient_blocks,
        }
    )


# --------------------------------------------------------------------------- #
# Pages and their current versions
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PageRef:
    id: uuid.UUID
    canonical_url: str
    current_version: int
    gone: bool


@dataclass(frozen=True, slots=True)
class VersionContent:
    """What analysis and the embedding recipe read from one page version."""

    page_id: uuid.UUID
    version: int
    url: str
    title: str | None
    meta_description: str | None
    headings: tuple[tuple[int, str], ...]
    body_html: str
    body_text: str
    metadata: dict[str, str]


def _headings(raw: Sequence[Mapping[str, Any]]) -> tuple[tuple[int, str], ...]:
    out: list[tuple[int, str]] = []
    for heading in raw:
        try:
            out.append((int(heading["level"]), str(heading["text"])))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(out)


async def list_pages(
    session: AsyncSession, *, page_ids: Sequence[uuid.UUID] | None = None
) -> list[PageRef]:
    """Pages (gone ones included) in id order, optionally restricted to ``page_ids``."""
    statement = select(Page.id, Page.canonical_url, Page.current_version, Page.gone_at).order_by(
        Page.id
    )
    if page_ids is not None:
        statement = statement.where(Page.id.in_(list(page_ids)))
    rows = (await session.execute(statement)).all()
    return [
        PageRef(
            id=r.id,
            canonical_url=r.canonical_url,
            current_version=r.current_version,
            gone=r.gone_at is not None,
        )
        for r in rows
    ]


async def load_version_content(
    session: AsyncSession, *, page_id: uuid.UUID, version: int
) -> VersionContent | None:
    row = (
        await session.execute(
            select(PageVersion, Page.canonical_url)
            .join(Page, Page.id == PageVersion.page_id)
            .where(PageVersion.page_id == page_id, PageVersion.version == version)
        )
    ).first()
    if row is None:
        return None
    version_row: PageVersion = row[0]
    return VersionContent(
        page_id=version_row.page_id,
        version=version_row.version,
        url=row[1],
        title=version_row.title,
        meta_description=version_row.meta_description,
        headings=_headings(version_row.headings),
        body_html=version_row.body_html,
        body_text=version_row.body_text,
        metadata={str(k): str(v) for k, v in dict(version_row.page_metadata).items()},
    )


# --------------------------------------------------------------------------- #
# Analyses
# --------------------------------------------------------------------------- #
async def analysed_versions(
    session: AsyncSession, *, analysis_version: str, page_ids: Iterable[uuid.UUID]
) -> set[tuple[uuid.UUID, int]]:
    """(page id, version) pairs that already have an analysis of ``analysis_version``."""
    ids = list(page_ids)
    if not ids:
        return set()
    rows = (
        await session.execute(
            select(PageAnalysisRow.page_id, PageAnalysisRow.page_version).where(
                PageAnalysisRow.analysis_version == analysis_version,
                PageAnalysisRow.page_id.in_(ids),
            )
        )
    ).all()
    return {(r.page_id, r.page_version) for r in rows}


async def insert_analysis(
    session: AsyncSession,
    *,
    analysis: PageAnalysis,
    run_id: uuid.UUID | None,
    created_at: datetime,
) -> bool:
    """Insert one analysis; False if (page version, analysis version) already exists."""
    dumped = analysis.model_dump(mode="json")
    result = await session.execute(
        insert(PageAnalysisRow)
        .values(
            id=uuid.uuid4(),
            page_id=analysis.page_id,
            page_version=analysis.page_version,
            analysis_version=analysis.analysis_version,
            extractor=analysis.extractor,
            page_type=analysis.page_type,
            intent=analysis.intent.value,
            commercial_importance=analysis.commercial_importance,
            industry_id=analysis.industry_id,
            market_id=analysis.market_id,
            topics=dumped["topics"],
            geographies=dumped["geographies"],
            entities=dumped["entities"],
            keywords=dumped["keywords"],
            summary=analysis.summary,
            salient_blocks=dumped["salient_blocks"],
            run_id=run_id,
            created_at=created_at,
        )
        .on_conflict_do_nothing(
            index_elements=[
                PageAnalysisRow.page_id,
                PageAnalysisRow.page_version,
                PageAnalysisRow.analysis_version,
            ]
        )
        .returning(PageAnalysisRow.id)
    )
    return result.scalar_one_or_none() is not None


@dataclass(frozen=True, slots=True)
class FreshAnalysis:
    page_id: uuid.UUID
    page_version: int
    analysis: PageAnalysis


async def fresh_analyses(
    session: AsyncSession, *, analysis_version: str
) -> dict[uuid.UUID, FreshAnalysis]:
    """Every page's analysis of its CURRENT version and ``analysis_version``."""
    rows = (
        await session.scalars(
            select(PageAnalysisRow)
            .join(Page, fresh_analysis_join(analysis_version))
            .order_by(PageAnalysisRow.page_id)
        )
    ).all()
    return {
        row.page_id: FreshAnalysis(
            page_id=row.page_id, page_version=row.page_version, analysis=analysis_from_row(row)
        )
        for row in rows
    }


# --------------------------------------------------------------------------- #
# Protections
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ActiveProtection:
    id: int
    source: str
    rule_id: str | None
    created_by: str


async def active_protections(
    session: AsyncSession, *, page_id: uuid.UUID
) -> list[ActiveProtection]:
    rows = (
        await session.execute(
            select(
                PageProtection.id,
                PageProtection.source,
                PageProtection.rule_id,
                PageProtection.created_by,
            )
            .where(PageProtection.page_id == page_id, PageProtection.active.is_(True))
            .order_by(PageProtection.id)
            .with_for_update()
        )
    ).all()
    return [
        ActiveProtection(id=r.id, source=r.source, rule_id=r.rule_id, created_by=r.created_by)
        for r in rows
    ]


async def insert_protection(
    session: AsyncSession,
    *,
    page_id: uuid.UUID,
    source: str,
    rule_id: str | None,
    reason: str,
    created_by: str,
    created_at: datetime,
) -> bool:
    """Insert an ACTIVE protection; False if an active one for (page, source, rule) exists."""
    result = await session.execute(
        insert(PageProtection)
        .values(
            page_id=page_id,
            source=source,
            rule_id=rule_id,
            reason=reason,
            active=True,
            created_by=created_by,
            created_at=created_at,
        )
        # The partial unique index uq_page_protections_active is the arbiter.
        .on_conflict_do_nothing()
        .returning(PageProtection.id)
    )
    return result.scalar_one_or_none() is not None


async def deactivate_protection(
    session: AsyncSession, *, protection_id: int, deactivated_by: str, now: datetime
) -> None:
    await session.execute(
        update(PageProtection)
        .where(PageProtection.id == protection_id, PageProtection.active.is_(True))
        .values(active=False, deactivated_at=now, deactivated_by=deactivated_by)
    )


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #
async def embedded_versions(
    session: AsyncSession,
    *,
    model: str,
    recipe_version: int,
    recipe_fingerprint: str,
    pages: Iterable[tuple[uuid.UUID, int]],
) -> set[tuple[uuid.UUID, int]]:
    """Which (page id, version) pairs already have an embedding for (model, recipe, fingerprint)."""
    wanted = list(pages)
    if not wanted:
        return set()
    rows = (
        await session.execute(
            select(PageEmbedding.page_id, PageEmbedding.page_version).where(
                embedding_identity_filter(
                    model=model,
                    recipe_version=recipe_version,
                    recipe_fingerprint=recipe_fingerprint,
                ),
                PageEmbedding.page_id.in_([page_id for page_id, _ in wanted]),
            )
        )
    ).all()
    return {(r.page_id, r.page_version) for r in rows} & set(wanted)


@dataclass(frozen=True, slots=True)
class NewEmbedding:
    page_id: uuid.UUID
    page_version: int
    model: str
    provider: str
    dimensions: int
    recipe_version: int
    recipe_fingerprint: str
    input_hash: str
    vector: list[float]


async def insert_embeddings(
    session: AsyncSession,
    *,
    embeddings: Sequence[NewEmbedding],
    run_id: uuid.UUID | None,
    created_at: datetime,
) -> set[uuid.UUID]:
    """Insert embeddings (skipping existing identities); returns the page ids written."""
    written: set[uuid.UUID] = set()
    for item in embeddings:
        result = await session.execute(
            insert(PageEmbedding)
            .values(
                id=uuid.uuid4(),
                page_id=item.page_id,
                page_version=item.page_version,
                model=item.model,
                provider=item.provider,
                dimensions=item.dimensions,
                recipe_version=item.recipe_version,
                recipe_fingerprint=item.recipe_fingerprint,
                input_hash=item.input_hash,
                embedding=item.vector,
                run_id=run_id,
                created_at=created_at,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    PageEmbedding.page_id,
                    PageEmbedding.page_version,
                    PageEmbedding.model,
                    PageEmbedding.recipe_version,
                    PageEmbedding.recipe_fingerprint,
                ]
            )
            .returning(PageEmbedding.page_id)
        )
        page_id = result.scalar_one_or_none()
        if page_id is not None:
            written.add(page_id)
    return written


@dataclass(frozen=True, slots=True)
class EmbeddedPage:
    page_id: uuid.UUID
    page_version: int
    embedding_id: uuid.UUID
    dimensions: int


async def similarity_candidates(
    session: AsyncSession,
    *,
    model: str,
    recipe_version: int,
    recipe_fingerprint: str,
    analysis_version: str,
) -> dict[uuid.UUID, EmbeddedPage]:
    """Pages eligible for ``semantic_similar`` edges: a fresh analysis AND a current embedding."""
    rows = (
        await session.execute(
            select(
                PageEmbedding.page_id,
                PageEmbedding.page_version,
                PageEmbedding.id,
                PageEmbedding.dimensions,
            )
            .join(Page, current_embedding_join(model, recipe_version, recipe_fingerprint))
            .join(PageAnalysisRow, fresh_analysis_join(analysis_version))
            .order_by(PageEmbedding.page_id)
        )
    ).all()
    return {
        r.page_id: EmbeddedPage(
            page_id=r.page_id,
            page_version=r.page_version,
            embedding_id=r.id,
            dimensions=r.dimensions,
        )
        for r in rows
    }


async def nearest_candidates(
    session: AsyncSession,
    *,
    source_embedding_id: uuid.UUID,
    source_page_id: uuid.UUID,
    model: str,
    dimensions: int,
    recipe_version: int,
    recipe_fingerprint: str,
    analysis_version: str,
    limit: int | None,
    min_score: float | None = None,
) -> list[tuple[uuid.UUID, int, float]]:
    """(page id, version, cosine) of the nearest similarity candidates, best first.

    Exact KNN (no index): O(candidates) per call. Candidates are current-version
    embeddings of the same (model, dimensions, recipe, fingerprint) whose page also has a
    fresh analysis; the source page itself is excluded.
    """
    source = (
        select(PageEmbedding.embedding)
        .where(PageEmbedding.id == source_embedding_id)
        .scalar_subquery()
    )
    distance = cosine_distance(PageEmbedding.embedding, source)
    statement = (
        select(PageEmbedding.page_id, PageEmbedding.page_version, (1.0 - distance).label("score"))
        .join(Page, current_embedding_join(model, recipe_version, recipe_fingerprint))
        .join(PageAnalysisRow, fresh_analysis_join(analysis_version))
        .where(PageEmbedding.dimensions == dimensions, PageEmbedding.page_id != source_page_id)
        .order_by(distance, PageEmbedding.page_id)
    )
    if min_score is not None:
        statement = statement.where(1.0 - distance >= min_score)
    if limit is not None:
        statement = statement.limit(limit)
    rows = (await session.execute(statement)).all()
    return [(r.page_id, r.page_version, float(r.score)) for r in rows]


# --------------------------------------------------------------------------- #
# Relationships (derived, replaceable)
# --------------------------------------------------------------------------- #
def _type_filter(*, semantic: bool) -> ColumnElement[bool]:
    if semantic:
        return PageRelationship.relationship_type == SEMANTIC
    return PageRelationship.relationship_type != SEMANTIC


async def delete_relationships_touching(
    session: AsyncSession, *, page_ids: Iterable[uuid.UUID], semantic: bool
) -> set[uuid.UUID]:
    """Delete every taxonomy (or semantic) edge with source OR target in ``page_ids``.

    Returns the OTHER endpoints of the deleted edges (pages outside
    ``page_ids`` whose edge set just changed).
    """
    ids = list(page_ids)
    if not ids:
        return set()
    result = await session.execute(
        delete(PageRelationship)
        .where(
            _type_filter(semantic=semantic),
            or_(
                PageRelationship.source_page_id.in_(ids),
                PageRelationship.target_page_id.in_(ids),
            ),
        )
        .returning(PageRelationship.source_page_id, PageRelationship.target_page_id)
    )
    touched = {page for row in result.all() for page in (row[0], row[1])}
    return touched - set(ids)


async def delete_outgoing_relationships(
    session: AsyncSession, *, page_ids: Iterable[uuid.UUID], semantic: bool
) -> int:
    """Delete every taxonomy (or semantic) edge whose SOURCE is in ``page_ids``."""
    ids = list(page_ids)
    if not ids:
        return 0
    result = await session.execute(
        delete(PageRelationship)
        .where(_type_filter(semantic=semantic), PageRelationship.source_page_id.in_(ids))
        .returning(PageRelationship.id)
    )
    return len(result.all())


async def delete_outgoing_semantic(session: AsyncSession, *, page_ids: Iterable[uuid.UUID]) -> None:
    await delete_outgoing_relationships(session, page_ids=page_ids, semantic=True)


async def semantic_sources_from_other_embeddings(
    session: AsyncSession, *, model: str, recipe_fingerprint: str
) -> set[uuid.UUID]:
    """Sources whose stored ``semantic_similar`` edges came from another model or recipe.

    Each semantic edge records the embedding identity that produced it in its
    evidence (``model``, ``recipe_fingerprint``); an edge without them predates
    that rule and counts as foreign too.
    """
    rows = (
        await session.execute(
            select(PageRelationship.source_page_id)
            .where(
                PageRelationship.relationship_type == RelationshipType.SEMANTIC_SIMILAR.value,
                or_(
                    PageRelationship.evidence["model"].astext.is_distinct_from(model),
                    PageRelationship.evidence["recipe_fingerprint"].astext.is_distinct_from(
                        recipe_fingerprint
                    ),
                ),
            )
            .distinct()
        )
    ).all()
    return {row[0] for row in rows}


async def semantic_out_stats(
    session: AsyncSession, *, page_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, float]]:
    """(out-degree, minimum weight) of each page's remaining ``semantic_similar`` edges."""
    ids = list(page_ids)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(
                PageRelationship.source_page_id,
                func.count(),
                func.min(PageRelationship.weight),
            )
            .where(_type_filter(semantic=True), PageRelationship.source_page_id.in_(ids))
            .group_by(PageRelationship.source_page_id)
        )
    ).all()
    return {r[0]: (int(r[1]), float(r[2])) for r in rows}


@dataclass(frozen=True, slots=True)
class NewRelationship:
    source_page_id: uuid.UUID
    source_page_version: int
    target_page_id: uuid.UUID
    target_page_version: int
    relationship_type: str
    weight: float
    evidence: dict[str, Any]


async def insert_relationships(
    session: AsyncSession,
    *,
    relationships: Sequence[NewRelationship],
    analysis_version: str,
    run_id: uuid.UUID | None,
    computed_at: datetime,
    chunk_size: int = 500,
) -> int:
    """Upsert edges on (source, target, type); returns the number written."""
    written = 0
    for start in range(0, len(relationships), chunk_size):
        chunk = relationships[start : start + chunk_size]
        if not chunk:
            continue
        statement = insert(PageRelationship).values(
            [
                {
                    "source_page_id": item.source_page_id,
                    "source_page_version": item.source_page_version,
                    "target_page_id": item.target_page_id,
                    "target_page_version": item.target_page_version,
                    "relationship_type": item.relationship_type,
                    "weight": item.weight,
                    "evidence": item.evidence,
                    "analysis_version": analysis_version,
                    "run_id": run_id,
                    "computed_at": computed_at,
                }
                for item in chunk
            ]
        )
        statement = statement.on_conflict_do_update(
            index_elements=[
                PageRelationship.source_page_id,
                PageRelationship.target_page_id,
                PageRelationship.relationship_type,
            ],
            set_={
                "source_page_version": statement.excluded.source_page_version,
                "target_page_version": statement.excluded.target_page_version,
                "weight": statement.excluded.weight,
                "evidence": statement.excluded.evidence,
                "analysis_version": statement.excluded.analysis_version,
                "run_id": statement.excluded.run_id,
                "computed_at": statement.excluded.computed_at,
            },
        )
        await session.execute(statement)
        written += len(chunk)
    return written
