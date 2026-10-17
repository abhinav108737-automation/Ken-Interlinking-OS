"""SHADOW: re-run selection with a candidate policy beside production, writing NO plans.

ADR-0016 section 5: "After each real ``make recommend``, ``shadow.py`` re-runs selection with
the candidate **inside the same job**, writes **no** ``link_plans``, and records one
``policy_evaluations`` row (``mode=shadow``) holding the plan-set diff: pairs added, dropped,
re-ordered, and score deltas. Production output is unchanged."

**What this module writes.** Exactly one ``policy_evaluations`` row, on the CALLER's session
and transaction, with ``job_run_id`` = the recommendation run being shadowed (that is what
"inside the same job" means here: the evaluation hangs off the production job). It reads
``pages``, ``link_plans``, ``link_candidates`` and the analysis tables; it never inserts a plan,
a transition, a candidate or a ``link.proposed`` event. A test wraps the call in a row-count
assertion over every table the production pipeline writes.

**How the comparison is made.** The production plan set is read from the run's own
``link_candidates`` rows (kept rows carry ``plan_id``; their ``id`` order is the order the
engine selected them in). The candidate plan set is produced by a faithful re-statement of the
pipeline's per-source selection (pair filter -> score -> relevance floor -> placement -> final
score -> caps) using the CANDIDATE policy, over the live state with THIS run's own plans
excluded from occupancy, spans, per-source and per-target counts -- otherwise every production
pair would be a ``duplicate_recommendation`` and the candidate could only ever add. A test
proves the restatement is faithful: shadowing the baseline policy against itself reports a
zero diff. If the pipeline's selection changes, that test fails here first.

**It is not a gate.** A shadow row is always ``gate_result = inconclusive``. Promotion needs a
``pass`` on an ``offline`` run, so a shadow row can never be the evidence of a promotion; what
shadow adds is the plan-set diff a human reads and the ``shadow_min_runs`` / ``shadow_min_plans``
floors the gate policy declares for advancing past the stage.

**Claims.** The diff reports what changed in the plan set. It states nothing about whether the
candidate is better.
"""

from __future__ import annotations

import math
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final
from urllib.parse import urlsplit

from pydantic import Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.common import FrozenModel
from ken_contracts.intelligence import Intent, TaxonomyPolicy, analysis_version
from ken_contracts.learning import (
    EvalSuite,
    EvaluationMetrics,
    LearningError,
    LearningPolicy,
    ProposalStatus,
    SuiteMetrics,
    TargetKind,
)
from ken_contracts.recommendations import CandidateRejection, RankingPolicy, RecommendationPolicy
from ken_contracts.vectors import VectorStore
from ken_core.config import REPO_ROOT, Settings
from ken_core.db.models.recommendations import LinkCandidate, LinkPlan
from ken_core.db.repositories import learning as learning_repo
from ken_core.db.repositories import recommendations as repo
from ken_core.logging import get_logger
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import extract_blocks
from ken_services.recommendations.channels import (
    EmbeddingKey,
    analytics_window_for,
    cosine_to,
    retrieve,
)
from ken_services.recommendations.filters import PairState, pair_rejection
from ken_services.recommendations.generators import build_exact_index
from ken_services.recommendations.placement import anchor_phrases, find_placement, market_phrases
from ken_services.recommendations.policy import load_recommendation_policy, resolve_ranking_policy
from ken_services.recommendations.scoring import PairFeatures, score_pair, with_placement
from ken_services.recommendations.validity import page_eligibility

__all__ = [
    "MAX_LISTED",
    "RUNNER_VERSION",
    "PairDelta",
    "ShadowDiff",
    "ShadowRefusal",
    "ShadowRefusedError",
    "compute_diff",
    "shadow_pass",
]

logger = get_logger(__name__)

RUNNER_VERSION: Final = "shadow-1"
MAX_LISTED: Final = 200
"""Cap on every list in a stored diff, so one shadow row stays bounded; the counts are exact."""
_R = CandidateRejection
_SHADOWABLE: Final = (TargetKind.RANKING.value, TargetKind.RECOMMENDATION.value)
_OPEN_FOR_SHADOW: Final = (ProposalStatus.OFFLINE_TEST.value, ProposalStatus.SHADOW.value)


class ShadowRefusal(StrEnum):
    """Why a shadow pass was refused (fixed text, never exception text)."""

    NOT_A_RECOMMENDATION_RUN = "not_a_recommendation_run"
    NO_RECOMMENDATION_RUN = "no_recommendation_run"
    CANDIDATE_NOT_FOUND = "candidate_not_found"
    PROPOSAL_NOT_SHADOWABLE = "proposal_not_shadowable"
    TARGET_NOT_SHADOWABLE = "target_not_shadowable"
    BASELINE_CHANGED = "baseline_changed"
    """The policy files on disk are no longer the versions the run recorded."""


class ShadowRefusedError(LearningError):
    def __init__(self, refusal: ShadowRefusal) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal


# --------------------------------------------------------------------------- #
# The stored diff
# --------------------------------------------------------------------------- #
class PairDelta(FrozenModel):
    source_page_id: uuid.UUID
    target_page_id: uuid.UUID
    production_rank: int | None = Field(default=None, ge=0)
    candidate_rank: int | None = Field(default=None, ge=0)
    production_score: float | None = Field(default=None, ge=0.0, le=1.0)
    candidate_score: float | None = Field(default=None, ge=0.0, le=1.0)
    score_delta: float | None = None


class ShadowDiff(FrozenModel):
    """``policy_evaluations.metrics["shadow"]``: the plan-set diff. Observed differences only."""

    fingerprint: str
    candidate_version: str
    learning_policy_version: int
    recommendation_run_id: uuid.UUID
    sources_compared: int = Field(ge=0)
    production_plans: int = Field(ge=0)
    candidate_plans: int = Field(ge=0)
    compared_pairs: int = Field(ge=0, description="Size of the union of both plan sets.")
    unchanged: int = Field(ge=0)
    added: tuple[PairDelta, ...]
    dropped: tuple[PairDelta, ...]
    reordered: tuple[PairDelta, ...]
    score_deltas: tuple[PairDelta, ...]
    added_count: int = Field(ge=0)
    dropped_count: int = Field(ge=0)
    reordered_count: int = Field(ge=0)
    mean_abs_score_delta: float | None = None
    max_abs_score_delta: float | None = None
    truncated: bool = False
    note: str = (
        "Observed differences in the plan set under the candidate policy. "
        "Not a claim that the candidate is better."
    )


Selected = dict[tuple[uuid.UUID, uuid.UUID], tuple[int, float]]
"""(source, target) -> (rank within the source, final score)."""


def compute_diff(
    *,
    production: Mapping[tuple[uuid.UUID, uuid.UUID], tuple[int, float]],
    candidate: Mapping[tuple[uuid.UUID, uuid.UUID], tuple[int, float]],
    sources_compared: int,
    fingerprint: str,
    candidate_version: str,
    learning_policy_version: int,
    recommendation_run_id: uuid.UUID,
) -> ShadowDiff:
    """Pure: the plan-set diff between two selections. Deterministic ordering throughout."""
    union = sorted(set(production) | set(candidate), key=lambda p: (str(p[0]), str(p[1])))
    added: list[PairDelta] = []
    dropped: list[PairDelta] = []
    reordered: list[PairDelta] = []
    deltas: list[PairDelta] = []
    unchanged = 0
    for pair in union:
        before, after = production.get(pair), candidate.get(pair)
        delta = PairDelta(
            source_page_id=pair[0],
            target_page_id=pair[1],
            production_rank=None if before is None else before[0],
            candidate_rank=None if after is None else after[0],
            production_score=None if before is None else before[1],
            candidate_score=None if after is None else after[1],
            score_delta=None if before is None or after is None else round(after[1] - before[1], 6),
        )
        if before is None:
            added.append(delta)
        elif after is None:
            dropped.append(delta)
        else:
            if before[0] != after[0]:
                reordered.append(delta)
            else:
                unchanged += 1
            if delta.score_delta:
                deltas.append(delta)
    magnitudes = [abs(d.score_delta) for d in deltas if d.score_delta is not None]
    both = [p for p in union if p in production and p in candidate]
    mean = None
    if both:
        mean = round(sum(abs(candidate[p][1] - production[p][1]) for p in both) / len(both), 6)
    lists = (added, dropped, reordered, deltas)
    return ShadowDiff(
        fingerprint=fingerprint,
        candidate_version=candidate_version,
        learning_policy_version=learning_policy_version,
        recommendation_run_id=recommendation_run_id,
        sources_compared=sources_compared,
        production_plans=len(production),
        candidate_plans=len(candidate),
        compared_pairs=len(union),
        unchanged=unchanged,
        added=tuple(added[:MAX_LISTED]),
        dropped=tuple(dropped[:MAX_LISTED]),
        reordered=tuple(reordered[:MAX_LISTED]),
        score_deltas=tuple(deltas[:MAX_LISTED]),
        added_count=len(added),
        dropped_count=len(dropped),
        reordered_count=len(reordered),
        mean_abs_score_delta=mean,
        max_abs_score_delta=max(magnitudes) if magnitudes else None,
        truncated=any(len(items) > MAX_LISTED for items in lists),
    )


def _metrics_for(diff: ShadowDiff) -> dict[str, Any]:
    """``EvaluationMetrics`` (the shape the reader validates) plus the ``shadow`` object.

    One ``policy-regression`` suite row: a "case" is a (source, target) pair in either plan
    set and it "passes" when it is kept at the same rank. That is plan-set AGREEMENT, reported
    as a count; it is not a quality score and no gate reads it.
    """
    failed = diff.compared_pairs - diff.unchanged
    suite = SuiteMetrics(
        suite=EvalSuite.POLICY_REGRESSION,
        cases=diff.compared_pairs,
        passed=diff.unchanged,
        failed=failed,
        must_not_violations=0,
        pass_rate=None if diff.compared_pairs == 0 else diff.unchanged / diff.compared_pairs,
    )
    metrics = EvaluationMetrics(
        cases_total=diff.compared_pairs,
        cases_passed=diff.unchanged,
        by_suite=(suite,),
        by_decision_class={},
    )
    return {**metrics.model_dump(mode="json"), "shadow": diff.model_dump(mode="json")}


# --------------------------------------------------------------------------- #
# Reading the production run
# --------------------------------------------------------------------------- #
async def _run_snapshot(session: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    row = (
        await session.execute(
            text("SELECT kind, config_snapshot FROM job_runs WHERE id = :id"), {"id": run_id}
        )
    ).first()
    if row is None or row.kind != "recommendation":
        raise ShadowRefusedError(ShadowRefusal.NOT_A_RECOMMENDATION_RUN)
    snapshot = row.config_snapshot
    return dict(snapshot) if isinstance(snapshot, dict) else {}


async def _production_selection(session: AsyncSession, run_id: uuid.UUID) -> Selected:
    rows = (
        await session.execute(
            select(
                LinkCandidate.source_page_id,
                LinkCandidate.target_page_id,
                LinkCandidate.total_score,
            )
            .where(LinkCandidate.run_id == run_id, LinkCandidate.plan_id.is_not(None))
            .order_by(LinkCandidate.id)
        )
    ).all()
    rank: Counter[uuid.UUID] = Counter()
    out: Selected = {}
    for source_id, target_id, score in rows:
        out[(source_id, target_id)] = (rank[source_id], float(score or 0.0))
        rank[source_id] += 1
    return out


# --------------------------------------------------------------------------- #
# Candidate selection (a restatement of pipeline._Recommend._generate)
# --------------------------------------------------------------------------- #
def _intent(value: Any) -> Intent | None:
    return None if value is None else Intent(value)


async def _candidate_selection(
    session: AsyncSession,
    *,
    settings: Settings,
    policy: RecommendationPolicy,
    ranking: RankingPolicy,
    taxonomy: TaxonomyPolicy,
    embedding: EmbeddingKey | None,
    source_page_ids: Sequence[uuid.UUID] | None,
    exclude_run_id: uuid.UUID,
    locked_targets: frozenset[uuid.UUID],
    now: datetime,
    vector_store: VectorStore | None = None,
) -> tuple[Selected, int]:
    av = analysis_version(taxonomy.version)
    facts = await repo.page_facts(session, analysis_version=av)
    inbound = await repo.inbound_link_counts(session)
    eligibility = {pid: page_eligibility(f, taxonomy=taxonomy, analysis_version=av)
                   for pid, f in facts.items()}  # fmt: skip
    own_plans = {
        row[0]: (row[1], row[2])
        for row in (
            await session.execute(
                select(LinkPlan.id, LinkPlan.target_page_id, LinkPlan.status).where(
                    LinkPlan.run_id == exclude_run_id
                )
            )
        ).all()
    }
    per_target: Counter[uuid.UUID] = Counter(await repo.open_plan_target_counts(session))
    for target_id, status in own_plans.values():
        if status in repo.OPEN_VALUES:
            per_target[target_id] -= 1
    market_of = {
        pid: (f.analysis.market_id if f.analysis is not None else None) for pid, f in facts.items()
    }
    markets = market_phrases(taxonomy)
    selection = policy.selection

    def by_url(pid: uuid.UUID) -> tuple[str, str]:
        return (facts[pid].canonical_url, str(pid))

    if source_page_ids is None:
        scope = sorted(facts, key=by_url)
    else:
        scope = sorted((pid for pid in set(source_page_ids) if pid in facts), key=by_url)

    chosen: Selected = {}
    compared_sources = 0
    for source_id in scope:
        source = facts[source_id]
        if not eligibility[source_id].as_source and source_page_ids is None:
            continue
        source_analysis = source.analysis
        if source_analysis is None:
            continue
        compared_sources += 1
        body = await repo.version_body(session, page_id=source.id, version=source.current_version)
        blocks = tuple(extract_blocks(body or ""))
        retrieved = await retrieve(
            session,
            source_page_id=source.id,
            retrieval=policy.retrieval,
            embedding=embedding,
            keywords=source_analysis.keywords,
            industry_id=source_analysis.industry_id,
            analysis_version=av,
            vector_store=vector_store,
            semantic_floor=settings.similarity_min_score,
            analytics_window_days=analytics_window_for(settings, policy.retrieval),
        )
        open_plans = [
            plan
            for plan in await repo.open_plans_for_source(session, source_page_id=source.id)
            if plan.id not in own_plans
        ]
        occupied = {p.target_page_id for p in open_plans} | await repo.occupying_targets(
            session, source_page_id=source.id
        )
        since = now - timedelta(days=selection.rejection_cooldown_days)
        rejected = await repo.rejected_targets_since(session, source_page_id=source.id, since=since)
        links = await repo.version_links(session, page_id=source.id, version=source.current_version)
        linked_targets = {link.target_page_id for link in links if link.target_page_id}
        links_per_block: Counter[str] = Counter(link.block_id for link in links)
        blocks_linking: dict[uuid.UUID, set[str]] = defaultdict(set)
        for link in links:
            if link.target_page_id is not None:
                blocks_linking[link.target_page_id].add(link.block_id)
        by_hash = {sha256_hex(b.text): b.block_id for b in blocks}
        taken: set[tuple[str, int, int]] = set()
        for plan in open_plans:
            taken.add((plan.locator.context_hash, plan.locator.start, plan.locator.end))
            block_id = by_hash.get(plan.locator.context_hash)
            if block_id is not None:
                links_per_block[block_id] += 1
                blocks_linking[plan.target_page_id].add(block_id)

        survivors = []
        for candidate in retrieved:
            target = facts.get(candidate.target_page_id)
            if target is None:
                continue
            state = PairState(
                source_id=source.id,
                target_id=target.id,
                source_protected=source.protected,
                target_protected=target.protected,
                experiment_locked=target.id in locked_targets,
                source_eligibility=eligibility[source.id],
                target_eligibility=eligibility[target.id],
                already_linked=target.id in linked_targets,
                duplicate=target.id in occupied,
                previously_rejected=target.id in rejected,
                source_page_type=source_analysis.page_type,
                target_page_type=None if target.analysis is None else target.analysis.page_type,
                source_intent=_intent(source_analysis.intent),
                target_intent=None if target.analysis is None else _intent(target.analysis.intent),
                target_path_excluded=_path_excluded(policy, target.canonical_url),
            )
            if pair_rejection(state, policy=policy) is None:
                survivors.append((candidate, target))
        if not survivors:
            continue
        cosines: Mapping[uuid.UUID, float] = {}
        if embedding is not None:
            cosines = await cosine_to(
                session,
                source_page_id=source.id,
                target_page_ids=[t.id for _, t in survivors],
                key=embedding,
            )
        source_markets = Counter(
            market_of.get(link.target_page_id)
            for link in links
            if link.target_page_id is not None and link.target_page_id != source.id
        )
        ranked = []
        for candidate, target in survivors:
            target_analysis = target.analysis
            if target_analysis is None:
                continue
            market = target_analysis.market_id
            scored = score_pair(
                PairFeatures(
                    cosine=cosines.get(target.id),
                    source=source_analysis,
                    target=target_analysis,
                    source_links_into_target_market=0 if market is None else source_markets[market],
                    target_inbound_links=inbound.get(target.id, 0),
                ),
                policy=policy,
                ranking=ranking,
                taxonomy=taxonomy,
                semantic_floor=settings.similarity_min_score,
            )
            if scored.breakdown.relevance < selection.min_relevance:
                continue
            ranked.append((candidate, target, target_analysis, scored))
        ranked.sort(key=lambda r: (-r[3].breakdown.relevance, r[1].canonical_url, r[1].id))

        salience = {b.block_id: float(b.score) for b in source_analysis.salient_blocks}
        selected = len(open_plans)
        rank = 0
        for _candidate, target, target_analysis, scored in ranked:
            phrases = anchor_phrases(
                target=target_analysis,
                target_title=target.title,
                taxonomy=taxonomy,
                policy=policy,
                source=source_analysis,
            )
            placement = find_placement(
                blocks=blocks,
                phrases=phrases,
                links_per_block=dict(links_per_block),
                blocks_linking_target=frozenset(blocks_linking.get(target.id, set())),
                taken_spans=frozenset(taken),
                salience=salience,
                policy=policy,
                enclosing_phrases=markets,
            )
            if placement is None:
                continue
            final = with_placement(scored.breakdown, placement.placement_quality)
            if final.total < selection.min_score:
                continue
            if _cap(policy, source, selected, per_target[target.id]) is not None:
                continue
            chosen[(source.id, target.id)] = (rank, min(1.0, max(0.0, final.total)))
            rank += 1
            selected += 1
            per_target[target.id] += 1
            locator = placement.locator
            taken.add((locator.context_hash, locator.start, locator.end))
            links_per_block[locator.block_id] += 1
            blocks_linking[target.id].add(locator.block_id)
    return chosen, compared_sources


def _path_excluded(policy: RecommendationPolicy, url: str) -> bool:
    path = urlsplit(url).path or "/"
    return any(re.search(pattern, path) for pattern in policy.excluded_target_paths)


def _cap(
    policy: RecommendationPolicy, source: repo.PageFacts, selected: int, target_open: int
) -> CandidateRejection | None:
    selection = policy.selection
    words = source.word_count
    density = (
        math.inf if words <= 0 else ((source.internal_link_count + selected + 1) / words * 100.0)
    )
    if density > selection.max_internal_links_per_100_words:
        return _R.LINK_DENSITY_EXCEEDED
    if selected >= selection.max_plans_per_source:
        return _R.PER_SOURCE_LIMIT
    if target_open >= selection.max_open_plans_per_target:
        return _R.DIVERSITY_LIMIT
    return None


# --------------------------------------------------------------------------- #
# The entry point (protocol.ShadowPass)
# --------------------------------------------------------------------------- #
async def shadow_pass(
    session: AsyncSession,
    *,
    settings: Settings,
    policy: LearningPolicy,
    candidate_id: uuid.UUID,
    recommendation_run_id: uuid.UUID,
) -> uuid.UUID:
    """Shadow ``candidate_id`` against the plans of ``recommendation_run_id``.

    Runs on the caller's session and transaction and returns the new ``policy_evaluations``
    id. Writes no plan, no transition, no candidate row and no ``link.proposed`` event.

    Raises:
        ShadowRefusedError: the run is not a recommendation run, the candidate is unknown or
            not shadowable (``agents`` cannot be shadowed), the proposal is not in
            ``offline_test`` / ``shadow``, or the policy files on disk are no longer the
            versions the run recorded (the comparison would not be like for like).
    """
    started = datetime.now(UTC)
    candidate = await learning_repo.candidate_by_id(session, candidate_id)
    if candidate is None:
        raise ShadowRefusedError(ShadowRefusal.CANDIDATE_NOT_FOUND)
    if candidate.policy_kind not in _SHADOWABLE:
        raise ShadowRefusedError(ShadowRefusal.TARGET_NOT_SHADOWABLE)
    proposal = await learning_repo.proposal_by_id(session, candidate.proposal_id)
    if proposal is None or proposal.status not in _OPEN_FOR_SHADOW:
        raise ShadowRefusedError(ShadowRefusal.PROPOSAL_NOT_SHADOWABLE)

    snapshot = await _run_snapshot(session, recommendation_run_id)
    baseline_policy = load_recommendation_policy(settings.recommendation_policy_path)
    baseline_ranking = resolve_ranking_policy(baseline_policy, root=REPO_ROOT)
    recorded = (
        (snapshot.get("policy") or {}).get("version"),
        (snapshot.get("ranking_policy") or {}).get("version"),
    )
    if recorded != (baseline_policy.version, baseline_ranking.version):
        raise ShadowRefusedError(ShadowRefusal.BASELINE_CHANGED)

    document = dict(candidate.document)
    if candidate.policy_kind == TargetKind.RANKING.value:
        policy_under_test, ranking_under_test = (
            baseline_policy,
            RankingPolicy.model_validate(document),
        )
    else:
        policy_under_test = RecommendationPolicy.model_validate(document)
        ranking_under_test = baseline_ranking

    taxonomy = load_taxonomy(settings.taxonomy_path)
    raw_embedding = snapshot.get("embedding")
    embedding = (
        EmbeddingKey(
            model=str(raw_embedding["model"]),
            recipe_fingerprint=str(raw_embedding["recipe_fingerprint"]),
        )
        if isinstance(raw_embedding, dict)
        else None
    )
    raw_sources = snapshot.get("source_page_ids")
    source_ids = None if raw_sources is None else [uuid.UUID(str(s)) for s in raw_sources]

    production = await _production_selection(session, recommendation_run_id)
    locked = await learning_repo.live_holdout_control_targets(session)
    # ONE exact index for the whole pass, not one per source (ADR-0017): derived from
    # PostgreSQL, so it answers exactly as the production run's index does.
    shadow_store = (
        None if embedding is None else await build_exact_index(session, embedding.identity())
    )
    try:
        chosen, sources_compared = await _candidate_selection(
            session,
            settings=settings,
            policy=policy_under_test,
            ranking=ranking_under_test,
            taxonomy=taxonomy,
            embedding=embedding,
            source_page_ids=source_ids,
            exclude_run_id=recommendation_run_id,
            locked_targets=locked,
            now=started,
            vector_store=shadow_store,
        )
    finally:
        if shadow_store is not None:
            await shadow_store.aclose()
    diff = compute_diff(
        production=production,
        candidate=chosen,
        sources_compared=sources_compared,
        fingerprint=proposal.fingerprint,
        candidate_version=candidate.candidate_version,
        learning_policy_version=policy.version,
        recommendation_run_id=recommendation_run_id,
    )
    evaluation_id = uuid.uuid4()
    await learning_repo.insert_shadow_evaluation(
        session,
        evaluation_id=evaluation_id,
        job_run_id=recommendation_run_id,
        proposal_id=proposal.id,
        candidate_id=candidate.id,
        proposal_fingerprint=proposal.fingerprint,
        suites=[EvalSuite.POLICY_REGRESSION.value],
        metrics=_metrics_for(diff),
        gate_reasons=[],
        runner_version=RUNNER_VERSION,
        started_at=started,
        finished_at=datetime.now(UTC),
    )
    logger.info(
        "learning_shadow_recorded",
        evaluation_id=str(evaluation_id),
        proposal_id=str(proposal.id),
        added=diff.added_count,
        dropped=diff.dropped_count,
        reordered=diff.reordered_count,
    )
    return evaluation_id
