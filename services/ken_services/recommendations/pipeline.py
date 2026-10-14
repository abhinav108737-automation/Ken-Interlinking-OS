"""The recommendation job (``JobKind.RECOMMENDATION``, ``make recommend``; ADR-0011).

One run, through the generic job runner (advisory lock, counters, ``job.completed``)::

    load policies + taxonomy; resolve the embedding identity (optional provider)
    live facts of every page (+ eligibility, inbound link counts)
    for every source in scope, in page-id order, in ONE transaction per source:
        open plans on an OLDER source version: check_plan_freshness
            stale     -> transition to EXPIRED (system:recommendation-engine)
            rebasable -> unchanged (still occupies its pair and span)
        if the source is eligible (as_source):
            retrieve (channels) -> candidates
            pair stage (PairState -> pair_rejection), in retrieval order
            score (score_pair) -> below_min_relevance
            in rank order (relevance desc, target id):
                place (find_placement) -> no_natural_anchor
                final score (with_placement) -> below_min_score
                caps: link_density_exceeded, per_source_limit, diversity_limit
            INSERT kept plans (status proposed) + initial transition + link.proposed event
            INSERT every candidate (kept ones carry plan_id)
    replace opportunities (orphan / underlinked / link_capacity), one transaction
    -> succeeded | completed_with_errors

**Idempotent.** An OPEN plan (or a LIVE plan whose source version is not older than the
source's current version) occupies its pair, so re-running on unchanged pages proposes
nothing new: the pairs are ``duplicate_recommendation``. Open plans of the source also
count toward its per-source cap and link density, occupy their anchor spans
(``taken_spans``) and count as a link in their block (``max_links_per_block``).

**Live state only.** Eligibility is ``compute_eligibility`` over current facts with a fresh
analysis (current version AND current analysis version); protections are the ACTIVE ones;
``already_linked`` follows the source's CURRENT version's body links through ``known_urls``
(redirects and aliases count); inbound counts come from current versions of live pages.

**Scope.** By default every page is considered and ineligible sources are skipped
(``sources_skipped_ineligible``; their stale plans are still expired). Sources named
explicitly (``source_page_ids``) are always processed, so an ineligible one records its
candidates with ``source_protected`` / ``source_ineligible`` and gets no plan.

**Failure isolation.** A source that raises rolls back its own transaction only: it counts
``errors`` (logged with the exception CLASS only) and the run ends
``completed_with_errors``. Plans, transitions, events and candidates of one source commit
together or not at all.

Counters: ``sources_considered``, ``sources_skipped_ineligible``, ``candidates``,
``rejected_<code>`` for every ``CandidateRejection``, ``plans_created``, ``plans_expired``,
``opportunities``, ``errors``.
"""

from __future__ import annotations

import math
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.events import EventEnvelope, EventName
from ken_contracts.intelligence import (
    Intent,
    LinkEligibility,
    PageAnalysis,
    TaxonomyPolicy,
    analysis_version,
)
from ken_contracts.inventory import CanonicalStatus, PublicationState, RunStatus
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
    OpportunityKind,
    PlanFreshness,
    RankingPolicy,
    RecommendationPolicy,
    ScoreBreakdown,
)
from ken_core.config import REPO_ROOT, Settings
from ken_core.db.repositories import recommendations as repo
from ken_core.db.repositories.events import append_event
from ken_core.logging import get_logger
from ken_services.intelligence.eligibility import EligibilityState, compute_eligibility
from ken_services.intelligence.embeddings import current_recipe_fingerprint, embedding_identity
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import ContentBlock, extract_blocks
from ken_services.jobs.runner import JobContext, JobTransaction, run_job, utc_now
from ken_services.recommendations.channels import (
    EmbeddingKey,
    RetrievedCandidate,
    cosine_to,
    retrieve,
)
from ken_services.recommendations.filters import NoExperiments, PairState, pair_rejection
from ken_services.recommendations.freshness import check_plan_freshness
from ken_services.recommendations.placement import (
    PlacementCandidate,
    anchor_phrases,
    find_placement,
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

__all__ = [
    "EMITTED_BY",
    "RECOMMENDATION_COUNTERS",
    "RecommendationToolkit",
    "default_toolkit",
    "run_recommendations",
]

logger = get_logger("ken_services.recommendations.pipeline")

EMITTED_BY: Final = "ken-recommend"
"""``emitted_by`` of every ``link.proposed`` event."""
RECOMMENDATION_COUNTERS: Final = (
    "sources_considered",
    "sources_skipped_ineligible",
    "candidates",
    *(f"rejected_{code.value}" for code in CandidateRejection),
    "plans_created",
    "plans_expired",
    "opportunities",
    "errors",
)
_R = CandidateRejection


# --------------------------------------------------------------------------- #
# Injectable deterministic core (workstream A's modules by default)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RecommendationToolkit:
    """The pure functions the job calls. Tests inject fakes where their logic matters."""

    pair_rejection: Callable[..., CandidateRejection | None] = pair_rejection
    score_pair: Callable[..., ScoredPair] = score_pair
    with_placement: Callable[[ScoreBreakdown, float], ScoreBreakdown] = with_placement
    anchor_phrases: Callable[..., Sequence[Any]] = anchor_phrases
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


@dataclass(slots=True)
class _Ranked:
    candidate: RetrievedCandidate
    target: repo.PageFacts
    target_analysis: PageAnalysis
    scored: ScoredPair


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
    ) -> None:
        self.job = job
        self.settings = settings
        self.policy = policy
        self.ranking = ranking
        self.taxonomy = taxonomy
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
        analysis = facts.analysis
        return compute_eligibility(
            EligibilityState(
                publication_state=PublicationState(facts.publication_state),
                gone=facts.gone,
                protected=facts.protected,
                robots_directives=facts.robots_directives,
                canonical_status=CanonicalStatus(
                    facts.canonical_status or CanonicalStatus.SELF.value
                ),
                http_status=facts.http_status,
                word_count=facts.word_count,
                page_type=None if analysis is None else analysis.page_type,
                has_analysis=facts.has_any_analysis,
                analysis_fresh=analysis is not None and analysis.analysis_version == self.av,
            ),
            taxonomy=self.taxonomy,
        )

    async def _world(self) -> _World:
        async with self.job.sessionmaker() as session:
            facts = await repo.page_facts(session, analysis_version=self.av)
            inbound = await repo.inbound_link_counts(session)
        return _World(
            facts=facts,
            eligibility={pid: self._eligibility(f) for pid, f in facts.items()},
            inbound=inbound,
            av=self.av,
        )

    def _target_path_excluded(self, url: str) -> bool:
        path = urlsplit(url).path or "/"
        return any(re.search(pattern, path) for pattern in self.policy.excluded_target_paths)

    # ------------------------------------------------------------------ run
    async def run(self) -> RunStatus:
        job = self.job
        world = await self._world()
        async with job.sessionmaker() as session:
            self.per_target.update(await repo.open_plan_target_counts(session))

        # Deterministic order by canonical URL (then id): caps such as
        # max_plans_per_target_per_run are consumed in this order, so random page
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
        clean = job.counters.get("errors", 0) == 0
        return RunStatus.SUCCEEDED if clean else RunStatus.COMPLETED_WITH_ERRORS

    # --------------------------------------------------------------- source
    async def _source(self, world: _World, source: repo.PageFacts) -> None:
        eligible = world.eligibility[source.id].as_source
        async with self.job.transaction() as tx:
            session = tx.session
            body = await repo.version_body(
                session, page_id=source.id, version=source.current_version
            )
            blocks = tuple(self.tools.extract_blocks(body or ""))
            await self._expire_stale(tx, source, blocks)
            if not eligible and self.source_page_ids is None:
                tx.increment("sources_skipped_ineligible")
                return
            outcome = await self._generate(session, world, source, blocks)
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
        # Committed: only now does this source's output count toward diversity.
        for plan in outcome.plans:
            self.per_target[plan.target_page_id] += 1

    async def _expire_stale(
        self, tx: JobTransaction, source: repo.PageFacts, blocks: Sequence[ContentBlock]
    ) -> None:
        for plan in await repo.open_plans_for_source(tx.session, source_page_id=source.id):
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
        )

        # Occupancy: open plans (after expiry) and live, not-yet-re-read plans.
        open_plans = await repo.open_plans_for_source(session, source_page_id=source.id)
        occupied = {p.target_page_id for p in open_plans} | await repo.live_targets_at_or_after(
            session, source_page_id=source.id, source_version=source.current_version
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

        # ---- pair stage + relevance
        survivors: list[tuple[RetrievedCandidate, repo.PageFacts]] = []
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
            survivors.append((candidate, target))

        if not survivors:
            return outcome
        if source_analysis is None:  # pragma: no cover - the pair stage rejects every pair
            raise LookupError("source without analysis passed the pair stage")
        cosines: Mapping[uuid.UUID, float] = {}
        if self.embedding is not None:
            cosines = await cosine_to(
                session,
                source_page_id=source.id,
                target_page_ids=[t.id for _, t in survivors],
                key=self.embedding,
            )
        source_markets = Counter(
            market_of.get(link.target_page_id)
            for link in links
            if link.target_page_id is not None and link.target_page_id != source.id
        )
        ranked: list[_Ranked] = []
        for candidate, target in survivors:
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
                )
            )
        ranked.sort(key=lambda r: (-r.scored.breakdown.relevance, r.target.id))

        # ---- placement + final score + caps, in rank order
        salience = {b.block_id: float(b.score) for b in source_analysis.salient_blocks}
        selected = len(open_plans)
        now = self.job.clock()
        for item in ranked:
            candidate, target, scored = item.candidate, item.target, item.scored
            relevance = scored.breakdown.relevance
            phrases = self.tools.anchor_phrases(
                target=item.target_analysis,
                target_title=target.title,
                taxonomy=self.taxonomy,
                policy=self.policy,
                source=source_analysis,
            )
            placement = self.tools.find_placement(
                blocks=blocks,
                phrases=phrases,
                links_per_block=dict(links_per_block),
                blocks_linking_target=frozenset(blocks_linking.get(target.id, set())),
                taken_spans=frozenset(taken),
                salience=salience,
                policy=self.policy,
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
        if self.per_target[target_id] >= selection.max_plans_per_target_per_run:
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
        reason_codes = merge_reason_codes(
            scored.reason_codes,
            channel_reason_codes(item.candidate.channels),
            anchor_reason_code(placement.anchor_source),
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
            target_title=target.title,
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
                target_title=target.title,
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
        ).run()

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
