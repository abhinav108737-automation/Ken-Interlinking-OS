"""The analysis job (``JobKind.ANALYSIS``): analyses, protections, embeddings, relationships.

One run, through the generic job runner (``ken_services.jobs.runner``)::

    load taxonomy -> av = analysis_version(taxonomy.version)
    for every page in scope (gone ones too; eligibility handles gone):
        sync POLICY protections                              (own transaction)
        analyse the CURRENT version unless (version, av) exists  (own transaction)
    embed every current version lacking (model, recipe, fingerprint)  (one tx per batch)
    replace the capped taxonomy out-edges of every affected page  (one transaction)
    replace / re-evaluate semantic_similar edges             (one transaction)
    -> succeeded | completed_with_errors, job.completed

``S`` is the set of pages analysed in this run (with ``force``: every page in
scope with a fresh analysis). ``force`` never mutates an existing analysis or
embedding: it inserts only missing rows and recomputes protections and edges.

**Relationship replacement (ADR-0010 section 4).** Only the MATERIALISED
taxonomy types are stored (``relationships.MATERIALISED_RELATIONSHIP_TYPES``:
same / parent / sub / related market, supply chain, complementary). The coarse
types same_geography, regional_parent and industry_adjacency are skipped:
Phase 3 derives them per candidate pair. Each source page keeps at most
``settings.relationship_max_per_type`` edges per type: the top ones by
(weight desc, number of shared geographies desc, target page id asc).

Because of the cap, a page's out-edges depend on ALL its candidates, so a run
recomputes the full out-edge set of every AFFECTED source ``R``: ``S``, every
prefilter candidate of a page in ``S`` (its new relations), and every other
endpoint of a stored edge touching ``S`` (its old relations). Every taxonomy
edge touching ``S`` and every taxonomy out-edge of ``R`` is deleted, then each
fresh page in ``R`` is evaluated against its prefilter candidates.

The key-based prefilter (:func:`taxonomy_relationship_keys`) only pairs pages
that can produce a materialised type: the same market, parent / child markets,
sibling markets and declared supply-chain / complementary links. Cost is
O(|R| x |candidates|) evaluations and O(|R| x types x cap) stored edges.

**semantic_similar** edges (only when an embedding identity was resolved this
run; otherwise they are left untouched, and readers drop stale ones). Let
``C = S + pages embedded this run``:

1. delete every semantic edge touching ``C``; ``R = C + the other endpoints``;
2. for each c in ``C``, every page p outside ``R`` that c would now enter the
   top-k of (score above the threshold and p has fewer than k edges or c beats
   p's weakest edge) joins ``R``;
3. every page in ``R`` gets its top-k recomputed by exact cosine KNN over
   current-version embeddings of the same (model, dimensions, recipe version,
   recipe fingerprint) whose pages have a fresh analysis.

That is O((|C| + |R|) x candidates) vector comparisons, done in PostgreSQL.

Per-page failures never abort the run: they count ``errors`` (logged with a
class name only) and the run ends ``completed_with_errors``.

Counters: ``pages_considered``, ``pages_analyzed``, ``pages_fresh_skipped``,
``protections_added``, ``protections_deactivated``, ``embeddings_written``,
``embeddings_fresh_skipped``, ``embedding_failures``, ``relationships_written``,
``errors``. Succeeded iff ``errors == 0`` and ``embedding_failures == 0``.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.intelligence import (
    EMBEDDING_RECIPE_VERSION,
    PageAnalysis,
    ProtectionSource,
    RelationshipType,
    TaxonomyPolicy,
    analysis_version,
)
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail
from ken_contracts.model import ModelProvider
from ken_contracts.vectors import VectorStore
from ken_core.config import Settings
from ken_core.db.repositories import intelligence as repo
from ken_core.logging import get_logger
from ken_services.intelligence.analyze import AnalysisInput, analyze_page
from ken_services.intelligence.embeddings import (
    EmbeddingIdentity,
    EmbeddingRecipe,
    current_recipe_fingerprint,
    default_recipe,
    embed_stale_pages,
    embedding_identity,
)
from ken_services.intelligence.protection import ProtectionMatch, match_protections
from ken_services.intelligence.relationships import (
    MATERIALISED_RELATIONSHIP_TYPES,
    RelationshipCandidate,
    semantic_relationship,
    taxonomy_relationships,
)
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.jobs.runner import JobContext, run_job, utc_now
from ken_services.vectors.retrieval import PostgresSemanticRetriever

__all__ = [
    "ANALYSIS_COUNTERS",
    "POLICY_ACTOR",
    "AnalysisToolkit",
    "default_toolkit",
    "load_policy",
    "run_analysis",
    "taxonomy_relationship_keys",
]

logger = get_logger("ken_services.intelligence.pipeline")

ANALYSIS_COUNTERS: Final = (
    "pages_considered",
    "pages_analyzed",
    "pages_fresh_skipped",
    "protections_added",
    "protections_deactivated",
    "embeddings_written",
    "embeddings_fresh_skipped",
    "embedding_failures",
    "relationships_written",
    "errors",
)
POLICY_ACTOR: Final = "policy"
"""``created_by`` / ``deactivated_by`` of every protection this job manages."""

_SEMANTIC: Final = RelationshipType.SEMANTIC_SIMILAR.value
_MATERIALISED: Final = frozenset(kind.value for kind in MATERIALISED_RELATIONSHIP_TYPES)
_STORE_SEARCH_CAP: Final = 500
"""The most neighbours one vector-store search returns (``MAX_SEARCH_LIMIT``, ADR-0017)."""

KeysFn = Callable[[PageAnalysis, TaxonomyPolicy], tuple[frozenset[str], frozenset[str]]]


# --------------------------------------------------------------------------- #
# Relationship prefilter
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Tree:
    parent: dict[str, str | None]
    declared: dict[str, frozenset[str]]


_TREE_CACHE: list[tuple[TaxonomyPolicy, _Tree]] = []


def _tree(taxonomy: TaxonomyPolicy) -> _Tree:
    for policy, tree in _TREE_CACHE:
        if policy is taxonomy:
            return tree
    tree = _Tree(
        parent={m.id: m.parent for m in taxonomy.markets},
        declared={
            m.id: frozenset((*m.upstream, *m.downstream, *m.complementary))
            for m in taxonomy.markets
        },
    )
    _TREE_CACHE[:] = [*_TREE_CACHE[-3:], (taxonomy, tree)]
    return tree


def taxonomy_relationship_keys(
    analysis: PageAnalysis, taxonomy: TaxonomyPolicy
) -> tuple[frozenset[str], frozenset[str]]:
    """(what the page IS, what it RELATES TO) keys for the pair prefilter.

    Pair (s, t) is evaluated iff ``Q(s) & E(t)`` or ``Q(t) & E(s)``. The keys
    cover exactly the MATERIALISED types of
    ``ken_services.intelligence.relationships`` (all need both markets known):
    ``m:<market>`` pairs the same market, a parent with its children and
    declared supply-chain / complementary links; ``c:<parent>`` pairs siblings.
    Geography and industry adjacency are deliberately absent: those types are
    never stored. A unit test checks the prefilter against the exhaustive
    pairing on the real taxonomy.
    """
    tree = _tree(taxonomy)
    market = analysis.market_id
    if market is None or market not in tree.parent:
        return frozenset(), frozenset()
    is_keys = {f"m:{market}"}
    relates = {f"m:{market}"}  # same_market
    relates |= {f"m:{other}" for other in tree.declared[market]}  # supply chain, complementary
    parent = tree.parent[market]
    if parent is not None:
        relates.add(f"m:{parent}")  # parent_market (submarket, from the other side)
        is_keys.add(f"c:{parent}")  # related_market: siblings share a parent
        relates.add(f"c:{parent}")
    return frozenset(is_keys), frozenset(relates)


def _top_edges(
    edges: Sequence[repo.NewRelationship],
    geographies: Mapping[uuid.UUID, frozenset[str]],
    *,
    cap: int,
) -> list[repo.NewRelationship]:
    """The ``cap`` best edges of ONE (source, type): weight desc, shared geographies desc,
    target page id asc. Deterministic (a total order)."""

    def rank(edge: repo.NewRelationship) -> tuple[float, int, uuid.UUID]:
        shared = geographies[edge.source_page_id] & geographies[edge.target_page_id]
        return (-edge.weight, -len(shared), edge.target_page_id)

    return sorted(edges, key=rank)[:cap]


# --------------------------------------------------------------------------- #
# Injectable analysis functions (workstream A's modules by default)
# --------------------------------------------------------------------------- #
class AnalyzeFn(Protocol):
    def __call__(self, item: AnalysisInput, *, taxonomy: TaxonomyPolicy) -> PageAnalysis: ...


class MatchProtectionsFn(Protocol):
    def __call__(
        self, *, url: str, metadata: Mapping[str, str], taxonomy: TaxonomyPolicy
    ) -> Sequence[ProtectionMatch]: ...


class TaxonomyRelationshipsFn(Protocol):
    def __call__(
        self, source: PageAnalysis, target: PageAnalysis, *, taxonomy: TaxonomyPolicy
    ) -> Sequence[RelationshipCandidate]: ...


class SemanticRelationshipFn(Protocol):
    def __call__(self, score: float, *, min_score: float) -> RelationshipCandidate | None: ...


@dataclass(frozen=True, slots=True)
class AnalysisToolkit:
    """The deterministic functions the job calls (statically typed). Tests inject fakes."""

    analyze: AnalyzeFn
    make_input: type[AnalysisInput]
    match_protections: MatchProtectionsFn
    taxonomy_relationships: TaxonomyRelationshipsFn
    semantic_relationship: SemanticRelationshipFn
    recipe: EmbeddingRecipe
    relationship_keys: KeysFn = field(default=taxonomy_relationship_keys)


def default_toolkit() -> AnalysisToolkit:
    return AnalysisToolkit(
        analyze=analyze_page,
        make_input=AnalysisInput,
        match_protections=match_protections,
        taxonomy_relationships=taxonomy_relationships,
        semantic_relationship=semantic_relationship,
        recipe=default_recipe(),
    )


def load_policy(path: str) -> TaxonomyPolicy:
    return load_taxonomy(path)


# --------------------------------------------------------------------------- #
# The job
# --------------------------------------------------------------------------- #
class _Analysis:
    def __init__(
        self,
        *,
        job: JobContext,
        settings: Settings,
        provider: ModelProvider,
        taxonomy: TaxonomyPolicy,
        toolkit: AnalysisToolkit,
        force: bool,
        embeddings: bool,
        page_ids: Sequence[uuid.UUID] | None,
        vector_store: VectorStore | None = None,
    ) -> None:
        self.job = job
        self.settings = settings
        self.provider = provider
        self.taxonomy = taxonomy
        self.toolkit = toolkit
        self.force = force
        self.embeddings = embeddings
        self.page_ids = page_ids
        self.av = analysis_version(taxonomy.version)
        self.fingerprint = current_recipe_fingerprint(settings)
        # ADR-0026: a vector store makes the semantic step sub-linear per page. None (the
        # default, ``KEN_VECTOR_STORE=memory``) keeps the exact scan over PostgreSQL.
        self._store = vector_store
        self._retriever: PostgresSemanticRetriever | None = None

    def _error(self, event: str, exc: BaseException, **fields: Any) -> None:
        logger.error(event, run_id=str(self.job.run_id), error_class=type(exc).__name__, **fields)
        self.job.increment("errors")

    async def run(self) -> RunStatus:
        job = self.job
        async with job.sessionmaker() as session:
            pages = await repo.list_pages(session, page_ids=self.page_ids)
            have = await repo.analysed_versions(
                session, analysis_version=self.av, page_ids=[p.id for p in pages]
            )
        job.increment("pages_considered", len(pages))
        analysed: set[uuid.UUID] = set()
        for page in pages:
            try:
                await self._sync_protections(page)
            except Exception as exc:
                self._error("protection_sync_failed", exc, page_id=str(page.id))
            if (page.id, page.current_version) in have:
                job.increment("pages_fresh_skipped")
                continue
            try:
                if await self._analyse(page):
                    analysed.add(page.id)
            except Exception as exc:
                self._error("page_analysis_failed", exc, page_id=str(page.id))
        await job.flush()

        identity, embedded = await self._embed()
        changed = {p.id for p in pages} if self.force else analysed
        await self._taxonomy_edges(changed)
        if identity is not None:
            await self._semantic_edges(identity, changed | embedded)
        await job.flush()
        counters = job.counters
        clean = counters.get("errors", 0) == 0 and counters.get("embedding_failures", 0) == 0
        return RunStatus.SUCCEEDED if clean else RunStatus.COMPLETED_WITH_ERRORS

    # ------------------------------------------------------- protections
    async def _sync_protections(self, page: repo.PageRef) -> None:
        async with self.job.sessionmaker() as session:
            content = await repo.load_version_content(
                session, page_id=page.id, version=page.current_version
            )
        if content is None:  # pragma: no cover - the deferred FK makes this impossible
            raise LookupError("current version missing")
        matches = self.toolkit.match_protections(
            url=page.canonical_url, metadata=content.metadata, taxonomy=self.taxonomy
        )
        wanted: dict[str | None, str] = {}
        for match in matches:
            if ProtectionSource(match.source) is ProtectionSource.POLICY:
                wanted.setdefault(match.rule_id, match.reason)
        policy = ProtectionSource.POLICY.value
        async with self.job.transaction() as tx:
            now = self.job.clock()
            active = await repo.active_protections(tx.session, page_id=page.id)
            present: set[str | None] = set()
            for row in active:
                if row.source != policy:
                    continue  # MANUAL and CMS protections are never touched here
                if row.rule_id in wanted:
                    present.add(row.rule_id)
                    continue
                await repo.deactivate_protection(
                    tx.session, protection_id=row.id, deactivated_by=POLICY_ACTOR, now=now
                )
                tx.increment("protections_deactivated")
            for rule_id, reason in wanted.items():
                if rule_id in present:
                    continue
                if await repo.insert_protection(
                    tx.session,
                    page_id=page.id,
                    source=policy,
                    rule_id=rule_id,
                    reason=reason,
                    created_by=POLICY_ACTOR,
                    created_at=now,
                ):
                    tx.increment("protections_added")

    # ---------------------------------------------------------- analysis
    async def _analyse(self, page: repo.PageRef) -> bool:
        async with self.job.sessionmaker() as session:
            content = await repo.load_version_content(
                session, page_id=page.id, version=page.current_version
            )
        if content is None:  # pragma: no cover - the deferred FK makes this impossible
            raise LookupError("current version missing")
        item = self.toolkit.make_input(
            page_id=content.page_id,
            page_version=content.version,
            url=content.url,
            title=content.title,
            meta_description=content.meta_description,
            headings=content.headings,
            body_html=content.body_html,
            body_text=content.body_text,
            metadata=content.metadata,
        )
        result = self.toolkit.analyze(item, taxonomy=self.taxonomy)
        # The service boundary: re-validate, and refuse an analysis of anything else.
        analysis = PageAnalysis.model_validate(result.model_dump(mode="json"))
        if (analysis.page_id, analysis.page_version, analysis.analysis_version) != (
            page.id,
            page.current_version,
            self.av,
        ):
            raise ValueError("the analysis does not describe the page version analysed")
        async with self.job.transaction() as tx:
            inserted = await repo.insert_analysis(
                tx.session, analysis=analysis, run_id=self.job.run_id, created_at=self.job.clock()
            )
            tx.increment("pages_analyzed" if inserted else "pages_fresh_skipped")
        return inserted

    # -------------------------------------------------------- embeddings
    async def _embed(self) -> tuple[EmbeddingIdentity | None, set[uuid.UUID]]:
        if not self.embeddings:
            return None, set()
        try:
            identity = await embedding_identity(self.provider)
        except Exception as exc:
            logger.warning("embedding_identity_unavailable", error_class=type(exc).__name__)
            self.job.increment("embedding_failures")
            await self.job.flush()
            return None, set()
        embedded = await embed_stale_pages(
            job=self.job,
            provider=self.provider,
            identity=identity,
            settings=self.settings,
            recipe=self.toolkit.recipe,
            page_ids=self.page_ids,
        )
        return identity, embedded

    # ----------------------------------------------------- relationships
    async def _taxonomy_edges(self, pages: set[uuid.UUID]) -> None:
        """Recompute the capped taxonomy out-edges of every page affected by ``pages``."""
        try:
            async with self.job.transaction() as tx:
                fresh = await repo.fresh_analyses(tx.session, analysis_version=self.av)
                changed = pages & fresh.keys()
                if not changed:
                    return
                related = self._prefilter(fresh)
                sources = set(changed) | await repo.delete_relationships_touching(
                    tx.session, page_ids=changed, semantic=False
                )
                for page_id in changed:
                    sources |= related(page_id)
                await repo.delete_outgoing_relationships(
                    tx.session, page_ids=sources - changed, semantic=False
                )
                edges = self._taxonomy_pairs(sources & fresh.keys(), fresh, related)
                tx.increment(
                    "relationships_written",
                    await repo.insert_relationships(
                        tx.session,
                        relationships=edges,
                        analysis_version=self.av,
                        run_id=self.job.run_id,
                        computed_at=self.job.clock(),
                    ),
                )
        except Exception as exc:
            self._error("taxonomy_relationships_failed", exc)

    def _prefilter(
        self, fresh: dict[uuid.UUID, repo.FreshAnalysis]
    ) -> Callable[[uuid.UUID], set[uuid.UUID]]:
        """page id -> the fresh pages the prefilter pairs it with (never itself)."""
        keys = {
            pid: self.toolkit.relationship_keys(item.analysis, self.taxonomy)
            for pid, item in fresh.items()
        }
        is_index: dict[str, set[uuid.UUID]] = defaultdict(set)
        relates_index: dict[str, set[uuid.UUID]] = defaultdict(set)
        for pid, (is_keys, relates) in keys.items():
            for key in is_keys:
                is_index[key].add(pid)
            for key in relates:
                relates_index[key].add(pid)

        def related(page_id: uuid.UUID) -> set[uuid.UUID]:
            is_keys, relates = keys[page_id]
            others: set[uuid.UUID] = set()
            for key in relates:
                others |= is_index.get(key, set())
            for key in is_keys:
                others |= relates_index.get(key, set())
            others.discard(page_id)
            return others

        return related

    def _taxonomy_pairs(
        self,
        sources: set[uuid.UUID],
        fresh: dict[uuid.UUID, repo.FreshAnalysis],
        related: Callable[[uuid.UUID], set[uuid.UUID]],
    ) -> list[repo.NewRelationship]:
        """The full, capped set of materialised out-edges of each page in ``sources``."""
        cap = self.settings.relationship_max_per_type
        geographies = {
            pid: frozenset(g.geo_id for g in item.analysis.geographies)
            for pid, item in fresh.items()
        }
        out: list[repo.NewRelationship] = []
        for src in sorted(sources):
            best: dict[tuple[str, uuid.UUID], repo.NewRelationship] = {}
            for tgt in sorted(related(src)):
                for candidate in self.toolkit.taxonomy_relationships(
                    fresh[src].analysis, fresh[tgt].analysis, taxonomy=self.taxonomy
                ):
                    kind = RelationshipType(candidate.relationship_type).value
                    if kind not in _MATERIALISED:
                        continue  # semantic and the derived-only coarse types
                    edge = (kind, tgt)
                    if edge in best and best[edge].weight >= candidate.weight:
                        continue
                    best[edge] = repo.NewRelationship(
                        source_page_id=src,
                        source_page_version=fresh[src].page_version,
                        target_page_id=tgt,
                        target_page_version=fresh[tgt].page_version,
                        relationship_type=kind,
                        weight=float(candidate.weight),
                        evidence=dict(candidate.evidence),
                    )
            by_kind: dict[str, list[repo.NewRelationship]] = defaultdict(list)
            for (kind, _), item in best.items():
                by_kind[kind].append(item)
            for items in by_kind.values():
                out.extend(_top_edges(items, geographies, cap=cap))
        out.sort(key=lambda e: (e.source_page_id, e.target_page_id, e.relationship_type))
        return out

    async def _semantic_edges(self, identity: EmbeddingIdentity, changed: set[uuid.UUID]) -> None:
        """Semantic edges, through the vector store when one is configured (ADR-0026)."""
        from ken_core.providers.vectors.factory import build_vector_store

        store = self._store
        owned = False
        if store is None and self.settings.vector_store == "qdrant":
            store = build_vector_store(self.settings)
            owned = True
        try:
            if store is not None:
                try:
                    await self._attach_store(store, identity)
                except Exception as exc:  # isolated: taxonomy edges are already written
                    self._error("semantic_index_failed", exc)
                    return
            await self._semantic_edges_exact_or_store(identity, changed)
        finally:
            self._retriever = None
            if owned and store is not None:
                await store.aclose()

    async def _attach_store(self, store: VectorStore, identity: EmbeddingIdentity) -> None:
        """Bring the derived index up to date with PostgreSQL, then search through it."""
        from ken_contracts.intelligence import EMBEDDING_RECIPE_VERSION as RECIPE
        from ken_services.vectors.identity import VectorIdentity
        from ken_services.vectors.index import PostgresVectorIndexer
        from ken_services.vectors.retrieval import PostgresSemanticRetriever

        vid = VectorIdentity(
            model=identity.model, recipe_version=RECIPE, recipe_fingerprint=self.fingerprint
        )
        sessionmaker = self.job.sessionmaker
        await PostgresVectorIndexer(
            sessionmaker=sessionmaker, store=store, settings=self.settings, identity=vid
        ).index()
        self._retriever = PostgresSemanticRetriever(
            sessionmaker=sessionmaker, store=store, identity=vid
        )

    async def _semantic_edges_exact_or_store(
        self, identity: EmbeddingIdentity, changed: set[uuid.UUID]
    ) -> None:
        top_k = self.settings.similarity_top_k
        min_score = self.settings.similarity_min_score
        try:
            async with self.job.transaction() as tx:
                session = tx.session
                # Edges made by another model or recipe are stale even when no page
                # changed (e.g. switching back to a previous recipe re-embeds nothing):
                # their sources are recomputed under the current identity (QA re-review).
                changed = set(changed) | await repo.semantic_sources_from_other_embeddings(
                    session, model=identity.model, recipe_fingerprint=self.fingerprint
                )
                if not changed:
                    return
                candidates = await repo.similarity_candidates(
                    session,
                    model=identity.model,
                    recipe_version=EMBEDDING_RECIPE_VERSION,
                    recipe_fingerprint=self.fingerprint,
                    analysis_version=self.av,
                )
                recompute = set(changed) | await repo.delete_relationships_touching(
                    session, page_ids=changed, semantic=True
                )
                stats = await repo.semantic_out_stats(
                    session, page_ids=candidates.keys() - recompute
                )
                for page_id in sorted(changed & candidates.keys()):
                    entry = candidates[page_id]
                    for other, _version, score in await self._nearest(
                        session,
                        identity,
                        entry,
                        limit=None,
                        min_score=min_score,
                        allowed=candidates,
                    ):
                        if other in recompute:
                            continue
                        candidate = self.toolkit.semantic_relationship(score, min_score=min_score)
                        if candidate is None:
                            continue
                        degree, weakest = stats.get(other, (0, math.inf))
                        if degree < top_k or candidate.weight >= weakest:
                            recompute.add(other)
                await repo.delete_outgoing_semantic(session, page_ids=recompute)
                edges: list[repo.NewRelationship] = []
                for page_id in sorted(recompute & candidates.keys()):
                    entry = candidates[page_id]
                    for target, version, score in await self._nearest(
                        session,
                        identity,
                        entry,
                        limit=top_k,
                        min_score=None,
                        allowed=candidates,
                    ):
                        candidate = self.toolkit.semantic_relationship(score, min_score=min_score)
                        if candidate is None:
                            continue
                        edges.append(
                            repo.NewRelationship(
                                source_page_id=page_id,
                                source_page_version=entry.page_version,
                                target_page_id=target,
                                target_page_version=version,
                                relationship_type=_SEMANTIC,
                                weight=float(candidate.weight),
                                evidence={
                                    **candidate.evidence,
                                    "model": identity.model,
                                    "recipe_fingerprint": self.fingerprint,
                                },
                            )
                        )
                tx.increment(
                    "relationships_written",
                    await repo.insert_relationships(
                        session,
                        relationships=edges,
                        analysis_version=self.av,
                        run_id=self.job.run_id,
                        computed_at=self.job.clock(),
                    ),
                )
        except Exception as exc:
            self._error("semantic_relationships_failed", exc)

    async def _nearest(
        self,
        session: AsyncSession,
        identity: EmbeddingIdentity,
        entry: repo.EmbeddedPage,
        *,
        limit: int | None,
        min_score: float | None,
        allowed: Mapping[uuid.UUID, Any] | None = None,
    ) -> list[tuple[uuid.UUID, int, float]]:
        if self._retriever is not None:
            return await self._nearest_from_store(
                entry, limit=limit, min_score=min_score, allowed=allowed
            )
        return await repo.nearest_candidates(
            session,
            source_embedding_id=entry.embedding_id,
            source_page_id=entry.page_id,
            model=identity.model,
            dimensions=entry.dimensions,
            recipe_version=EMBEDDING_RECIPE_VERSION,
            recipe_fingerprint=self.fingerprint,
            analysis_version=self.av,
            limit=limit,
            min_score=min_score,
        )

    async def _nearest_from_store(
        self,
        entry: repo.EmbeddedPage,
        *,
        limit: int | None,
        min_score: float | None,
        allowed: Mapping[uuid.UUID, Any] | None,
    ) -> list[tuple[uuid.UUID, int, float]]:
        """The same answer as :func:`repo.nearest_candidates`, from the vector store.

        Best first, ties by page id (so a run is reproducible), self excluded, stale hits
        dropped by the retriever, and only pages that are valid candidates for this analysis
        version (``allowed``). With ``limit=None`` the store's search cap
        (``_STORE_SEARCH_CAP``) bounds the answer: that only matters to the incremental
        "whose top-k does this page enter" probe, and a full recompute is unaffected.
        """
        assert self._retriever is not None  # noqa: S101 - guarded by the caller
        cap = _STORE_SEARCH_CAP if limit is None else min(_STORE_SEARCH_CAP, limit * 2)
        hits = await self._retriever.similar_to_page(
            page_id=entry.page_id, limit=cap, score_threshold=min_score
        )
        rows = [
            (hit.page_id, int(hit.page_version_id.rsplit(":", 1)[1]), hit.score)
            for hit in hits
            if allowed is None or hit.page_id in allowed
        ]
        rows.sort(key=lambda r: (-r[2], str(r[0])))
        return rows if limit is None else rows[:limit]


def _snapshot(
    *,
    settings: Settings,
    provider: ModelProvider,
    taxonomy: TaxonomyPolicy,
    force: bool,
    embeddings: bool,
    page_ids: Sequence[uuid.UUID] | None,
) -> dict[str, Any]:
    """Secret-free: versions, switches and numeric settings only (no URLs, no paths)."""
    return {
        "analysis_version": analysis_version(taxonomy.version),
        "taxonomy": {
            "version": taxonomy.version,
            "status": taxonomy.status,
            "source": taxonomy.source,
        },
        "force": force,
        "embeddings": embeddings,
        "page_ids": None if page_ids is None else sorted(str(p) for p in page_ids),
        "embedding": {
            "provider": provider.name,
            "recipe_version": EMBEDDING_RECIPE_VERSION,
            "recipe_fingerprint": current_recipe_fingerprint(settings),
            "batch_size": settings.embedding_batch_size,
            "max_chars": settings.embedding_max_chars,
        },
        "similarity": {
            "top_k": settings.similarity_top_k,
            "min_score": settings.similarity_min_score,
        },
        "relationships": {
            "max_per_type": settings.relationship_max_per_type,
            "materialised_types": sorted(_MATERIALISED),
        },
    }


async def run_analysis(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    model_provider: ModelProvider,
    triggered_by: str,
    force: bool = False,
    embeddings: bool = True,
    page_ids: Sequence[uuid.UUID] | None = None,
    analyzer: Callable[..., PageAnalysis] | None = None,
    taxonomy: TaxonomyPolicy | None = None,
    toolkit: AnalysisToolkit | None = None,
    vector_store: VectorStore | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> JobRunDetail:
    """Run one analysis job and return the finished run (see the module docstring).

    ``analyzer`` / ``taxonomy`` / ``toolkit`` default to the deterministic
    workstream modules and the configured policy file; tests inject fakes.

    Raises:
        JobAlreadyRunningError: another analysis run is in progress (nothing written).
        FileNotFoundError / ValueError: the taxonomy policy cannot be loaded.
    """
    policy = taxonomy if taxonomy is not None else load_policy(settings.taxonomy_path)
    tools = toolkit if toolkit is not None else default_toolkit()
    if analyzer is not None:
        tools = AnalysisToolkit(
            analyze=analyzer,
            make_input=tools.make_input,
            match_protections=tools.match_protections,
            taxonomy_relationships=tools.taxonomy_relationships,
            semantic_relationship=tools.semantic_relationship,
            recipe=tools.recipe,
            relationship_keys=tools.relationship_keys,
        )
    ids = None if page_ids is None else list(dict.fromkeys(page_ids))

    async def body(job: JobContext) -> RunStatus:
        return await _Analysis(
            job=job,
            settings=settings,
            provider=model_provider,
            taxonomy=policy,
            toolkit=tools,
            force=force,
            embeddings=embeddings,
            page_ids=ids,
            vector_store=vector_store,
        ).run()

    return await run_job(
        kind=JobKind.ANALYSIS,
        sessionmaker=sessionmaker,
        triggered_by=triggered_by,
        body=body,
        config_snapshot=_snapshot(
            settings=settings,
            provider=model_provider,
            taxonomy=policy,
            force=force,
            embeddings=embeddings,
            page_ids=ids,
        ),
        counter_names=ANALYSIS_COUNTERS,
        clock=clock,
    )
