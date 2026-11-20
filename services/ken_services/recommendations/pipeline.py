"""The recommendation job (``JobKind.RECOMMENDATION``, ``make recommend``; ADR-0011).

One run, through the generic job runner (advisory lock, counters, ``job.completed``)::

    load policies + taxonomy; resolve the embedding identity (optional provider)
    live facts of every page (+ eligibility, inbound link counts)
    for every source in scope, in canonical-URL order, in ONE transaction per source
    holding the per-source plan-writer lock (``validity.lock_source_plans``; review edits
    and the autonomy engine's own recording take the same lock):
        every open plan of the source (except VALIDATED ones, owned by the deployment
            service) whose source or target is now DURABLY ineligible
            (protected, gone, unpublished, noindex, alias, thin, excluded type), or whose
            pair is now already linked -> EXPIRED ("invalid: ...", fail closed); analysis
            lag and HTTP errors only pause generation (``validity.plan_invalidity``, shared
            with the review service)
        other open plans on an OLDER source version: check_plan_freshness
            stale     -> transition to EXPIRED (system:recommendation-engine)
            rebasable -> unchanged (still occupies its pair and span)
        if the source is eligible (as_source):
            retrieve (channels) -> candidates
            pair stage (PairState -> pair_rejection), in retrieval order
            GATES (refuse, never down-score; ``gates``): geography_gate ->
                geography_mismatch, market_gate -> market_unrelated
            score (score_pair) -> below_min_relevance
            in rank order (relevance desc, target canonical URL, target id):
                anchor = the TARGET's market keyword (``market_keyword_anchors``), named
                    with the target's geography when it differs from the source's
                    -> no_market_keyword_anchor when the source text lacks it verbatim
                place (find_placement) -> no_natural_anchor
                final score (with_placement) -> below_min_score
                caps: link_density_exceeded, per_source_limit, diversity_limit
            INSERT kept plans (status proposed) + initial transition + link.proposed event
            INSERT every candidate (kept ones carry plan_id)
    replace opportunities (orphan / underlinked / link_capacity), one transaction
    -> succeeded | completed_with_errors

**ADR-0018 (autonomy-first publishing) changes nothing here, deliberately.** The engine still
proposes plans in ``proposed``; what reads them afterwards is now ``make autonomy-run``
instead of a human queue. Two consequences are worth stating because they are easy to get
wrong later:

* ``autonomy_decided`` IS in ``OPEN_STATUSES``, so such a plan occupies its pair and its
  anchor span here and is RE-VALIDATED by ``_expire_invalid`` like any other open plan. A
  decision taken minutes ago must not survive its destination being protected, and this is
  one of the two places that catches it (the publisher re-evaluates as well).
* ``blocked`` is deliberately NOT open: a doomed pair must not hold its target's diversity
  budget for ever. What stops the engine re-proposing it in a loop is the per-(source, target)
  retry budget and cool-down in ``publish_attempts``, not an occupied pair.

**Idempotent.** An OPEN plan, or a plan whose link is physically on the page (ADR-0014 s8:
deployed / verified / measured / learning_captured, and failed after an APPLIED deployment,
until rolled back -- regardless of ``source_page_version``) occupies its pair, so
re-running on unchanged pages proposes nothing new: the pairs are
``duplicate_recommendation``. Open plans of the source also
count toward its per-source cap and link density, occupy their anchor spans
(``taken_spans``) and count as a link in their block (``max_links_per_block``).

**Live state only.** Eligibility is ``compute_eligibility`` over current facts with a fresh
analysis (current version AND current analysis version); protections are the ACTIVE ones;
``already_linked`` follows the source's CURRENT version's body links through ``known_urls``
(redirects and aliases count); inbound counts come from current versions of live pages.

**Scope.** By default every page is considered and ineligible sources are skipped
(``sources_skipped_ineligible``; their open plans are still expired as invalid). Sources named
explicitly (``source_page_ids``) are always processed, so an ineligible one records its
candidates with ``source_protected`` / ``source_ineligible`` and gets no plan.

**Failure isolation.** A source that raises rolls back its own transaction only: it counts
``errors`` (logged with the exception CLASS only) and the run ends
``completed_with_errors``. Plans, transitions, events and candidates of one source commit
together or not at all.

Counters: ``sources_considered``, ``sources_skipped_ineligible``, ``candidates``,
``rejected_<code>`` for every ``CandidateRejection``, ``plans_created``, ``plans_expired``,
``plans_invalidated``, ``opportunities``, ``errors``.
"""

from __future__ import annotations

import math
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.events import EventEnvelope, EventName
from ken_contracts.intelligence import (
    GeographyNode,
    Intent,
    LinkEligibility,
    PageAnalysis,
    TaxonomyPolicy,
    analysis_version,
)
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail
from ken_contracts.model import ModelProvider
from ken_contracts.recommendations import (
    ENGINE_VERSION,
    SUBJECT_LINK_PLAN,
    CandidateRejection,
    ExperimentGuard,
    LinkPlan,
    LinkPlanEvent,
    LinkPlanStatus,
    ModuleKind,
    OpportunityKind,
    PlanFreshness,
    RankingPolicy,
    ReasonCode,
    RecommendationPolicy,
    ScoreBreakdown,
)
from ken_contracts.vectors import VectorStore
from ken_core.config import REPO_ROOT, Settings
from ken_core.db.repositories import recommendations as repo
from ken_core.db.repositories.events import append_event
from ken_core.logging import get_logger
from ken_core.providers.vectors.factory import build_vector_store
from ken_services.intelligence.embeddings import current_recipe_fingerprint, embedding_identity
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import ContentBlock, extract_blocks
from ken_services.jobs.runner import (
    JobContext,
    JobFailedError,
    JobTransaction,
    run_job,
    utc_now,
)
from ken_services.recommendations.channels import (
    EmbeddingKey,
    RetrievedCandidate,
    cosine_to,
    retrieve,
)
from ken_services.recommendations.filters import NoExperiments, PairState, pair_rejection
from ken_services.recommendations.freshness import check_plan_freshness
from ken_services.recommendations.gates import (
    GeographyVerdict,
    geography_gate,
    geography_terms,
    market_gate,
    primary_geography,
)
from ken_services.recommendations.modules import (
    ModuleCandidate,
    ModuleSource,
    regional_sibling_module,
)
from ken_services.recommendations.placement import (
    MARKET_WORD,
    PlacementCandidate,
    corpus_title_phrases,
    find_placement,
    market_core,
    market_keyword_anchors,
    market_keyword_phrases,
    market_phrases,
    phrase_occurrences,
)
from ken_services.recommendations.policy import (
    load_recommendation_policy,
    resolve_ranking_policy,
)
from ken_services.recommendations.rationale import render_rationale
from ken_services.recommendations.scoring import (
    PairFeatures,
    ScoredPair,
    anchor_reason_code,
    channel_reason_codes,
    merge_reason_codes,
    score_pair,
    with_placement,
)
from ken_services.recommendations.transitions import ENGINE_ACTOR, record_initial, transition
from ken_services.recommendations.validity import (
    DURABLE_INELIGIBILITY,
    lock_source_plans,
    page_eligibility,
    plan_invalidity,
)

__all__ = [
    "DURABLE_INELIGIBILITY",
    "EMITTED_BY",
    "RECOMMENDATION_COUNTERS",
    "RecommendationToolkit",
    "default_toolkit",
    "run_recommendations",
]

logger = get_logger("ken_services.recommendations.pipeline")

EMITTED_BY: Final = "ken-recommend"
"""``emitted_by`` of every ``link.proposed`` event."""
NO_PAGE_AT_ANALYSIS_VERSION: Final = "no page in the corpus carries this run's analysis_version"
"""``failure_reason`` when the corpus is analysed, but under a DIFFERENT version (item 28).

Eligibility requires ``analysis_version`` to match, and that string is derived from the loaded
taxonomy. Point the engine at taxonomy v1 while the corpus was analysed under v3 and every page
is refused as ``stale_analysis``: before this guard the job still exited ``succeeded``, and the
only signal was a rejection counter. One full-corpus run was wasted that way on 2026-10-31.

Fixed text, as :class:`JobFailedError` requires -- never the versions themselves, which a
``config_snapshot`` already records.
"""
RECOMMENDATION_COUNTERS: Final = (
    "sources_considered",
    "sources_skipped_ineligible",
    "candidates",
    *(f"rejected_{code.value}" for code in CandidateRejection),
    "plans_created",
    "plans_expired",
    "plans_invalidated",
    "opportunities",
    "modules",
    "module_items",
    "errors",
)
_R = CandidateRejection
MAX_TARGET_TITLE_CHARS: Final = 300
"""A plan copies at most this much of the target title (QA: ingestion does not cap it)."""
RELATED_REPORT_MODULES_ENABLED: Final = False
"""Owner decision 2026-11-20: the site already has Related Reports.

Generating another recommendation surface there is duplicative and reads as a pushy cross-sell.
Regional siblings must earn an inline placement in allowed explanatory prose or receive no link.
"""


def analysis_version_absent(facts: Iterable[repo.PageFacts], *, analysis_version: str) -> bool:
    """Is the corpus analysed, but NOT ONE page at ``analysis_version`` (item 28)?

    The condition is narrow on purpose. A corpus that has never been analysed is a legitimate
    state -- a fresh install, or the inventory running ahead of analysis -- and a run where
    pages do match but are ineligible for their own reasons is a real result worth recording.
    Only "there are analyses, and none of them is ours" means the run is configured to produce
    nothing, which is the signature of a misconfigured ``KEN_TAXONOMY_PATH``.

    ``PageFacts.analysis`` only PREFERS this version -- it falls back to the newest analysis of
    any version -- so the version has to be compared explicitly rather than tested for None. A
    corpus re-crawled but not re-analysed answers true here too, and should: the run would
    produce nothing either way, and an operator wants to hear that in seconds, not in hours.
    """
    present = tuple(facts)
    if any(
        f.analysis is not None and f.analysis.analysis_version == analysis_version for f in present
    ):
        return False
    return any(f.has_any_analysis for f in present)


def _any_occurrence(phrases: Sequence[Any], blocks: Sequence[ContentBlock]) -> bool:
    """Does any anchor phrase occur, whole-phrase, in any block of the source version?

    Separates "the source text does not contain the target's market keyword"
    (``no_market_keyword_anchor``) from "it does, but no occurrence is placeable"
    (``no_natural_anchor``), so the counters say which problem a pair has.
    """
    return any(
        phrase_occurrences(getattr(phrase, "text", ""), block.text)
        for phrase in phrases
        for block in blocks
    )


def _bounded_title(title: str | None) -> str | None:
    if title is None or len(title) <= MAX_TARGET_TITLE_CHARS:
        return title
    return title[: MAX_TARGET_TITLE_CHARS - 1].rstrip() + "\u2026"


# --------------------------------------------------------------------------- #
# Injectable deterministic core (workstream A's modules by default)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RecommendationToolkit:
    """The pure functions the job calls. Tests inject fakes where their logic matters."""

    pair_rejection: Callable[..., CandidateRejection | None] = pair_rejection
    geography_gate: Callable[..., GeographyVerdict | None] = geography_gate
    market_gate: Callable[..., Sequence[Any]] = market_gate
    score_pair: Callable[..., ScoredPair] = score_pair
    with_placement: Callable[[ScoreBreakdown, float], ScoreBreakdown] = with_placement
    market_keyword_anchors: Callable[..., Sequence[Any]] = market_keyword_anchors
    find_placement: Callable[..., PlacementCandidate | None] = find_placement
    check_plan_freshness: Callable[..., Any] = check_plan_freshness
    render_rationale: Callable[..., str] = render_rationale
    extract_blocks: Callable[[str], Sequence[ContentBlock]] = extract_blocks


def default_toolkit() -> RecommendationToolkit:
    return RecommendationToolkit()


# --------------------------------------------------------------------------- #
# Run-wide state
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _World:
    facts: dict[uuid.UUID, repo.PageFacts]
    eligibility: dict[uuid.UUID, LinkEligibility]
    inbound: dict[uuid.UUID, int]
    av: str
    titles: tuple[str, ...] = ()
    """Every page title in the inventory, as a fragment guard for
    :func:`find_placement` ``enclosing_phrases``.

    Ken's report pages end with a cross-sell list of OTHER reports, one title per ``li``
    ("Thailand Residential Furniture Market"). Those titles are not taxonomy market labels,
    so ``market_phrases`` does not cover them, and the engine was anchoring on their tail:
    "Thailand Residential [[Furniture Market]]" pointing at a Germany furniture report.
    Measured over the 1,028-plan corpus run, 185 plans (18%) were this. It is the same
    strict-fragment rule already in force, applied to a wider phrase set, so no new
    mechanism is introduced."""


@dataclass(slots=True)
class _Ranked:
    candidate: RetrievedCandidate
    target: repo.PageFacts
    target_analysis: PageAnalysis
    scored: ScoredPair
    geography: GeographyVerdict
    """Which row of the geography gate let this pair through (and what the anchor must say)."""


@dataclass(slots=True)
class _Outcome:
    """What one source produced: candidate rows and the plans to insert (with events)."""

    candidates: list[repo.NewCandidate] = field(default_factory=list)
    plans: list[LinkPlan] = field(default_factory=list)


def _intent(value: Any) -> Intent | None:
    return None if value is None else Intent(value)


class _Recommend:
    def __init__(
        self,
        *,
        job: JobContext,
        settings: Settings,
        policy: RecommendationPolicy,
        ranking: RankingPolicy,
        taxonomy: TaxonomyPolicy,
        embedding: EmbeddingKey | None,
        guard: ExperimentGuard,
        toolkit: RecommendationToolkit,
        source_page_ids: Sequence[uuid.UUID] | None,
        vector_store: VectorStore | None = None,
    ) -> None:
        self.job = job
        self.settings = settings
        # ADR-0026: the shared index, built ONCE per run. None makes `retrieve` rebuild an
        # exact in-process index from PostgreSQL for EVERY source -- O(pages) per source,
        # about 2.3 s per page at 8,858 pages.
        self.vector_store = vector_store
        self.policy = policy
        self.ranking = ranking
        self.taxonomy = taxonomy
        self.market_phrases = market_phrases(taxonomy)
        self.embedding = embedding
        self.guard = guard
        self.tools = toolkit
        self.source_page_ids = source_page_ids
        self.av = analysis_version(taxonomy.version)
        self.per_target: Counter[uuid.UUID] = Counter()
        """OPEN plans per target -- seeded from the database at the start of the run, then
        incremented as plans are committed (destination diversity). Seeding it is what makes
        a re-run idempotent: a target capped in run 1 stays capped in run 2."""

    # ------------------------------------------------------------- plumbing
    def _error(self, event: str, exc: BaseException, **fields: Any) -> None:
        logger.error(event, run_id=str(self.job.run_id), error_class=type(exc).__name__, **fields)
        self.job.increment("errors")

    def _eligibility(self, facts: repo.PageFacts) -> LinkEligibility:
        return page_eligibility(facts, taxonomy=self.taxonomy, analysis_version=self.av)

    async def _world(self) -> _World:
        async with self.job.sessionmaker() as session:
            facts = await repo.page_facts(session, analysis_version=self.av)
            inbound = await repo.inbound_link_counts(session)
        return _World(
            facts=facts,
            eligibility={pid: self._eligibility(f) for pid, f in facts.items()},
            inbound=inbound,
            av=self.av,
            titles=corpus_title_phrases(f.title for f in facts.values()),
        )

    def _target_path_excluded(self, url: str) -> bool:
        path = urlsplit(url).path or "/"
        return any(re.search(pattern, path) for pattern in self.policy.excluded_target_paths)

    def _require_matching_analysis(self, world: _World) -> None:
        """Fail a run that can only produce nothing (item 28)."""
        if not analysis_version_absent(world.facts.values(), analysis_version=self.av):
            return
        logger.error(
            "recommendation_analysis_version_absent",
            run_id=str(self.job.run_id),
            analysis_version=self.av,
            taxonomy_version=self.taxonomy.version,
            pages=len(world.facts),
        )
        raise JobFailedError(NO_PAGE_AT_ANALYSIS_VERSION)

    # ------------------------------------------------------------------ run
    async def run(self) -> RunStatus:
        job = self.job
        world = await self._world()
        self._require_matching_analysis(world)
        async with job.sessionmaker() as session:
            self.per_target.update(await repo.open_plan_target_counts(session))

        # Deterministic order by canonical URL (then id): caps such as
        # max_open_plans_per_target are consumed in this order, so random page
        # UUIDs must not decide which source gets a contested target (eval finding).
        def by_url(pid: uuid.UUID) -> tuple[str, str]:
            return (world.facts[pid].canonical_url, str(pid))

        if self.source_page_ids is None:
            scope = sorted(world.facts, key=by_url)
        else:
            scope = sorted(
                (pid for pid in set(self.source_page_ids) if pid in world.facts), key=by_url
            )
        job.increment("sources_considered", len(scope))
        for source_id in scope:
            try:
                await self._source(world, world.facts[source_id])
            except Exception as exc:
                self._error("recommendation_source_failed", exc, page_id=str(source_id))
        await job.flush()
        try:
            await self._opportunities(world)
        except Exception as exc:
            self._error("opportunities_failed", exc)
        await job.flush()
        try:
            await self._modules(world)
        except Exception as exc:
            self._error("modules_failed", exc)
        await job.flush()
        clean = job.counters.get("errors", 0) == 0
        return RunStatus.SUCCEEDED if clean else RunStatus.COMPLETED_WITH_ERRORS

    # --------------------------------------------------------------- source
    async def _source(self, world: _World, source: repo.PageFacts) -> None:
        eligible = world.eligibility[source.id].as_source
        outcome: _Outcome | None = None
        async with self.job.transaction() as tx:
            session = tx.session
            # Serialise with review edits of this source (ADR-0013 s7): open-pair and
            # open-span reads below and the inserts after them must not interleave.
            await lock_source_plans(session, source_page_id=source.id)
            body = await repo.version_body(
                session, page_id=source.id, version=source.current_version
            )
            blocks = tuple(self.tools.extract_blocks(body or ""))
            expired = await self._expire_invalid(tx, world, source, blocks)
            if not eligible and self.source_page_ids is None:
                tx.increment("sources_skipped_ineligible")
            else:
                outcome = await self._generate(session, world, source, blocks)
                await self._commit_outcome(tx, outcome)
        # Committed: only now do expiries free, and new plans take, a diversity slot.
        for target_id in expired:
            self.per_target[target_id] -= 1
        for plan in () if outcome is None else outcome.plans:
            self.per_target[plan.target_page_id] += 1

    async def _commit_outcome(self, tx: JobTransaction, outcome: _Outcome) -> None:
        session = tx.session
        now = self.job.clock()
        for plan in outcome.plans:
            await self._insert_plan(session, plan, now)
        await repo.insert_candidates(
            session, run_id=self.job.run_id, candidates=outcome.candidates, created_at=now
        )
        tx.increment("candidates", len(outcome.candidates))
        tx.increment("plans_created", len(outcome.plans))
        for item in outcome.candidates:
            if item.rejection is not None:
                tx.increment(f"rejected_{item.rejection.value}")

    def _invalidity(
        self, world: _World, source: repo.PageFacts, target_id: uuid.UUID, linked: set[uuid.UUID]
    ) -> str | None:
        """Why an OPEN plan may no longer stand, whatever its freshness (fail closed: a plan
        must never be published -- or, on the exception path, wait for a human -- for a
        protected, gone or unlinkable page).

        Only DURABLE reasons expire a plan (``DURABLE_INELIGIBILITY``). Expiry is terminal,
        so lag (``stale_analysis`` / ``not_analyzed`` after a recrawl or a taxonomy bump,
        before ``make analyze``) and a possibly transient ``http_not_ok`` keep the plan open:
        no new plans are generated meanwhile, and deployment re-validates (QA re-review).
        The rules are ``validity.plan_invalidity`` (shared with the review service)."""
        return plan_invalidity(
            source_eligibility=world.eligibility[source.id],
            target_eligibility=world.eligibility.get(target_id),
            already_linked=target_id in linked,
        )

    async def _expire_invalid(
        self,
        tx: JobTransaction,
        world: _World,
        source: repo.PageFacts,
        blocks: Sequence[ContentBlock],
    ) -> list[uuid.UUID]:
        """Expire the source's open plans that became invalid or stale; their target ids."""
        expired: list[uuid.UUID] = []
        links = await repo.version_links(
            tx.session, page_id=source.id, version=source.current_version
        )
        linked = {link.target_page_id for link in links if link.target_page_id}
        for plan in await repo.open_plans_for_source(tx.session, source_page_id=source.id):
            if plan.status is LinkPlanStatus.VALIDATED:
                # ADR-0014 s8: VALIDATED belongs to the deployment service (its durable intent
                # is in flight); only it may expire or fail the plan. It still occupies its
                # pair and span below (OPEN_STATUSES).
                continue
            invalid = self._invalidity(world, source, plan.target_page_id, linked)
            if invalid is not None:
                await transition(
                    tx.session,
                    plan_id=plan.id,
                    to_status=LinkPlanStatus.EXPIRED,
                    actor=ENGINE_ACTOR,
                    note="invalid: " + invalid,
                    clock=self.job.clock,
                )
                tx.increment("plans_invalidated")
                expired.append(plan.target_page_id)
                continue
            if plan.source_page_version >= source.current_version:
                continue
            check = self.tools.check_plan_freshness(
                plan_source_version=plan.source_page_version,
                current_version=source.current_version,
                locator=plan.locator,
                anchor_text=plan.anchor_text,
                current_blocks=blocks,
                excluded_ancestors=self.policy.placement.excluded_ancestors,
            )
            if PlanFreshness(check.freshness) is not PlanFreshness.STALE:
                continue  # rebasable: keeps its status (and its pair and span)
            await transition(
                tx.session,
                plan_id=plan.id,
                to_status=LinkPlanStatus.EXPIRED,
                actor=ENGINE_ACTOR,
                note="stale: " + ", ".join(check.reasons),
                clock=self.job.clock,
            )
            tx.increment("plans_expired")
            expired.append(plan.target_page_id)
        return expired

    async def _generate(
        self,
        session: AsyncSession,
        world: _World,
        source: repo.PageFacts,
        blocks: Sequence[ContentBlock],
    ) -> _Outcome:
        selection = self.policy.selection
        source_analysis = source.analysis  # None only for an ineligible, requested source
        retrieved = await retrieve(
            session,
            source_page_id=source.id,
            retrieval=self.policy.retrieval,
            embedding=self.embedding,
            keywords=() if source_analysis is None else source_analysis.keywords,
            industry_id=None if source_analysis is None else source_analysis.industry_id,
            analysis_version=self.av,
            vector_store=self.vector_store,
        )

        # Occupancy: open plans (after expiry) and plans whose link is on the page.
        open_plans = await repo.open_plans_for_source(session, source_page_id=source.id)
        occupied = {p.target_page_id for p in open_plans} | await repo.occupying_targets(
            session, source_page_id=source.id
        )
        since = self.job.clock() - timedelta(days=selection.rejection_cooldown_days)
        rejected = await repo.rejected_targets_since(session, source_page_id=source.id, since=since)
        links = await repo.version_links(session, page_id=source.id, version=source.current_version)
        linked_targets = {link.target_page_id for link in links if link.target_page_id}
        links_per_block: Counter[str] = Counter(link.block_id for link in links)
        blocks_linking: dict[uuid.UUID, set[str]] = defaultdict(set)
        for link in links:
            if link.target_page_id is not None:
                blocks_linking[link.target_page_id].add(link.block_id)
        # Open plans' spans: placed against the same block TEXT (fresh or rebasable).
        by_hash = {sha256_hex(b.text): b.block_id for b in blocks}
        taken: set[tuple[str, int, int]] = set()
        for plan in open_plans:
            taken.add((plan.locator.context_hash, plan.locator.start, plan.locator.end))
            block_id = by_hash.get(plan.locator.context_hash)
            if block_id is not None:
                links_per_block[block_id] += 1
                blocks_linking[plan.target_page_id].add(block_id)
        market_of = {
            pid: (f.analysis.market_id if f.analysis is not None else None)
            for pid, f in world.facts.items()
        }

        outcome = _Outcome()
        source_eligibility = world.eligibility[source.id]

        def record(
            candidate: RetrievedCandidate,
            target_version: int,
            *,
            rejection: CandidateRejection | None,
            total: float | None,
            plan_id: uuid.UUID | None = None,
        ) -> None:
            outcome.candidates.append(
                repo.NewCandidate(
                    source_page_id=source.id,
                    source_page_version=source.current_version,
                    target_page_id=candidate.target_page_id,
                    target_page_version=target_version,
                    channels=candidate.channels,
                    channel_scores={k: round(v, 6) for k, v in candidate.channel_scores.items()},
                    total_score=None if total is None else min(1.0, max(0.0, total)),
                    rejection=rejection,
                    plan_id=plan_id,
                )
            )

        # ---- pair stage + GATES + relevance
        # The gates read the source page's own text (every block, not only the placeable
        # ones): naming a geography in a heading or a list item is still naming it.
        source_text = "\n".join(block.text for block in blocks)
        # The SOURCE's own market name, as a fragment guard. A report mostly writes about
        # itself ("The India Cloud Gaming and Esports Market is projected to ..."), so without
        # this a slice of its own name becomes the anchor for a DIFFERENT report, which
        # misreads the sentence. `market_phrases` covers markets the taxonomy knows; this
        # covers the page's own identity whether or not the taxonomy lists it.
        _own = market_keyword_phrases(
            target_url=source.canonical_url, target_title=source.title, taxonomy=self.taxonomy
        )
        source_own_phrases: tuple[str, ...] = (
            *_own,
            *(f"{phrase} {MARKET_WORD}" for phrase in _own),
        )
        # ADR-0027 §1 amendment: the same market in another region is the owner's FIRST
        # priority ("Germany EV market" -> "India EV market"), so a cross-region pair is
        # allowed when the market cores match. Computed once per source.
        source_core = market_core(
            target_url=source.canonical_url, target_title=source.title, taxonomy=self.taxonomy
        )
        survivors: list[tuple[RetrievedCandidate, repo.PageFacts, GeographyVerdict]] = []
        for candidate in retrieved:
            target = world.facts.get(candidate.target_page_id)
            if target is None:  # pragma: no cover - retrieval reads the same pages
                continue
            locked = await self.guard.locked(source_page_id=source.id, target_page_id=target.id)
            state = PairState(
                source_id=source.id,
                target_id=target.id,
                source_protected=source.protected,
                target_protected=target.protected,
                experiment_locked=bool(locked),
                source_eligibility=source_eligibility,
                target_eligibility=world.eligibility[target.id],
                already_linked=target.id in linked_targets,
                duplicate=target.id in occupied,
                previously_rejected=target.id in rejected,
                source_page_type=None if source_analysis is None else source_analysis.page_type,
                target_page_type=None if target.analysis is None else target.analysis.page_type,
                source_intent=None if source_analysis is None else _intent(source_analysis.intent),
                target_intent=None if target.analysis is None else _intent(target.analysis.intent),
                target_path_excluded=self._target_path_excluded(target.canonical_url),
            )
            rejection = self.tools.pair_rejection(state, policy=self.policy)
            if rejection is not None:
                record(candidate, target.current_version, rejection=rejection, total=None)
                continue
            target_analysis = target.analysis
            if source_analysis is None or target_analysis is None:  # pragma: no cover
                continue  # the pair stage already rejected both cases
            # Hard gates, before any scoring: a wrong geography or an unrelated market is
            # REFUSED, never carried by the rest of the score.
            target_core = market_core(
                target_url=target.canonical_url,
                target_title=target.title,
                taxonomy=self.taxonomy,
            )
            verdict = self.tools.geography_gate(
                source_analysis,
                target_analysis,
                taxonomy=self.taxonomy,
                source_text=source_text,
                same_market_core=source_core is not None and source_core == target_core,
            )
            if verdict is None:
                record(candidate, target.current_version, rejection=_R.GEOGRAPHY_MISMATCH,
                       total=None)  # fmt: skip
                continue
            if not self.tools.market_gate(
                source_analysis,
                target_analysis,
                taxonomy=self.taxonomy,
                # Siblings need the embeddings to agree as well (ADR-0027 amendment).
                semantic_similarity=candidate.signals.get("semantic_similarity"),
                sibling_floor=self.settings.similarity_min_score,
            ):
                record(candidate, target.current_version, rejection=_R.MARKET_UNRELATED,
                       total=None)  # fmt: skip
                continue
            survivors.append((candidate, target, verdict))

        if not survivors:
            return outcome
        if source_analysis is None:  # pragma: no cover - the pair stage rejects every pair
            raise LookupError("source without analysis passed the pair stage")
        cosines: Mapping[uuid.UUID, float] = {}
        if self.embedding is not None:
            cosines = await cosine_to(
                session,
                source_page_id=source.id,
                target_page_ids=[t.id for _, t, _ in survivors],
                key=self.embedding,
            )
        source_markets = Counter(
            market_of.get(link.target_page_id)
            for link in links
            if link.target_page_id is not None and link.target_page_id != source.id
        )
        ranked: list[_Ranked] = []
        for candidate, target, verdict in survivors:
            target_analysis = target.analysis
            if target_analysis is None:  # pragma: no cover - an eligible target has one
                raise LookupError("eligible target without analysis")
            market = target_analysis.market_id
            features = PairFeatures(
                cosine=cosines.get(target.id),
                source=source_analysis,
                target=target_analysis,
                source_links_into_target_market=0 if market is None else source_markets[market],
                target_inbound_links=world.inbound.get(target.id, 0),
            )
            scored = self.tools.score_pair(
                features,
                policy=self.policy,
                ranking=self.ranking,
                taxonomy=self.taxonomy,
                semantic_floor=self.settings.similarity_min_score,
            )
            relevance = scored.breakdown.relevance
            if relevance < selection.min_relevance:
                record(candidate, target.current_version, rejection=_R.BELOW_MIN_RELEVANCE,
                       total=relevance)  # fmt: skip
                continue
            ranked.append(
                _Ranked(
                    candidate=candidate,
                    target=target,
                    target_analysis=target_analysis,
                    scored=scored,
                    geography=verdict,
                )
            )
        # Ties break on the target's canonical URL, never a random UUID (determinism).
        ranked.sort(
            key=lambda r: (-r.scored.breakdown.relevance, r.target.canonical_url, r.target.id)
        )

        # ---- placement + final score + caps, in rank order
        salience = {b.block_id: float(b.score) for b in source_analysis.salient_blocks}
        selected = len(open_plans)
        now = self.job.clock()
        for item in ranked:
            candidate, target, scored = item.candidate, item.target, item.scored
            relevance = scored.breakdown.relevance
            verdict = item.geography
            phrases = self.tools.market_keyword_anchors(
                target_url=target.canonical_url,
                target_title=target.title,
                taxonomy=self.taxonomy,
                policy=self.policy,
                geography_terms=(
                    ()
                    if verdict.target_geography is None
                    else geography_terms(verdict.target_geography)
                ),
                require_geography=verdict.anchor_must_name_target_geography,
                # Partial title runs produced anchors that were technically related but too
                # vague to predict the destination. Require the target's complete market
                # phrase; a missing exact phrase is safer than an engagement-poor link.
                allow_context=False,
            )
            if not phrases or not _any_occurrence(phrases, blocks):
                # The target's market keyword is not in the source text. A later phase may
                # rewrite the sentence; this one proposes nothing.
                record(candidate, target.current_version, rejection=_R.NO_MARKET_KEYWORD_ANCHOR,
                       total=relevance)  # fmt: skip
                continue
            placement = self.tools.find_placement(
                blocks=blocks,
                phrases=phrases,
                links_per_block=dict(links_per_block),
                blocks_linking_target=frozenset(blocks_linking.get(target.id, set())),
                taken_spans=frozenset(taken),
                salience=salience,
                policy=self.policy,
                enclosing_phrases=(
                    *self.market_phrases,
                    *source_own_phrases,
                    *world.titles,
                ),
            )
            if placement is None:
                record(candidate, target.current_version, rejection=_R.NO_NATURAL_ANCHOR,
                       total=relevance)  # fmt: skip
                continue
            final = self.tools.with_placement(scored.breakdown, placement.placement_quality)
            if final.total < selection.min_score:
                record(candidate, target.current_version, rejection=_R.BELOW_MIN_SCORE,
                       total=final.total)  # fmt: skip
                continue
            cap = self._cap(source, selected, target.id)
            if cap is not None:
                record(candidate, target.current_version, rejection=cap, total=final.total)
                continue
            new_plan = self._plan(
                source, source_analysis.analysis_version, item, placement, final, now
            )
            outcome.plans.append(new_plan)
            record(candidate, target.current_version, rejection=None, total=final.total,
                   plan_id=new_plan.id)  # fmt: skip
            selected += 1
            locator = placement.locator
            taken.add((locator.context_hash, locator.start, locator.end))
            links_per_block[locator.block_id] += 1
            blocks_linking[target.id].add(locator.block_id)
        return outcome

    def _cap(
        self, source: repo.PageFacts, selected: int, target_id: uuid.UUID
    ) -> CandidateRejection | None:
        selection = self.policy.selection
        words = source.word_count
        density = (
            math.inf
            if words <= 0
            else ((source.internal_link_count + selected + 1) / words * 100.0)
        )
        if density > selection.max_internal_links_per_100_words:
            return _R.LINK_DENSITY_EXCEEDED
        if selected >= selection.max_plans_per_source:
            return _R.PER_SOURCE_LIMIT
        # A source holds at most one open plan per target, so only OTHER sources' plans of
        # this run count here (committed ones: see _source).
        if self.per_target[target_id] >= selection.max_open_plans_per_target:
            return _R.DIVERSITY_LIMIT
        return None

    def _plan(
        self,
        source: repo.PageFacts,
        source_analysis_version: str,
        item: _Ranked,
        placement: PlacementCandidate,
        final: ScoreBreakdown,
        now: datetime,
    ) -> LinkPlan:
        scored, target = item.scored, item.target
        geography = item.geography.target_geography
        names_geography = geography is not None and any(
            phrase_occurrences(term, placement.anchor_text) for term in geography_terms(geography)
        )
        reason_codes = merge_reason_codes(
            scored.reason_codes,
            item.geography.reason_codes,
            channel_reason_codes(item.candidate.channels),
            anchor_reason_code(placement.anchor_source),
            (ReasonCode.ANCHOR_IS_TARGET_MARKET_KEYWORD,),
            (ReasonCode.ANCHOR_NAMES_TARGET_GEOGRAPHY,) if names_geography else (),
        )
        return LinkPlan(
            id=uuid.uuid4(),
            run_id=self.job.run_id,
            source_page_id=source.id,
            source_page_version=source.current_version,
            source_url=source.canonical_url,
            target_page_id=target.id,
            target_page_version=target.current_version,
            target_url=target.canonical_url,
            target_title=_bounded_title(target.title),
            relationship_type=scored.relationship_type,
            channels=item.candidate.channels,
            anchor_text=placement.anchor_text,
            anchor_source=placement.anchor_source,
            locator=placement.locator,
            preview=placement.preview,
            score=final,
            confidence=final.total,
            calibration_version=None,
            reason_codes=reason_codes,
            rationale=self.tools.render_rationale(
                reason_codes=reason_codes,
                target_title=_bounded_title(target.title),
                relationship_type=scored.relationship_type,
                anchor_text=placement.anchor_text,
            ),
            engine_version=ENGINE_VERSION,
            policy_version=self.policy.version,
            ranking_policy_version=self.ranking.version,
            embedding_model=None if self.embedding is None else self.embedding.model,
            reasoning_model=None,
            analysis_version=source_analysis_version,
            supersedes=None,
            status=LinkPlanStatus.PROPOSED,
            created_at=now,
            updated_at=now,
        )

    async def _insert_plan(self, session: AsyncSession, plan: LinkPlan, now: datetime) -> None:
        await repo.insert_plan(session, plan=plan)
        await record_initial(
            session,
            plan_id=plan.id,
            status=LinkPlanStatus.PROPOSED,
            actor=ENGINE_ACTOR,
            occurred_at=now,
        )
        payload = LinkPlanEvent(
            plan_id=plan.id,
            source_page_id=plan.source_page_id,
            source_page_version=plan.source_page_version,
            target_page_id=plan.target_page_id,
            anchor_text=plan.anchor_text,
            status=plan.status,
            score=plan.score.total,
            run_id=self.job.run_id,
            actor=ENGINE_ACTOR,
        )
        await append_event(
            session,
            EventEnvelope(
                event_name=EventName.LINK_PROPOSED,
                occurred_at=now,
                emitted_by=EMITTED_BY,
                subject_type=SUBJECT_LINK_PLAN,
                subject_id=str(plan.id),
                payload=payload.model_dump(mode="json"),
                correlation_id=self.job.run_id,
            ),
        )

    # -------------------------------------------------------- opportunities
    async def _opportunities(self, world: _World) -> None:
        """Replace ``opportunities`` from current state (ADR-0011 section 7)."""
        threshold = self.policy.underlinked_inbound_threshold
        selection = self.policy.selection
        async with self.job.transaction() as tx:
            open_counts = Counter(await repo.open_plan_counts(tx.session))
            items: list[repo.NewOpportunity] = []
            for pid in sorted(world.facts):
                facts, eligibility = world.facts[pid], world.eligibility[pid]
                inbound = world.inbound.get(pid, 0)
                importance = 0 if facts.analysis is None else facts.analysis.commercial_importance
                if eligibility.as_target and inbound == 0:
                    items.append(
                        repo.NewOpportunity(
                            page_id=pid,
                            kind=OpportunityKind.ORPHAN,
                            score=round((1 + importance) / 4, 6),
                            evidence={"inbound_links": 0, "commercial_importance": importance},
                        )
                    )
                if eligibility.as_target and importance >= 2 and inbound < threshold:
                    items.append(
                        repo.NewOpportunity(
                            page_id=pid,
                            kind=OpportunityKind.UNDERLINKED,
                            score=round((1 - inbound / threshold) * importance / 3, 6),
                            evidence={
                                "inbound_links": inbound,
                                "threshold": threshold,
                                "commercial_importance": importance,
                            },
                        )
                    )
                if eligibility.as_source and facts.word_count > 0:
                    allowed = math.floor(
                        selection.max_internal_links_per_100_words * facts.word_count / 100
                    )
                    used = facts.internal_link_count + open_counts[pid]
                    headroom = allowed - used
                    if headroom > 0:
                        items.append(
                            repo.NewOpportunity(
                                page_id=pid,
                                kind=OpportunityKind.LINK_CAPACITY,
                                score=round(min(1.0, headroom / selection.max_plans_per_source), 6),
                                evidence={
                                    "word_count": facts.word_count,
                                    "internal_links": facts.internal_link_count,
                                    "open_plans": open_counts[pid],
                                    "allowed_links": allowed,
                                    "headroom": headroom,
                                },
                            )
                        )
            tx.increment(
                "opportunities",
                await repo.replace_opportunities(
                    tx.session,
                    run_id=self.job.run_id,
                    opportunities=items,
                    computed_at=self.job.clock(),
                ),
            )

    async def _modules(self, world: _World) -> None:
        """Replace the regional-sibling modules from current state (ADR-0027 §1b).

        ADR-0031 now disables this surface because the site already renders Related Reports.
        While disabled, a run clears older regional-sibling modules and writes nothing. The
        historical builder remains below so a later governed decision can reuse it.

        Runs in the SAME job as the plans, on the world already in memory, because a module
        and a plan must never disagree about the corpus: they are two surfaces for one
        decision, and a module built by a separate job could outlive the analysis the plans
        were built from. No model call, no extra database read and no embedding lookup --
        ranking inside a tier uses signals the run already holds.

        Cost is linear: the market cores are grouped once, and each page only looks at its
        own core's group.
        """
        if not RELATED_REPORT_MODULES_ENABLED:
            async with self.job.transaction() as tx:
                await repo.clear_modules(tx.session, kinds=(ModuleKind.REGIONAL_SIBLING,))
            return

        cores: dict[str, list[uuid.UUID]] = defaultdict(list)
        pages: dict[uuid.UUID, tuple[str, GeographyNode | None]] = {}
        for pid in sorted(world.facts):
            facts = world.facts[pid]
            if facts.analysis is None or facts.analysis.analysis_version != world.av:
                continue
            core = market_core(
                target_url=facts.canonical_url, target_title=facts.title, taxonomy=self.taxonomy
            )
            if core is None:
                continue
            pages[pid] = (core, primary_geography(facts.analysis, taxonomy=self.taxonomy))
            if world.eligibility[pid].as_target:
                cores[core].append(pid)

        modules: list[repo.NewModule] = []
        for pid, (core, geography) in pages.items():
            if not world.eligibility[pid].as_source:
                continue
            items = regional_sibling_module(
                ModuleSource(page_id=pid, market_core=core, geography=geography),
                (self._module_candidate(world, other, pages) for other in cores[core]),
                taxonomy=self.taxonomy,
            )
            if not items:
                continue
            modules.append(
                repo.NewModule(
                    page_id=pid,
                    page_version=world.facts[pid].current_version,
                    kind=ModuleKind.REGIONAL_SIBLING,
                    market_core=core,
                    geography_id=None if geography is None else geography.id,
                    geography_label=None if geography is None else geography.label,
                    analysis_version=world.av,
                    items=[
                        repo.NewModuleItem(
                            rank=item.rank,
                            target_page_id=item.page_id,
                            target_url=item.url,
                            target_title=item.title,
                            geography_label=item.geography_label,
                            reason_codes=item.reason_codes,
                        )
                        for item in items
                    ],
                )
            )
        async with self.job.transaction() as tx:
            written = await repo.replace_modules(
                tx.session,
                run_id=self.job.run_id,
                modules=modules,
                computed_at=self.job.clock(),
            )
            tx.increment("modules", len(modules))
            tx.increment("module_items", written)

    def _module_candidate(
        self,
        world: _World,
        page_id: uuid.UUID,
        pages: Mapping[uuid.UUID, tuple[str, GeographyNode | None]],
    ) -> ModuleCandidate:
        facts = world.facts[page_id]
        return ModuleCandidate(
            page_id=page_id,
            url=facts.canonical_url,
            title=facts.title,
            market_core=pages[page_id][0],
            geography=pages[page_id][1],
            inbound_links=world.inbound.get(page_id, 0),
            commercial_importance=0
            if facts.analysis is None
            else facts.analysis.commercial_importance,
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def load_policies(settings: Settings) -> tuple[RecommendationPolicy, RankingPolicy]:
    policy = load_recommendation_policy(settings.recommendation_policy_path)
    return policy, resolve_ranking_policy(policy, root=REPO_ROOT)


async def _embedding_key(provider: ModelProvider | None, settings: Settings) -> EmbeddingKey | None:
    """The CURRENT embedding identity, or None (semantic channel and component off)."""
    if provider is None:
        return None
    try:
        identity = await embedding_identity(provider)
    except Exception as exc:  # unreachable or no model: fail closed, never another model
        logger.warning("embedding_identity_unavailable", error_class=type(exc).__name__)
        return None
    return EmbeddingKey(
        model=identity.model, recipe_fingerprint=current_recipe_fingerprint(settings)
    )


def _snapshot(
    *,
    settings: Settings,
    policy: RecommendationPolicy,
    ranking: RankingPolicy,
    taxonomy: TaxonomyPolicy,
    embedding: EmbeddingKey | None,
    source_page_ids: Sequence[uuid.UUID] | None,
) -> dict[str, Any]:
    """Secret-free: versions, switches and numeric settings only (no URLs, no paths)."""
    return {
        "engine_version": ENGINE_VERSION,
        "analysis_version": analysis_version(taxonomy.version),
        "policy": {"version": policy.version, "status": policy.status},
        "ranking_policy": {"version": ranking.version, "status": ranking.status},
        "taxonomy": {"version": taxonomy.version, "status": taxonomy.status},
        "source_page_ids": None
        if source_page_ids is None
        else sorted(str(p) for p in source_page_ids),
        "retrieval": policy.retrieval.model_dump(mode="json"),
        "selection": policy.selection.model_dump(mode="json"),
        "semantic_floor": settings.similarity_min_score,
        "embedding": None
        if embedding is None
        else {"model": embedding.model, "recipe_fingerprint": embedding.recipe_fingerprint},
    }


async def run_module_build(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    triggered_by: str,
    policy: RecommendationPolicy | None = None,
    ranking: RankingPolicy | None = None,
    taxonomy: TaxonomyPolicy | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> JobRunDetail:
    """Apply the module policy ONLY, without re-proposing a single inline plan.

    Under ADR-0031 this clears the disabled regional-sibling surface and produces no new
    module items. The command remains useful for removing older derived module rows.

    A full run on the real corpus takes about four hours, nearly all of it in retrieval,
    scoring and placement -- none of which a module needs. Without this an operator who
    changed the module ranking, or who wanted modules after an analysis change, had to pay
    for the plans as well. The module step itself takes seconds: it reads the world once and
    groups the market cores.

    It runs under ``JobKind.RECOMMENDATION``, so the advisory lock still serialises it
    against a plan run. That is deliberate -- both write recommendation artifacts from the
    same world, and they must not interleave.

    No model provider and no vector store: modules use neither. ``errors`` stays the
    contract for failure, so a module build that raises is a ``completed_with_errors`` run
    rather than a silent success.

    Raises:
        JobAlreadyRunningError: a recommendation run is in progress (nothing written).
    """
    if policy is None:
        policy, loaded_ranking = load_policies(settings)
        resolved_ranking = ranking or loaded_ranking
    else:
        resolved_ranking = ranking or resolve_ranking_policy(policy, root=REPO_ROOT)
    tax = taxonomy if taxonomy is not None else load_taxonomy(settings.taxonomy_path)

    async def body(job: JobContext) -> RunStatus:
        engine = _Recommend(
            job=job,
            settings=settings,
            policy=policy,
            ranking=resolved_ranking,
            taxonomy=tax,
            embedding=None,
            guard=NoExperiments(),
            toolkit=default_toolkit(),
            source_page_ids=None,
            vector_store=None,
        )
        world = await engine._world()
        engine._require_matching_analysis(world)
        try:
            await engine._modules(world)
        except Exception as exc:
            engine._error("modules_failed", exc)
        await job.flush()
        return (
            RunStatus.SUCCEEDED
            if job.counters.get("errors", 0) == 0
            else RunStatus.COMPLETED_WITH_ERRORS
        )

    return await run_job(
        kind=JobKind.RECOMMENDATION,
        sessionmaker=sessionmaker,
        triggered_by=triggered_by,
        body=body,
        config_snapshot={
            **_snapshot(
                settings=settings,
                policy=policy,
                ranking=resolved_ranking,
                taxonomy=tax,
                embedding=None,
                source_page_ids=None,
            ),
            "step": "modules",
        },
        counter_names=RECOMMENDATION_COUNTERS,
        clock=clock,
    )


async def run_recommendations(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    triggered_by: str,
    source_page_ids: Sequence[uuid.UUID] | None = None,
    policy: RecommendationPolicy | None = None,
    ranking: RankingPolicy | None = None,
    experiment_guard: ExperimentGuard | None = None,
    clock: Callable[[], datetime] = utc_now,
    taxonomy: TaxonomyPolicy | None = None,
    model_provider: ModelProvider | None = None,
    toolkit: RecommendationToolkit | None = None,
    vector_store: VectorStore | None = None,
) -> JobRunDetail:
    """Run one recommendation job and return the finished run (module docstring).

    ``policy`` / ``ranking`` / ``taxonomy`` default to the configured policy files.
    ``model_provider`` (optional) resolves the CURRENT embedding identity for the semantic
    channel and component; without one (or when it cannot answer) both are off and plans
    record ``embedding_model = None``. Nothing is ever embedded here.

    Raises:
        JobAlreadyRunningError: another recommendation run is in progress (nothing written).
        FileNotFoundError / ValueError: a policy file cannot be loaded.
    """
    if policy is None:
        policy, loaded_ranking = load_policies(settings)
        resolved_ranking = ranking or loaded_ranking
    else:
        resolved_ranking = ranking or resolve_ranking_policy(policy, root=REPO_ROOT)
    tax = taxonomy if taxonomy is not None else load_taxonomy(settings.taxonomy_path)
    tools = toolkit if toolkit is not None else default_toolkit()
    guard: ExperimentGuard = experiment_guard if experiment_guard is not None else NoExperiments()
    ids = None if source_page_ids is None else list(dict.fromkeys(source_page_ids))
    embedding = await _embedding_key(model_provider, settings)
    # ADR-0026: one shared index per run. Built here when the deployment configures a real
    # store and the caller supplied none; closed in `finally` because we own it then.
    store = vector_store
    owned = False
    if store is None and settings.vector_store != "memory":
        store = build_vector_store(settings)
        owned = True

    async def body(job: JobContext) -> RunStatus:
        return await _Recommend(
            job=job,
            settings=settings,
            policy=policy,
            ranking=resolved_ranking,
            taxonomy=tax,
            embedding=embedding,
            guard=guard,
            toolkit=tools,
            source_page_ids=ids,
            vector_store=store,
        ).run()

    try:
        return await run_job(
            kind=JobKind.RECOMMENDATION,
            sessionmaker=sessionmaker,
            triggered_by=triggered_by,
            body=body,
            config_snapshot=_snapshot(
                settings=settings,
                policy=policy,
                ranking=resolved_ranking,
                taxonomy=tax,
                embedding=embedding,
                source_page_ids=ids,
            ),
            counter_names=RECOMMENDATION_COUNTERS,
            clock=clock,
        )
    finally:
        if owned and store is not None:
            await store.aclose()
