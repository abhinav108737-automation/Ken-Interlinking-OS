"""Retrieval channels (ADR-0011 section 2): SQL over CURRENT versions, one source at a time.

Each channel returns ``{target_page_id: channel_score}`` for ONE source page. Targets are
never the source and never gone; eligibility is decided later by the filter chain (so a
rejected candidate is still recorded, with its code).

=========  ===================================================================  ==============
channel    candidates                                                           score
=========  ===================================================================  ==============
semantic   top ``semantic_k`` by exact cosine KNN over current-version          cosine
           embeddings of the CURRENT identity (model, dimensions, recipe
           version, recipe fingerprint); zero vectors are skipped
lexical    top ``lexical_k`` by ``ts_rank_cd(page_versions.search_vector, q)``  ts_rank_cd
           where ``q`` is the OR of ``plainto_tsquery('english', keyword)``
           over the source analysis keywords. Keywords are BOUND parameters:
           no text is ever spliced into SQL
taxonomy   stored non-semantic ``page_relationships`` from the source whose     max weight
           two endpoint versions are both current
graph      top ``graph_k`` targets in the source's industry (fresh analyses),   1/(1+inbound)
           fewest inbound internal links first, then commercial importance
           desc, then page id
=========  ===================================================================  ==============

:func:`retrieve` unions the enabled channels into :class:`RetrievedCandidate` s (each with
the set of channels that found it) and keeps the best ``max_candidates_per_source`` by
(max channel score desc, target id).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import and_, bindparam, cast, func, literal, select
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ken_contracts.intelligence import EMBEDDING_RECIPE_VERSION, RelationshipType
from ken_contracts.recommendations import RetrievalChannel, RetrievalPolicy
from ken_core.db.models.intelligence import PageAnalysis as PageAnalysisRow
from ken_core.db.models.intelligence import PageEmbedding, PageRelationship
from ken_core.db.models.inventory import Page, PageVersion
from ken_core.db.repositories.intelligence import (
    cosine_distance,
    current_embedding_join,
    fresh_analysis_join,
)
from ken_core.db.repositories.recommendations import inbound_count_expression

__all__ = [
    "MAX_LEXICAL_KEYWORDS",
    "EmbeddingKey",
    "RetrievedCandidate",
    "graph_channel",
    "lexical_channel",
    "retrieve",
    "semantic_channel",
    "taxonomy_channel",
]

MAX_LEXICAL_KEYWORDS: Final = 25
"""Bound on the OR-query size (the analysis keeps far fewer)."""
_SEMANTIC: Final = RelationshipType.SEMANTIC_SIMILAR.value


@dataclass(frozen=True, slots=True)
class EmbeddingKey:
    """The CURRENT embedding identity: provider-reported model + recipe fingerprint."""

    model: str
    recipe_fingerprint: str
    recipe_version: int = EMBEDDING_RECIPE_VERSION


@dataclass(frozen=True, slots=True)
class RetrievedCandidate:
    target_page_id: uuid.UUID
    channels: tuple[RetrievalChannel, ...]
    channel_scores: dict[str, float]

    @property
    def best(self) -> float:
        return max(self.channel_scores.values(), default=0.0)


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


# --------------------------------------------------------------------------- #
# semantic
# --------------------------------------------------------------------------- #
async def source_embedding(
    session: AsyncSession, *, source_page_id: uuid.UUID, key: EmbeddingKey
) -> tuple[uuid.UUID, int] | None:
    """(embedding id, dimensions) of the source's current-version, current-identity vector."""
    row = (
        await session.execute(
            select(PageEmbedding.id, PageEmbedding.dimensions)
            .join(
                Page, current_embedding_join(key.model, key.recipe_version, key.recipe_fingerprint)
            )
            .where(Page.id == source_page_id)
            .order_by(PageEmbedding.created_at.desc(), PageEmbedding.id)
            .limit(1)
        )
    ).first()
    return None if row is None else (row[0], int(row[1]))


async def semantic_channel(
    session: AsyncSession, *, source_page_id: uuid.UUID, key: EmbeddingKey, k: int
) -> dict[uuid.UUID, float]:
    if k <= 0:
        return {}
    source = await source_embedding(session, source_page_id=source_page_id, key=key)
    if source is None:
        return {}
    embedding_id, dimensions = source
    vector = (
        select(PageEmbedding.embedding).where(PageEmbedding.id == embedding_id).scalar_subquery()
    )
    distance = cosine_distance(PageEmbedding.embedding, vector)
    rows = (
        await session.execute(
            select(PageEmbedding.page_id, distance.label("distance"))
            .join(
                Page, current_embedding_join(key.model, key.recipe_version, key.recipe_fingerprint)
            )
            .where(
                PageEmbedding.dimensions == dimensions,
                PageEmbedding.page_id != source_page_id,
                Page.gone_at.is_(None),
            )
            .order_by(distance, PageEmbedding.page_id)
            .limit(k)
        )
    ).all()
    out: dict[uuid.UUID, float] = {}
    for page_id, raw in rows:
        dist = _finite(raw)
        if dist is None:  # a zero vector has no direction
            continue
        out.setdefault(page_id, max(-1.0, min(1.0, 1.0 - dist)))
    return out


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
            .order_by(rank.desc(), Page.id)
            .limit(k)
        )
    ).all()
    out: dict[uuid.UUID, float] = {}
    for row in rows:
        value = _finite(row[1])
        if value is not None and value > 0.0:
            out[uuid.UUID(str(row[0]))] = value
    return out


# --------------------------------------------------------------------------- #
# taxonomy
# --------------------------------------------------------------------------- #
async def taxonomy_channel(
    session: AsyncSession, *, source_page_id: uuid.UUID
) -> dict[uuid.UUID, float]:
    source = aliased(Page)
    target = aliased(Page)
    rows = (
        await session.execute(
            select(PageRelationship.target_page_id, func.max(PageRelationship.weight))
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
    return {r[0]: float(r[1]) for r in rows if r[0] != source_page_id}


# --------------------------------------------------------------------------- #
# graph
# --------------------------------------------------------------------------- #
async def graph_channel(
    session: AsyncSession,
    *,
    source_page_id: uuid.UUID,
    industry_id: str | None,
    analysis_version: str,
    k: int,
) -> dict[uuid.UUID, float]:
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


# --------------------------------------------------------------------------- #
# union
# --------------------------------------------------------------------------- #
def union_candidates(
    found: Mapping[RetrievalChannel, Mapping[uuid.UUID, float]],
    *,
    source_page_id: uuid.UUID,
    cap: int,
) -> list[RetrievedCandidate]:
    """Union per target (channels in declaration order); keep the best ``cap``.

    Channel scores live on different scales (cosine <= 1; ``ts_rank_cd`` often 3-8;
    relationship weights; 1/(1+inbound)), so ranking by the raw maximum would let
    the lexical channel dominate the cap. Each channel's scores are therefore
    normalised by that channel's best score for this source (scale-free, in (0, 1])
    and candidates are ranked by the SUM of their normalised scores -- which also
    rewards agreement between channels -- then by target id. The stored
    ``channel_scores`` stay raw, for explainability.
    """
    channels: dict[uuid.UUID, list[RetrievalChannel]] = {}
    scores: dict[uuid.UUID, dict[str, float]] = {}
    normalised: dict[uuid.UUID, float] = {}
    for channel in RetrievalChannel:
        hits = {t: float(v) for t, v in found.get(channel, {}).items() if t != source_page_id}
        top = max(hits.values(), default=0.0)
        for target, score in hits.items():
            channels.setdefault(target, []).append(channel)
            scores.setdefault(target, {})[channel.value] = score
            share = score / top if top > 0 else 0.0
            normalised[target] = normalised.get(target, 0.0) + max(0.0, share)
    items = [
        RetrievedCandidate(target_page_id=t, channels=tuple(channels[t]), channel_scores=scores[t])
        for t in channels
    ]
    items.sort(key=lambda c: (-normalised[c.target_page_id], c.target_page_id))
    return items[:cap]


async def retrieve(
    session: AsyncSession,
    *,
    source_page_id: uuid.UUID,
    retrieval: RetrievalPolicy,
    embedding: EmbeddingKey | None,
    keywords: Sequence[str],
    industry_id: str | None,
    analysis_version: str,
) -> list[RetrievedCandidate]:
    """Every enabled channel for one source, unioned and capped (module docstring)."""
    enabled = set(retrieval.channels_enabled)
    found: dict[RetrievalChannel, dict[uuid.UUID, float]] = {}
    if RetrievalChannel.SEMANTIC in enabled and embedding is not None:
        found[RetrievalChannel.SEMANTIC] = await semantic_channel(
            session, source_page_id=source_page_id, key=embedding, k=retrieval.semantic_k
        )
    if RetrievalChannel.LEXICAL in enabled:
        found[RetrievalChannel.LEXICAL] = await lexical_channel(
            session, source_page_id=source_page_id, keywords=keywords, k=retrieval.lexical_k
        )
    if RetrievalChannel.TAXONOMY in enabled:
        found[RetrievalChannel.TAXONOMY] = await taxonomy_channel(
            session, source_page_id=source_page_id
        )
    if RetrievalChannel.GRAPH in enabled:
        found[RetrievalChannel.GRAPH] = await graph_channel(
            session,
            source_page_id=source_page_id,
            industry_id=industry_id,
            analysis_version=analysis_version,
            k=retrieval.graph_k,
        )
    return union_candidates(
        found, source_page_id=source_page_id, cap=retrieval.max_candidates_per_source
    )


async def cosine_to(
    session: AsyncSession,
    *,
    source_page_id: uuid.UUID,
    target_page_ids: Sequence[uuid.UUID],
    key: EmbeddingKey,
) -> dict[uuid.UUID, float]:
    """Cosine of the source's current vector to each target's (current identity; scoring)."""
    if not target_page_ids:
        return {}
    source = await source_embedding(session, source_page_id=source_page_id, key=key)
    if source is None:
        return {}
    embedding_id, dimensions = source
    vector = (
        select(PageEmbedding.embedding).where(PageEmbedding.id == embedding_id).scalar_subquery()
    )
    distance = cosine_distance(PageEmbedding.embedding, vector)
    rows = (
        await session.execute(
            select(PageEmbedding.page_id, distance)
            .join(
                Page, current_embedding_join(key.model, key.recipe_version, key.recipe_fingerprint)
            )
            .where(
                PageEmbedding.dimensions == dimensions,
                PageEmbedding.page_id.in_(list(target_page_ids)),
            )
            .order_by(PageEmbedding.page_id, PageEmbedding.created_at.desc())
        )
    ).all()
    out: dict[uuid.UUID, float] = {}
    for page_id, raw in rows:
        dist = _finite(raw)
        if dist is not None:
            out.setdefault(page_id, max(-1.0, min(1.0, 1.0 - dist)))
    return out
