"""Retrieval channels (ADR-0011 section 2; directive section 6): independent generators, unioned.

Each channel is an independent generator (``generators.py``) that returns candidates for ONE
source page. Targets are never the source and never gone; eligibility is decided later by the
filter chain (so a rejected candidate is still recorded, with its code).

=============== ======================================================================= ===========
channel         candidates                                                              raw score
=============== ======================================================================= ===========
semantic        top ``semantic_k`` by cosine through ``VectorStore.search`` (ADR-0017); cosine
                stale points are dropped against PostgreSQL
lexical         top ``lexical_k`` by ``ts_rank_cd`` over the source's keywords          ts_rank_cd
taxonomy        stored non-semantic ``page_relationships`` (both endpoints current)     max weight
graph           low in-degree targets in the source's industry (``page_graph_metrics``) 1/(1+in)
analytics       demand without supply from GSC/GA4 (inert unless ``analytics_k`` > 0)   gsc opp.
orphan_recovery orphan / nearly-orphan pages near the source (inert unless              recovery
                ``orphan_recovery_k`` > 0)                                              value
=============== ======================================================================= ===========

:func:`retrieve` unions the enabled channels into :class:`RetrievedCandidate` s. Each carries
(a) which channels nominated it, (b) a :class:`~ken_contracts.recommendations.CandidateAttribution`
per channel with the raw value, the comparable normalised value, the rank in that channel and
the channel's evidence, and (c) the SEPARATE signals each generator measured -- never one opaque
score. The cap keeps the best ``max_candidates_per_source``.

**Analytics may surface, may block, never alone justify.** An ANALYTICS-only candidate is kept
only if a non-analytics signal supports it (cosine at or above the semantic floor, or a stored
taxonomy relationship); the check is :class:`~ken_services.recommendations.generators.
AnalyticsSupportGate`, which can only block. Analytics also contributes nothing to the cap
ranking of a candidate another channel found.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.recommendations import (
    CandidateAttribution,
    GraphSignals,
    RetrievalChannel,
    RetrievalPolicy,
)
from ken_contracts.vectors import VectorStore, cosine_similarity
from ken_core.db.models.intelligence import PageEmbedding
from ken_core.db.models.inventory import Page
from ken_core.db.repositories import graph as graph_repo
from ken_core.db.repositories.intelligence import current_embedding_join
from ken_services.recommendations.generators import (
    MAX_LEXICAL_KEYWORDS,
    AnalyticsSupportGate,
    CandidateGenerator,
    EmbeddingKey,
    GeneratedCandidate,
    GenerationContext,
    default_generators,
    destination_priorities,
    graph_channel,
    keyword_query,
    lexical_channel,
    semantic_channel,
    source_embedding,
    taxonomy_channel,
)

__all__ = [
    "MAX_LEXICAL_KEYWORDS",
    "EmbeddingKey",
    "RetrievedCandidate",
    "cosine_to",
    "graph_channel",
    "keyword_query",
    "lexical_channel",
    "retrieve",
    "semantic_channel",
    "source_embedding",
    "taxonomy_channel",
    "union_candidates",
]

_ANALYTICS: Final = RetrievalChannel.ANALYTICS


@dataclass(frozen=True, slots=True)
class RetrievedCandidate:
    target_page_id: uuid.UUID
    channels: tuple[RetrievalChannel, ...]
    channel_scores: dict[str, float]
    attribution: tuple[CandidateAttribution, ...] = ()
    """One entry per channel in ``channels`` (raw, normalised, rank, evidence)."""
    signals: Mapping[str, float] = field(default_factory=dict)
    """The separate signals, by name (``semantic_similarity``, ``lexical_similarity``,
    ``topic_relevance``, ``graph_*``, ``gsc_opportunity``, ``ga4_engagement``,
    ``orphan_recovery_value``, ``destination_priority``). Never summed here."""
    graph_signals: GraphSignals | None = None
    """The TARGET's link-graph position when metrics exist (``page_graph_metrics``)."""

    @property
    def best(self) -> float:
        return max(self.channel_scores.values(), default=0.0)


# --------------------------------------------------------------------------- #
# union
# --------------------------------------------------------------------------- #
def union_candidates(
    found: Mapping[RetrievalChannel, Mapping[uuid.UUID, float]],
    *,
    source_page_id: uuid.UUID,
    cap: int,
    evidence: Mapping[RetrievalChannel, Mapping[uuid.UUID, Mapping[str, Any]]] | None = None,
    signals: Mapping[uuid.UUID, Mapping[str, float]] | None = None,
    support: Mapping[uuid.UUID, float] | None = None,
) -> list[RetrievedCandidate]:
    """Union per target (channels in declaration order); keep the best ``cap``.

    Channel scores live on different scales (cosine <= 1; ``ts_rank_cd`` often 3-8;
    relationship weights; 1/(1+inbound)), so ranking by the raw maximum would let the lexical
    channel dominate the cap. Each channel's scores are therefore normalised by that channel's
    best score for this source (scale-free, in (0, 1]) and candidates are ranked by the SUM of
    their normalised scores -- which also rewards agreement between channels -- then by target
    id. The stored ``channel_scores`` stay raw, for explainability.

    ANALYTICS contributes NOTHING to that sum (it may surface a destination, never justify
    one). A candidate found ONLY by analytics is ranked by its ``support`` -- the best
    non-analytics relevance the caller measured -- so the number that orders it is not an
    analytics number.
    """
    channels: dict[uuid.UUID, list[RetrievalChannel]] = {}
    scores: dict[uuid.UUID, dict[str, float]] = {}
    attribution: dict[uuid.UUID, list[CandidateAttribution]] = {}
    normalised: dict[uuid.UUID, float] = {}
    for channel in RetrievalChannel:
        hits = {t: float(v) for t, v in found.get(channel, {}).items() if t != source_page_id}
        top = max(hits.values(), default=0.0)
        order = sorted(hits, key=lambda t: (-hits[t], str(t)))
        for rank, target in enumerate(order, start=1):
            score = hits[target]
            channels.setdefault(target, []).append(channel)
            scores.setdefault(target, {})[channel.value] = score
            share = max(0.0, score / top) if top > 0 else 0.0
            attribution.setdefault(target, []).append(
                CandidateAttribution(
                    channel=channel,
                    raw=score,
                    normalised=min(1.0, share),
                    rank_in_channel=rank,
                    evidence=dict((evidence or {}).get(channel, {}).get(target, {})),
                )
            )
            if channel is not _ANALYTICS:
                normalised[target] = normalised.get(target, 0.0) + share
    for target, names in channels.items():
        if names == [_ANALYTICS]:
            normalised[target] = min(1.0, max(0.0, (support or {}).get(target, 0.0)))
    items = [
        RetrievedCandidate(
            target_page_id=t,
            channels=tuple(channels[t]),
            channel_scores=scores[t],
            attribution=tuple(attribution[t]),
            signals=dict((signals or {}).get(t, {})),
        )
        for t in channels
    ]
    items.sort(key=lambda c: (-normalised.get(c.target_page_id, 0.0), c.target_page_id))
    return items[:cap]


# --------------------------------------------------------------------------- #
# retrieve
# --------------------------------------------------------------------------- #
async def retrieve(
    session: AsyncSession,
    *,
    source_page_id: uuid.UUID,
    retrieval: RetrievalPolicy,
    embedding: EmbeddingKey | None,
    keywords: Sequence[str],
    industry_id: str | None,
    analysis_version: str,
    vector_store: VectorStore | None = None,
    semantic_floor: float | None = None,
    generators: Sequence[CandidateGenerator] | None = None,
) -> list[RetrievedCandidate]:
    """Every enabled channel for one source, unioned and capped (module docstring).

    ``vector_store`` is the shared index (built once per run); ``None`` falls back to an exact
    in-process index built from PostgreSQL per call. ``semantic_floor`` is the cosine an
    analytics-only candidate must reach to count as semantically supported (``None``: only a
    stored taxonomy relationship can support it).
    """
    enabled = set(retrieval.channels_enabled)
    ctx = GenerationContext(
        source_page_id=source_page_id,
        retrieval=retrieval,
        embedding=embedding,
        keywords=keywords,
        industry_id=industry_id,
        analysis_version=analysis_version,
        vector_store=vector_store,
    )
    results: dict[RetrievalChannel, list[GeneratedCandidate]] = {}
    for generator in generators if generators is not None else default_generators():
        if generator.channel in enabled:
            results[generator.channel] = await generator.generate(session, ctx)

    support: dict[uuid.UUID, float] = {}
    analytics = results.get(_ANALYTICS, [])
    if analytics:
        others = {
            c.target_page_id for ch, items in results.items() if ch is not _ANALYTICS for c in items
        }
        only = frozenset(c.target_page_id for c in analytics if c.target_page_id not in others)
        if only:
            support = await _support(
                session,
                source_page_id=source_page_id,
                targets=sorted(only, key=str),
                embedding=embedding,
                semantic_floor=semantic_floor,
            )
            blocked = {
                target for target, _ in AnalyticsSupportGate(only, support).blocking_reasons()
            }
            results[_ANALYTICS] = [c for c in analytics if c.target_page_id not in blocked]

    found: dict[RetrievalChannel, dict[uuid.UUID, float]] = {}
    evidence: dict[RetrievalChannel, dict[uuid.UUID, Mapping[str, Any]]] = {}
    signals: dict[uuid.UUID, dict[str, float]] = {}
    for channel, items in results.items():
        found[channel] = {c.target_page_id: c.raw for c in items}
        evidence[channel] = {c.target_page_id: c.evidence for c in items}
        for c in items:
            signals.setdefault(c.target_page_id, {}).update(c.signals)
    kept = union_candidates(
        found,
        source_page_id=source_page_id,
        cap=retrieval.max_candidates_per_source,
        evidence=evidence,
        signals=signals,
        support=support,
    )
    if not kept:
        return kept
    ids = [c.target_page_id for c in kept]
    priority = await destination_priorities(
        session, page_ids=ids, analysis_version=analysis_version
    )
    graph = await graph_repo.fetch_signals(session, page_ids=ids)
    return [
        RetrievedCandidate(
            target_page_id=c.target_page_id,
            channels=c.channels,
            channel_scores=c.channel_scores,
            attribution=c.attribution,
            signals={
                **c.signals,
                **(
                    {"destination_priority": priority[c.target_page_id]}
                    if c.target_page_id in priority
                    else {}
                ),
            },
            graph_signals=graph.get(c.target_page_id),
        )
        for c in kept
    ]


async def _support(
    session: AsyncSession,
    *,
    source_page_id: uuid.UUID,
    targets: Sequence[uuid.UUID],
    embedding: EmbeddingKey | None,
    semantic_floor: float | None,
) -> dict[uuid.UUID, float]:
    """Best NON-analytics relevance of each target: cosine at or above the floor, or a stored
    taxonomy weight. Zero means unsupported."""
    support: dict[uuid.UUID, float] = dict.fromkeys(targets, 0.0)
    for target, weight in (await taxonomy_channel(session, source_page_id=source_page_id)).items():
        if target in support and weight > 0.0:
            support[target] = max(support[target], min(1.0, weight))
    if embedding is not None and semantic_floor is not None:
        cosines = await cosine_to(
            session, source_page_id=source_page_id, target_page_ids=targets, key=embedding
        )
        for target, cosine in cosines.items():
            if target in support and cosine >= semantic_floor:
                support[target] = max(support[target], min(1.0, cosine))
    return support


async def cosine_to(
    session: AsyncSession,
    *,
    source_page_id: uuid.UUID,
    target_page_ids: Sequence[uuid.UUID],
    key: EmbeddingKey,
) -> dict[uuid.UUID, float]:
    """Cosine of the source's current vector to each target's (current identity; scoring).

    A pairwise computation over the ``real[]`` values PostgreSQL owns, via the one shared pure
    function; it is a score input for already-retrieved candidates, not a retrieval path.
    """
    if not target_page_ids:
        return {}
    source = await source_embedding(session, source_page_id=source_page_id, key=key)
    if source is None:
        return {}
    vector, dimensions = source
    rows = (
        await session.execute(
            select(PageEmbedding.page_id, PageEmbedding.embedding)
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
    for page_id, candidate in rows:
        score = cosine_similarity(vector, candidate)
        if score is not None and math.isfinite(score):
            out.setdefault(page_id, max(-1.0, min(1.0, score)))
    return out
