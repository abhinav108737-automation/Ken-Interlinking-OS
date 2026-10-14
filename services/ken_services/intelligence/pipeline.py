"""The analysis job (``JobKind.ANALYSIS``): analyses, protections, embeddings, relationships.

One run, through the generic job runner (``ken_services.jobs.runner``)::

    load taxonomy -> av = analysis_version(taxonomy.version)
    for every page in scope (gone ones too; eligibility handles gone):
        sync POLICY protections                              (own transaction)
        analyse the CURRENT version unless (version, av) exists  (own transaction)
    embed every current version lacking (identity.model, recipe)  (one tx per batch)
    replace taxonomy edges touching S                        (one transaction)
    replace / re-evaluate semantic_similar edges             (one transaction)
    -> succeeded | completed_with_errors, job.completed

``S`` is the set of pages analysed in this run (with ``force``: every page in
scope with a fresh analysis). ``force`` never mutates an existing analysis or
embedding: it inserts only missing rows and recomputes protections and edges.

**Relationship replacement (ADR-0010 section 4).** Taxonomy edges: every edge
with source OR target in ``S`` is deleted, then every pair (s, t) with s in
``S`` and t any page with a fresh analysis is evaluated in BOTH directions. A
key-based prefilter (:func:`taxonomy_relationship_keys`) skips pairs that
cannot relate: same industry root, declared supply-chain / complementary /
adjacency links, shared countries and country-to-region containment. Cost is
O(|S| x |candidates|) evaluations; O(n^2) only on a forced run over pages that
all share keys.

**semantic_similar** edges (only when an embedding identity was resolved this
run; otherwise they are left untouched, and readers drop stale ones). Let
``C = S + pages embedded this run``:

1. delete every semantic edge touching ``C``; ``R = C + the other endpoints``;
2. for each c in ``C``, every page p outside ``R`` that c would now enter the
   top-k of (score above the threshold and p has fewer than k edges or c beats
   p's weakest edge) joins ``R``;
3. every page in ``R`` gets its top-k recomputed by exact cosine KNN over
   current-version embeddings of the same (model, dimensions, recipe) whose
   pages have a fresh analysis.

That is O((|C| + |R|) x candidates) vector comparisons, done in PostgreSQL.

Per-page failures never abort the run: they count ``errors`` (logged with a
class name only) and the run ends ``completed_with_errors``.

Counters: ``pages_considered``, ``pages_analyzed``, ``pages_fresh_skipped``,
``protections_added``, ``protections_deactivated``, ``embeddings_written``,
``embeddings_fresh_skipped``, ``embedding_failures``, ``relationships_written``,
``errors``. Succeeded iff ``errors == 0`` and ``embedding_failures == 0``.
"""

from __future__ import annotations

import importlib
import math
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

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
from ken_core.config import Settings
from ken_core.db.repositories import intelligence as repo
from ken_core.logging import get_logger
from ken_services.intelligence.embeddings import (
    EmbeddingIdentity,
    EmbeddingRecipe,
    default_recipe,
    embed_stale_pages,
    embedding_identity,
)
from ken_services.jobs.runner import JobContext, run_job, utc_now

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

KeysFn = Callable[[PageAnalysis, TaxonomyPolicy], tuple[frozenset[str], frozenset[str]]]


# --------------------------------------------------------------------------- #
# Relationship prefilter
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Tree:
    market_root: dict[str, str]
    declared: dict[str, frozenset[str]]
    adjacent: dict[str, frozenset[str]]
    geo_ancestors: dict[str, tuple[str, ...]]


_TREE_CACHE: list[tuple[TaxonomyPolicy, _Tree]] = []


def _tree(taxonomy: TaxonomyPolicy) -> _Tree:
    for policy, tree in _TREE_CACHE:
        if policy is taxonomy:
            return tree
    parents = {m.id: m.parent for m in taxonomy.markets}
    roots: dict[str, str] = {}
    for market in taxonomy.markets:
        node, seen = market.id, set()
        while parents.get(node) is not None and node not in seen:
            seen.add(node)
            node = parents[node] or node
        roots[market.id] = node
    geo_parents = {g.id: g.parent for g in taxonomy.geographies}
    ancestors: dict[str, tuple[str, ...]] = {}
    for geo in taxonomy.geographies:
        chain: list[str] = []
        parent = geo.parent
        while parent is not None and parent not in chain:
            chain.append(parent)
            parent = geo_parents.get(parent)
        ancestors[geo.id] = tuple(chain)
    tree = _Tree(
        market_root=roots,
        declared={
            m.id: frozenset((*m.upstream, *m.downstream, *m.complementary))
            for m in taxonomy.markets
        },
        adjacent={m.id: frozenset(m.adjacent) for m in taxonomy.markets},
        geo_ancestors=ancestors,
    )
    _TREE_CACHE[:] = [*_TREE_CACHE[-3:], (taxonomy, tree)]
    return tree


def taxonomy_relationship_keys(
    analysis: PageAnalysis, taxonomy: TaxonomyPolicy
) -> tuple[frozenset[str], frozenset[str]]:
    """(what the page IS, what it RELATES TO) keys for the pair prefilter.

    Pair (s, t) is evaluated iff ``Q(s) & E(t)`` or ``Q(t) & E(s)``. The keys
    mirror ``ken_services.intelligence.relationships``: market relationships
    need a shared industry root; supply-chain / complementary links a declared
    market link; industry adjacency a declared industry link; geography a
    shared country or a country inside a listed region. A unit test checks the
    prefilter against the exhaustive pairing on the real taxonomy.
    """
    tree = _tree(taxonomy)
    is_keys: set[str] = set()
    relates: set[str] = set()
    market = analysis.market_id
    if market is not None and market in tree.market_root:
        root = tree.market_root[market]
        is_keys |= {f"root:{root}", f"m:{market}"}
        relates.add(f"root:{root}")
        relates |= {f"m:{other}" for other in tree.declared[market]}
    industry = analysis.industry_id
    if industry is not None and industry in tree.adjacent:
        is_keys.add(f"i:{industry}")
        relates |= {f"i:{other}" for other in tree.adjacent[industry]}
    for geo in analysis.geographies:
        if geo.kind == "country":
            is_keys.add(f"c:{geo.geo_id}")
            relates.add(f"c:{geo.geo_id}")
            relates |= {f"r:{a}" for a in tree.geo_ancestors.get(geo.geo_id, ())}
        elif geo.kind == "region":
            is_keys.add(f"r:{geo.geo_id}")
    return frozenset(is_keys), frozenset(relates)


# --------------------------------------------------------------------------- #
# Injectable analysis functions (workstream A's modules by default)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AnalysisToolkit:
    """The deterministic functions the job calls. Tests inject fakes."""

    analyze: Callable[..., PageAnalysis]
    """``analyze(item, *, taxonomy) -> PageAnalysis``."""
    make_input: Callable[..., Any]
    """``make_input(page_id=, page_version=, url=, title=, ...) -> AnalysisInput``."""
    match_protections: Callable[..., Sequence[Any]]
    """``match_protections(*, url, metadata, taxonomy) -> (ProtectionMatch, ...)``."""
    taxonomy_relationships: Callable[..., Sequence[Any]]
    """``taxonomy_relationships(src, tgt, *, taxonomy) -> (RelationshipCandidate, ...)``."""
    semantic_relationship: Callable[..., Any]
    """``semantic_relationship(score, *, min_score) -> RelationshipCandidate | None``."""
    recipe: EmbeddingRecipe
    relationship_keys: KeysFn = field(default=taxonomy_relationship_keys)


def default_toolkit() -> AnalysisToolkit:
    def module(name: str) -> Any:
        return importlib.import_module(f"ken_services.intelligence.{name}")

    analyze, protection, relationships = (
        module("analyze"),
        module("protection"),
        module("relationships"),
    )
    return AnalysisToolkit(
        analyze=analyze.analyze_page,
        make_input=analyze.AnalysisInput,
        match_protections=protection.match_protections,
        taxonomy_relationships=relationships.taxonomy_relationships,
        semantic_relationship=relationships.semantic_relationship,
        recipe=default_recipe(),
    )


def load_policy(path: str) -> TaxonomyPolicy:
    module: Any = importlib.import_module("ken_services.intelligence.taxonomy")
    policy: TaxonomyPolicy = module.load_taxonomy(path)
    return policy


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
        """Replace every taxonomy edge touching ``pages`` (those with a fresh analysis)."""
        try:
            async with self.job.transaction() as tx:
                fresh = await repo.fresh_analyses(tx.session, analysis_version=self.av)
                changed = pages & fresh.keys()
                if not changed:
                    return
                await repo.delete_relationships_touching(
                    tx.session, page_ids=changed, semantic=False
                )
                edges = self._taxonomy_pairs(changed, fresh)
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

    def _taxonomy_pairs(
        self, changed: set[uuid.UUID], fresh: dict[uuid.UUID, repo.FreshAnalysis]
    ) -> list[repo.NewRelationship]:
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
        pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
        for source in changed:
            is_keys, relates = keys[source]
            others: set[uuid.UUID] = set()
            for key in relates:
                others |= is_index.get(key, set())
            for key in is_keys:
                others |= relates_index.get(key, set())
            others.discard(source)
            pairs |= {(min(source, other), max(source, other)) for other in others}
        best: dict[tuple[uuid.UUID, uuid.UUID, str], repo.NewRelationship] = {}
        for first, second in sorted(pairs):
            for src, tgt in ((first, second), (second, first)):
                for candidate in self.toolkit.taxonomy_relationships(
                    fresh[src].analysis, fresh[tgt].analysis, taxonomy=self.taxonomy
                ):
                    kind = RelationshipType(candidate.relationship_type).value
                    if kind == _SEMANTIC:
                        continue
                    edge = (src, tgt, kind)
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
        return [best[edge] for edge in sorted(best)]

    async def _semantic_edges(self, identity: EmbeddingIdentity, changed: set[uuid.UUID]) -> None:
        if not changed:
            return
        top_k = self.settings.similarity_top_k
        min_score = self.settings.similarity_min_score
        try:
            async with self.job.transaction() as tx:
                session = tx.session
                candidates = await repo.similarity_candidates(
                    session,
                    model=identity.model,
                    recipe_version=EMBEDDING_RECIPE_VERSION,
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
                        session, identity, entry, limit=None, min_score=min_score
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
                        session, identity, entry, limit=top_k, min_score=None
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
                                evidence=dict(candidate.evidence),
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
    ) -> list[tuple[uuid.UUID, int, float]]:
        return await repo.nearest_candidates(
            session,
            source_embedding_id=entry.embedding_id,
            source_page_id=entry.page_id,
            model=identity.model,
            dimensions=entry.dimensions,
            recipe_version=EMBEDDING_RECIPE_VERSION,
            analysis_version=self.av,
            limit=limit,
            min_score=min_score,
        )


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
            "batch_size": settings.embedding_batch_size,
            "max_chars": settings.embedding_max_chars,
        },
        "similarity": {
            "top_k": settings.similarity_top_k,
            "min_score": settings.similarity_min_score,
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
