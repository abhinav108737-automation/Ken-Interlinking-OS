"""The suite scorers: one executor per :class:`~ken_contracts.learning.EvalSuite`.

**What makes these evals rather than tests.** Each executor drives the PRODUCTION decision
function for its suite and reduces the result to a short decision string from a fixed
vocabulary, which is then compared against the case's ``expected.decision``,
``acceptable_alternatives`` and ``must_not``. Nothing here reimplements a rule: if the
rule changes, the suite moves, which is the whole point of a gate.

===========================  ==============================================================
Suite                        The production code it drives
===========================  ==============================================================
``destination-selection``    ``filters.pair_rejection`` then ``scoring.score_pair`` under the
                             committed recommendation and ranking policies; the top-ranked
                             eligible destination is the decision.
``anchor-selection``         ``placement.anchor_phrases`` + ``placement.find_placement`` over
                             blocks parsed from the fixture's HTML by the production
                             normaliser, so a fabricated anchor is structurally impossible.
``relationship-detection``   ``relationships.taxonomy_relationships`` over the live taxonomy.
``critic``                   the REAL ``ken_agents.nodes.critique`` node, with the recorded
                             ``CriticAssessment`` replayed through ``MockModelProvider``.
``no-action``                the REAL ``ken_agents.nodes.triage`` node over an in-memory
                             :class:`~ken_services.agents.protocol.AgentBackend`, and the
                             selection thresholds for "zero plans rather than the least bad".
``protected-content``        ``pair_rejection`` (protection is checked third, before
                             eligibility), ``validity.plan_invalidity`` for the re-check at
                             VALIDATE, and the CMS adapter's own protected-page refusal.
``stale-page-version``       ``freshness.check_plan_freshness``, ``HtmlMutator.insert_links``
                             against the NEW body, and the CMS adapter's
                             ``expected_version`` precondition.
``policy-regression``        the same ranking path under a CANDIDATE weight vector, plus the
                             frozen-path boundary a candidate must not cross.
``reflection-grounding``     re-derives every number of a claimed
                             :class:`~ken_contracts.learning.DecisionClassObservation` from
                             the case's data snapshot. The anti-fabrication gate.
``proposal-safety``          ``PolicyChange`` / ``PolicyProposalBody`` (the frozen-field
                             boundary) and ``protocol.code_only_violations`` (the prompt
                             whitelist). The anti-injection gate.
===========================  ==============================================================

Everything is deterministic: no wall clock, no randomness, no network. The seed is recorded in
the report and used by nothing, because nothing here samples -- which is the honest way to
report it.
"""

from __future__ import annotations

import inspect
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ken_agents import nodes as agent_nodes
from ken_agents.llm import RoleModel
from ken_agents.recording import StepScratch
from ken_agents.state import AgentState
from ken_agents.tools import ToolBox
from ken_contracts.agents import (
    AgentPolicy,
    AgentTrigger,
    CriticAssessment,
    CriticVerdict,
)
from ken_contracts.autonomy import AutonomyMode, CriticOutcome, RiskTier
from ken_contracts.cms import (
    CMSNotFoundError,
    CMSPage,
    CMSValidationError,
    CMSVersionConflictError,
    LinkChange,
    LinkChangeRequest,
    LinkLocator,
    LinkOperation,
)
from ken_contracts.deployment import VerificationOutcome
from ken_contracts.intelligence import (
    LinkEligibility,
    PageAnalysis,
    RelationshipType,
    TaxonomyPolicy,
)
from ken_contracts.learning import (
    PROPOSER_PATTERN,
    DecisionClass,
    DecisionClassObservation,
    EvalCase,
    EvalSuite,
    PolicyChange,
    PolicyProposalBody,
    ProposalKind,
    SkillProposalBody,
)
from ken_contracts.model import ModelProvider, ModelRole
from ken_contracts.recommendations import (
    PlanFreshness,
    RankingPolicy,
    RecommendationPolicy,
)
from ken_core.config import Settings
from ken_core.providers.cms.mock import MockCMSProvider
from ken_services.agents.protocol import (
    AgentBackend,
    HandOffResult,
    PageSnapshot,
    PlanCheck,
    PlanReviewInput,
)
from ken_services.autonomy.blocks import DeterministicBlocks, evaluate_blocks
from ken_services.autonomy.budget import DatabaseRetryBudgetGate
from ken_services.autonomy.calibration import EvaluationCalibrationReader
from ken_services.autonomy.engine import (
    DatabaseAutonomyEngine,
    _Inputs,
    critic_outcome_of,
)
from ken_services.autonomy.facts import PlanIdentity
from ken_services.autonomy.policy import effective_retry, load_configured_autonomy_policy
from ken_services.autonomy.protocol import (
    CalibratedConfidence,
    PlanFacts,
    budget_from_attempts,
    target_status_for,
)
from ken_services.autonomy.risk import (
    DeterministicRiskClassifier,
    RiskSignals,
    raise_to,
    tier_from_signals,
)
from ken_services.deployment.changes import ChangeEngine, Located, insertion_for, locate
from ken_services.deployment.mutation import HtmlMutator
from ken_services.deployment.protocol import LinkInsertion
from ken_services.deployment.verification import HtmlLinkVerifier
from ken_services.intelligence.relationships import taxonomy_relationships
from ken_services.learning.fixtures import (
    FixtureError,
    PageFixture,
    blocks_of,
    context_hash,
    eval_uuid,
    link_plan_of,
    mapping_of,
    page_fixtures,
    require,
    sequence_of,
)
from ken_services.learning.protocol import code_only_violations
from ken_services.recommendations.filters import PairState, pair_rejection
from ken_services.recommendations.freshness import check_plan_freshness
from ken_services.recommendations.placement import anchor_phrases, find_placement, market_phrases
from ken_services.recommendations.scoring import PairFeatures, score_pair
from ken_services.recommendations.validity import plan_invalidity

__all__ = [
    "DECISION_NO_PLACEMENT",
    "SCORERS",
    "CaseOutcome",
    "RankingObservation",
    "ScoringContext",
    "score_case",
]

DECISION_NO_PLACEMENT: Final = "no_placement"
DECISION_NO_CANDIDATE: Final = "no_eligible_candidate"
_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RankingObservation:
    """A ranking case's predicted order against the RECORDED human-preferred order.

    Only cases that declare ``extra.preferred`` contribute to precision@1, recall@3 and
    Kendall tau: a suite-wide ranking metric computed over cases that never recorded a human
    preference would be a number about nothing.
    """

    predicted: tuple[str, ...]
    preferred: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    case_id: str
    suite: EvalSuite
    decision: str
    passed: bool
    must_not_hit: bool
    detail: str = ""
    decision_class: str | None = None
    confidence: float | None = None
    """RAW predicted confidence, where the case produces one. Feeds the reliability bins,
    which are REPORTED and never gating in Phase 8."""
    ranking: RankingObservation | None = None
    critic_observation: tuple[str, str] | None = None
    """``(predicted_verdict, expected_verdict)``, for critic precision and recall."""


@dataclass(frozen=True, slots=True)
class ScoringContext:
    """Everything a scorer may read. Built once per run, never per case."""

    settings: Settings
    taxonomy: TaxonomyPolicy
    recommendation: RecommendationPolicy
    ranking: RankingPolicy
    agents: AgentPolicy
    provider: ModelProvider
    provider_name: str
    seed: int
    cosines: dict[tuple[str, str], float] = field(default_factory=dict)
    cache: dict[str, Any] = field(default_factory=dict)
    """Per-run objects that are expensive or stateful to build (the unconnected
    sessionmaker, the autonomy engine). Never carried between runs."""

    async def cosine(self, source: PageFixture, target: PageFixture) -> float:
        """Cosine of the two pages' embeddings, through the configured EMBEDDING role.

        Not a recorded number: the fixture declares the TEXT and the provider embeds it, so
        ``--provider mock`` gives the deterministic feature-hashing vector and
        ``--provider ollama`` gives the local model's. A dataset that pinned a cosine would be
        pinning a model, which ``RecordedModelResponse`` exists to forbid.
        """
        key = (source.key, target.key)
        if key not in self.cosines:
            result = await self.provider.embed(
                texts=[source.embedding_text, target.embedding_text], role=ModelRole.EMBEDDING
            )
            a, b = result.vectors[0], result.vectors[1]
            norm_a = math.sqrt(math.fsum(x * x for x in a))
            norm_b = math.sqrt(math.fsum(x * x for x in b))
            value = (
                0.0
                if norm_a == 0.0 or norm_b == 0.0
                else math.fsum(x * y for x, y in zip(a, b, strict=True)) / (norm_a * norm_b)
            )
            self.cosines[key] = round(value, 9)
        return self.cosines[key]


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _extra(case: EvalCase) -> Mapping[str, Any]:
    return case.inputs.extra


def _keys(extra: Mapping[str, Any], name: str) -> tuple[str, ...]:
    return tuple(str(item) for item in sequence_of(extra.get(name, ()), name))


def _pair_state(
    *,
    source: PageFixture,
    target: PageFixture,
    taxonomy: TaxonomyPolicy,
    policy: RecommendationPolicy,
    already_linked: bool = False,
    duplicate: bool = False,
    previously_rejected: bool = False,
    experiment_locked: bool = False,
) -> PairState:
    source_eligibility: LinkEligibility = source.eligibility(taxonomy=taxonomy)
    target_eligibility: LinkEligibility = target.eligibility(taxonomy=taxonomy)
    excluded = any(pattern.search(target.path) for pattern in _compiled_excluded(policy))
    return PairState(
        source_id=source.page_id,
        target_id=target.page_id,
        source_protected=source.protected,
        target_protected=target.protected,
        experiment_locked=experiment_locked,
        source_eligibility=source_eligibility,
        target_eligibility=target_eligibility,
        already_linked=already_linked,
        duplicate=duplicate,
        previously_rejected=previously_rejected,
        source_page_type=source.analysis.page_type,
        target_page_type=target.analysis.page_type,
        source_intent=source.analysis.intent,
        target_intent=target.analysis.intent,
        target_path_excluded=excluded,
    )


_EXCLUDED_CACHE: dict[tuple[str, ...], tuple[re.Pattern[str], ...]] = {}


def _compiled_excluded(policy: RecommendationPolicy) -> tuple[re.Pattern[str], ...]:
    """``excluded_target_paths`` compiled once per distinct pattern tuple."""
    key = tuple(policy.excluded_target_paths)
    if key not in _EXCLUDED_CACHE:
        _EXCLUDED_CACHE[key] = tuple(re.compile(pattern) for pattern in key)
    return _EXCLUDED_CACHE[key]


async def _ranked(
    case: EvalCase, ctx: ScoringContext, *, ranking: RankingPolicy
) -> tuple[tuple[str, ...], dict[str, str], dict[str, float]]:
    """(eligible keys best first, rejected key -> rejection code, key -> relevance).

    This is the recommendation engine's own order: ``pair_rejection`` first (so protection and
    eligibility are decided before any score exists), then ``score_pair``, then the
    ``min_relevance`` floor, then relevance descending with the target key as a stable tiebreak.
    """
    extra = _extra(case)
    pages = page_fixtures(extra, taxonomy=ctx.taxonomy)
    source = pages[str(require(extra, "source"))]
    already = set(_keys(extra, "already_linked"))
    duplicates = set(_keys(extra, "open_plan_pairs"))
    rejected_before = set(_keys(extra, "previously_rejected"))
    locked = set(_keys(extra, "experiment_locked"))
    rejections: dict[str, str] = {}
    scored: list[tuple[float, str]] = []
    for key in _keys(extra, "candidates"):
        target = pages[key]
        state = _pair_state(
            source=source,
            target=target,
            taxonomy=ctx.taxonomy,
            policy=ctx.recommendation,
            already_linked=key in already,
            duplicate=key in duplicates,
            previously_rejected=key in rejected_before,
            experiment_locked=key in locked,
        )
        rejection = pair_rejection(state, policy=ctx.recommendation)
        if rejection is not None:
            rejections[key] = rejection.value
            continue
        pair = score_pair(
            PairFeatures(
                cosine=await ctx.cosine(source, target),
                source=source.analysis,
                target=target.analysis,
                source_links_into_target_market=source.links_into_market,
                target_inbound_links=target.facts.internal_link_count,
            ),
            policy=ctx.recommendation,
            ranking=ranking,
            taxonomy=ctx.taxonomy,
            semantic_floor=ctx.settings.similarity_min_score,
        )
        if pair.breakdown.relevance < ctx.recommendation.selection.min_relevance:
            rejections[key] = "below_min_relevance"
            continue
        scored.append((pair.breakdown.relevance, key))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return (
        tuple(key for _value, key in scored),
        rejections,
        {key: round(value, 9) for value, key in scored},
    )


# --------------------------------------------------------------------------- #
# destination-selection
# --------------------------------------------------------------------------- #
async def _score_destination_selection(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    order, rejections, relevance = await _ranked(case, ctx, ranking=ctx.ranking)
    cap = ctx.recommendation.selection.max_plans_per_source
    selected = order[:cap]
    decision = f"select:{order[0]}" if order else DECISION_NO_CANDIDATE
    preferred = _keys(_extra(case), "preferred")
    detail = "; ".join(
        [
            "order=" + ",".join(order),
            "rejected=" + ",".join(f"{k}:{v}" for k, v in sorted(rejections.items())),
        ]
    )
    return _verdict(
        case,
        decision=decision,
        detail=detail,
        selected={f"select:{key}" for key in selected} | {decision},
        confidence=relevance.get(order[0]) if order else None,
        ranking=(RankingObservation(predicted=order, preferred=preferred) if preferred else None),
    )


# --------------------------------------------------------------------------- #
# anchor-selection
# --------------------------------------------------------------------------- #
def _score_anchor_selection(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    extra = _extra(case)
    pages = page_fixtures(extra, taxonomy=ctx.taxonomy)
    source = pages[str(require(extra, "source"))]
    target = pages[str(require(extra, "target"))]
    blocks = source.blocks()
    phrases = anchor_phrases(
        target=target.analysis,
        target_title=target.title,
        taxonomy=ctx.taxonomy,
        policy=ctx.recommendation,
        source=source.analysis,
    )
    placement = find_placement(
        blocks=blocks,
        phrases=phrases,
        links_per_block={
            str(k): int(v)
            for k, v in mapping_of(extra.get("links_per_block", {}), "links_per_block").items()
        },
        blocks_linking_target=set(_keys(extra, "blocks_linking_target")),
        taken_spans=tuple(
            (context_hash(str(item[0])), int(item[1]), int(item[2]))
            for item in sequence_of(extra.get("taken_spans", ()), "taken_spans")
        ),
        salience={
            str(k): float(v) for k, v in mapping_of(extra.get("salience", {}), "salience").items()
        },
        policy=ctx.recommendation,
        enclosing_phrases=market_phrases(ctx.taxonomy),
    )
    if placement is None:
        return _verdict(
            case,
            decision=DECISION_NO_PLACEMENT,
            detail="phrases=" + ",".join(phrase.text for phrase in phrases),
        )
    block_text = next(b.text for b in blocks if b.block_id == placement.locator.block_id)
    verbatim = block_text[placement.locator.start : placement.locator.end] == placement.anchor_text
    decision = f"anchor:{placement.anchor_text}@{placement.locator.block_id}"
    return _verdict(
        case,
        decision=decision,
        detail=(
            f"source={placement.anchor_source} quality={placement.placement_quality} "
            f"verbatim={verbatim}"
        ),
        confidence=placement.placement_quality,
        extra_must_not=() if verbatim else ("fabricated_anchor",),
    )


# --------------------------------------------------------------------------- #
# relationship-detection
# --------------------------------------------------------------------------- #
def _score_relationship_detection(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    extra = _extra(case)
    pages = page_fixtures(extra, taxonomy=ctx.taxonomy)
    source = pages[str(require(extra, "source"))]
    target = pages[str(require(extra, "target"))]
    found = taxonomy_relationships(source.analysis, target.analysis, taxonomy=ctx.taxonomy)
    kinds = tuple(candidate.relationship_type.value for candidate in found)
    # The PRIMARY edge is the one a LinkPlan would record: strongest by weight, ties broken by
    # RelationshipType declaration order -- the exact rule `scoring.score_pair` uses, so this
    # suite measures the relationship a plan would carry and not a different notion of "first".
    order = {kind: index for index, kind in enumerate(RelationshipType)}
    strongest = min(found, key=lambda c: (-c.weight, order[c.relationship_type]), default=None)
    primary = strongest.relationship_type.value if strongest is not None else "none"
    return _verdict(
        case,
        decision=f"relationship:{primary}",
        detail="all=" + ",".join(kinds),
        selected={f"relationship:{kind}" for kind in kinds} or {"relationship:none"},
    )


# --------------------------------------------------------------------------- #
# critic -- the real node
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _EvalBackend:
    """An in-memory :class:`AgentBackend`: the five reads a node may do, and no hand-off.

    It implements the frozen protocol in full, so the nodes run EXACTLY as they do in
    production; ``hand_off`` raises, because an eval never writes a review row.
    """

    page: PageSnapshot | None
    analysis: PageAnalysis | None
    eligibility: LinkEligibility | None
    plans: list[Any] = field(default_factory=list)
    check: PlanCheck = field(default_factory=lambda: PlanCheck(ok=True))

    async def get_page(self, *, page_id: uuid.UUID) -> PageSnapshot | None:
        del page_id
        return self.page

    async def get_analysis(self, *, page_id: uuid.UUID) -> PageAnalysis | None:
        del page_id
        return self.analysis

    async def get_eligibility(self, *, page_id: uuid.UUID) -> LinkEligibility | None:
        del page_id
        return self.eligibility

    async def list_proposed_plans(self, *, source_page_id: uuid.UUID, limit: int) -> list[Any]:
        del source_page_id
        return self.plans[:limit]

    async def check_plan(self, *, plan_id: uuid.UUID) -> PlanCheck:
        del plan_id
        return self.check

    async def hand_off(
        self,
        *,
        agent_run_id: uuid.UUID,
        reviews: Sequence[PlanReviewInput],
        reviewed_at: datetime,
    ) -> HandOffResult:
        del agent_run_id, reviews, reviewed_at
        raise FixtureError("an eval never hands plans off")


def _snapshot(page: PageFixture) -> PageSnapshot:
    return PageSnapshot(
        id=page.page_id,
        canonical_url=page.canonical_url,
        title=page.title,
        current_version=page.current_version,
        publication_state=page.facts.publication_state,
        gone=page.facts.gone,
        word_count=page.facts.word_count,
        internal_link_count=page.facts.internal_link_count,
    )


def _toolbox(backend: AgentBackend, ctx: ScoringContext, run_id: uuid.UUID) -> ToolBox:
    return ToolBox(backend=backend, policy=ctx.agents, agent_run_id=run_id, clock=lambda: _EPOCH)


async def _score_critic(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    extra = _extra(case)
    pages = page_fixtures(extra, taxonomy=ctx.taxonomy)
    source = pages[str(require(extra, "source"))]
    plans = [
        link_plan_of(
            mapping_of(raw, "plans[]"),
            source=source,
            target=pages[str(mapping_of(raw, "plans[]")["target"])],
            plan_key=f"{case.case_id}-{index}",
        )
        for index, raw in enumerate(sequence_of(require(extra, "plans"), "plans"))
    ]
    run_id = eval_uuid("run", case.case_id)
    backend = _EvalBackend(
        page=_snapshot(source),
        analysis=source.analysis,
        eligibility=source.eligibility(taxonomy=ctx.taxonomy),
        plans=list(plans),
    )
    critic_model = _critic_role_model(case, ctx)
    policy = ctx.agents
    if "accept_min_confidence" in extra:
        policy = policy.model_copy(
            update={
                "critic": policy.critic.model_copy(
                    update={"accept_min_confidence": float(extra["accept_min_confidence"])}
                )
            }
        )
    if critic_model is None:
        policy = policy.model_copy(
            update={"critic": policy.critic.model_copy(update={"use_model": False})}
        )
    context = agent_nodes.NodeContext(
        tools=_toolbox(backend, ctx, run_id), policy=policy, planner=None, critic=critic_model
    )
    state = AgentState(
        run_id=run_id,
        source_page_id=source.page_id,
        trigger=AgentTrigger.MANUAL,
        source_page=backend.page,
        candidates=list(plans),
        ranked_candidates=[plan.id for plan in plans],
    )
    step = StepScratch()
    update = await agent_nodes.critique(state, context, step)
    reviews = update.critic_result
    if not reviews:
        return _verdict(case, decision="verdict:none", detail="no plan was offered")
    review = reviews[0]
    suffix = "+fallback" if review.fallback_reason is not None else ""
    decision = f"verdict:{review.verdict.value}{suffix}"
    expected_verdict = str(extra.get("human_verdict", "")) or None
    return _verdict(
        case,
        decision=decision,
        detail=(
            "reasons="
            + ",".join(reason.value for reason in review.reasons)
            + f" fallback={review.fallback_reason.value if review.fallback_reason else 'none'}"
        ),
        selected={decision, f"verdict:{review.verdict.value}"},
        confidence=review.confidence,
        critic_observation=((review.verdict.value, expected_verdict) if expected_verdict else None),
    )


def _critic_role_model(case: EvalCase, ctx: ScoringContext) -> RoleModel | None:
    """A :class:`RoleModel` over the run's provider when the case recorded a REASONING answer.

    A case with no recorded response gets ``None``, which is the documented
    ``MODEL_UNAVAILABLE`` path: the node falls back to its deterministic rule. That is the
    baseline a live ``--provider ollama`` run is compared with.
    """
    recorded = [
        item for item in case.inputs.recorded_model_responses if item.role is ModelRole.REASONING
    ]
    if not recorded:
        return None
    registered = getattr(ctx.provider, "register", None)
    if callable(registered):
        try:
            registered(CriticAssessment, recorded[0].response)
        except (ValidationError, ValueError, TypeError):
            # A deliberately malformed recorded response: script the violation instead, so the
            # node takes its SCHEMA_VIOLATION path rather than this scorer swallowing the case.
            violation = getattr(ctx.provider, "script_violation", None)
            if callable(violation):
                violation(CriticAssessment, raw_text="recorded response is not a valid assessment")
    return RoleModel(
        provider=ctx.provider,
        role=ModelRole.REASONING,
        timeout_seconds=ctx.agents.node_timeout_seconds,
    )


# --------------------------------------------------------------------------- #
# no-action
# --------------------------------------------------------------------------- #
async def _score_no_action(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    extra = _extra(case)
    check = str(extra.get("check", "triage"))
    if check == "triage":
        return await _score_triage(case, ctx)
    if check == "selection":
        order, rejections, relevance = await _ranked(case, ctx, ranking=ctx.ranking)
        floor = ctx.recommendation.selection.min_score
        kept = [key for key in order if relevance[key] >= floor]
        detail = "rejected=" + ",".join(f"{k}:{v}" for k, v in sorted(rejections.items()))
        return _verdict(case, decision=f"plans:{len(kept)}", detail=detail)
    raise FixtureError(f"no-action: unknown check {check!r}")


async def _score_triage(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    extra = _extra(case)
    pages = page_fixtures(extra, taxonomy=ctx.taxonomy)
    source = pages[str(require(extra, "source"))]
    has_analysis = bool(extra.get("analysis_available", True))
    has_eligibility = bool(extra.get("eligibility_available", True))
    plan_specs = sequence_of(extra.get("plans", ()), "plans")
    plans = [
        link_plan_of(
            mapping_of(raw, "plans[]"),
            source=source,
            target=pages[str(mapping_of(raw, "plans[]")["target"])],
            plan_key=f"{case.case_id}-{index}",
        )
        for index, raw in enumerate(plan_specs)
    ]
    backend = _EvalBackend(
        page=_snapshot(source),
        analysis=source.analysis if has_analysis else None,
        eligibility=source.eligibility(taxonomy=ctx.taxonomy) if has_eligibility else None,
        plans=list(plans),
    )
    run_id = eval_uuid("run", case.case_id)
    context = agent_nodes.NodeContext(
        tools=_toolbox(backend, ctx, run_id), policy=ctx.agents, planner=None, critic=None
    )
    state = AgentState(
        run_id=run_id,
        source_page_id=source.page_id,
        trigger=AgentTrigger.BATCH,
        source_page=backend.page,
    )
    update = await agent_nodes.triage(state, context, StepScratch())
    return _verdict(
        case,
        decision=f"triage:{update.triage_decision.value}",
        detail="notes=" + str(len(update.review_notes or [])),
    )


# --------------------------------------------------------------------------- #
# protected-content (hard fail)
# --------------------------------------------------------------------------- #
async def _score_protected_content(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    extra = _extra(case)
    check = str(extra.get("check", "pair"))
    pages = page_fixtures(extra, taxonomy=ctx.taxonomy)
    source = pages[str(require(extra, "source"))]
    if check == "pair":
        target = pages[str(require(extra, "target"))]
        state = _pair_state(
            source=source,
            target=target,
            taxonomy=ctx.taxonomy,
            policy=ctx.recommendation,
            already_linked=target.key in set(_keys(extra, "already_linked")),
            experiment_locked=target.key in set(_keys(extra, "experiment_locked")),
        )
        rejection = pair_rejection(state, policy=ctx.recommendation)
        decision = "allowed" if rejection is None else f"reject:{rejection.value}"
        return _verdict(
            case,
            decision=decision,
            detail=(
                f"source_protected={source.protected} target_protected={target.protected} "
                f"rules={'+'.join(source.protection_rules + target.protection_rules) or 'none'}"
            ),
        )
    if check == "revalidate":
        target = pages[str(require(extra, "target"))]
        reason = plan_invalidity(
            source_eligibility=source.eligibility(taxonomy=ctx.taxonomy),
            target_eligibility=target.eligibility(taxonomy=ctx.taxonomy),
            already_linked=bool(extra.get("already_linked_now", False)),
        )
        decision = "valid" if reason is None else f"invalid:{reason}"
        return _verdict(case, decision=decision, detail="re-checked at VALIDATE")
    if check == "cms":
        return await _score_cms(case, ctx, pages=pages, source=source)
    raise FixtureError(f"protected-content: unknown check {check!r}")


# --------------------------------------------------------------------------- #
# stale-page-version (hard fail)
# --------------------------------------------------------------------------- #
async def _score_stale_page_version(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    extra = _extra(case)
    check = str(extra.get("check", "freshness"))
    pages = page_fixtures(extra, taxonomy=ctx.taxonomy)
    source = pages[str(require(extra, "source"))]
    if check == "freshness":
        plan = link_plan_of(
            mapping_of(require(extra, "plan"), "plan"),
            source=source,
            target=pages[str(mapping_of(require(extra, "plan"), "plan")["target"])],
            plan_key=case.case_id,
        )
        current = blocks_of(str(require(extra, "body_html_after")))
        result = check_plan_freshness(
            plan_source_version=plan.source_page_version,
            current_version=int(extra.get("current_version_after", plan.source_page_version + 1)),
            locator=plan.locator,
            anchor_text=plan.anchor_text,
            current_blocks=current,
            excluded_ancestors=ctx.recommendation.placement.excluded_ancestors,
        )
        freshness = PlanFreshness(result.freshness)
        # The REASONS are part of the decision, not a note: "stale" is not an interesting
        # claim, while "stale because the block is gone" versus "stale because the anchor
        # moved" is the difference between two different regeneration paths.
        reasons = "+".join(result.reasons)
        decision = f"freshness:{freshness.value}" + (f":{reasons}" if reasons else "")
        return _verdict(
            case,
            decision=decision,
            detail=f"block={result.block_id or 'none'}",
            selected={decision, f"freshness:{freshness.value}"},
        )
    if check == "mutation":
        plan = link_plan_of(
            mapping_of(require(extra, "plan"), "plan"),
            source=source,
            target=pages[str(mapping_of(require(extra, "plan"), "plan")["target"])],
            plan_key=case.case_id,
        )
        after = str(require(extra, "body_html_after"))
        mutation = HtmlMutator().insert_links(
            after,
            [
                LinkInsertion(
                    plan_id=plan.id,
                    href=plan.target_url,
                    anchor_text=plan.anchor_text,
                    context_hash=plan.locator.context_hash,
                    block_id_hint=plan.locator.block_id,
                    occurrence_index=plan.locator.occurrence_index,
                    start=plan.locator.start,
                    end=plan.locator.end,
                )
            ],
            excluded_ancestors=ctx.recommendation.placement.excluded_ancestors,
        )
        if mutation.issues:
            codes = "+".join(sorted({issue.code.value for issue in mutation.issues}))
            return _verdict(
                case,
                decision=f"mutation:{codes}",
                detail=f"html_changed={mutation.html != after}",
            )
        if mutation.applied:
            return _verdict(case, decision="mutation:applied", detail="the link was spliced in")
        return _verdict(case, decision="mutation:already_applied", detail="nothing to do")
    if check == "cms":
        return await _score_cms(case, ctx, pages=pages, source=source)
    raise FixtureError(f"stale-page-version: unknown check {check!r}")


async def _score_cms(
    case: EvalCase, ctx: ScoringContext, *, pages: Mapping[str, PageFixture], source: PageFixture
) -> CaseOutcome:
    """The CMS adapter's own preconditions: protected page, then ``expected_version``.

    Driven through the mock CMS provider, which is the reference implementation of the frozen
    :class:`~ken_contracts.cms.CMSProvider` contract: it is in-process, deterministic and the
    thing the Phase 6 contract test pins, so what this case measures is the adapter's refusal
    and not a re-description of it.
    """
    extra = _extra(case)
    target = pages[str(require(extra, "target"))]
    body = str(extra.get("body_html_after") or source.body_html)
    live_version = str(extra.get("cms_version", "v1"))
    expected = str(extra.get("expected_version", live_version))
    block = next(iter(blocks_of(body)), None)
    anchor = str(extra.get("anchor_text", "")) or (block.text.split(" ")[0] if block else "x")
    provider = MockCMSProvider(
        [
            CMSPage(
                page_id=str(source.page_id),
                url=source.canonical_url,
                canonical_url=source.canonical_url,
                title=source.title or source.key,
                body_html=body,
                version=live_version,
                protected=source.protected,
            ),
            CMSPage(
                page_id=str(target.page_id),
                url=target.canonical_url,
                canonical_url=target.canonical_url,
                title=target.title or target.key,
                body_html=target.body_html or "<div><p>Target page.</p></div>",
                version="v1",
                protected=target.protected,
            ),
        ],
        clock=lambda: _EPOCH,
    )
    request = LinkChangeRequest(
        operation_id=eval_uuid("operation", case.case_id),
        source_page_id=str(source.page_id),
        expected_version=expected,
        actor=f"system:{ctx.provider_name}",
        changes=[
            LinkChange(
                change_id=eval_uuid("change", case.case_id),
                operation=LinkOperation.INSERT,
                target_page_id=str(target.page_id),
                href=target.canonical_url,
                anchor_text=anchor,
                locator=LinkLocator(
                    block_selector=block.block_id if block else "b0",
                    occurrence_index=0,
                    context_hash=context_hash(block.text) if block else None,
                ),
            )
        ],
        dry_run=bool(extra.get("dry_run", True)),
    )
    replays = int(extra.get("apply_times", 1))
    try:
        outcome = "cms:nothing"
        detail = ""
        for attempt in range(max(1, replays)):
            try:
                result = await provider.apply_link_changes(request=request)
            except CMSVersionConflictError:
                return _verdict(
                    case, decision="cms:version_conflict", detail="409: regenerate the plan"
                )
            except CMSValidationError as exc:
                codes = "+".join(sorted({issue.code for issue in exc.issues}))
                return _verdict(case, decision=f"cms:rejected:{codes}", detail="adapter refused")
            except CMSNotFoundError:
                return _verdict(case, decision="cms:not_found", detail="no such page")
            outcome = f"cms:{'replay' if result.idempotent_replay else result.status}"
            detail = f"attempt={attempt + 1} changes={len(result.applied_change_ids)}"
        return _verdict(case, decision=outcome, detail=detail)
    finally:
        await provider.aclose()


# --------------------------------------------------------------------------- #
# policy-regression
# --------------------------------------------------------------------------- #
async def _score_policy_regression(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    extra = _extra(case)
    check = str(extra.get("check", "candidate_ranking"))
    if check == "candidate_weights_refused":
        return _score_proposal_body(case, extra)
    candidate = ctx.ranking
    weights = mapping_of(extra.get("candidate_weights", {}), "candidate_weights")
    if weights:
        merged = dict(ctx.ranking.weights)
        merged.update({str(k): float(v) for k, v in weights.items()})
        try:
            candidate = ctx.ranking.model_copy(update={"weights": merged})
            RankingPolicy.model_validate(candidate.model_dump(mode="json"))
        except ValidationError as exc:
            return _verdict(
                case,
                decision="candidate:invalid",
                detail=f"{exc.error_count()} validation error(s)",
            )
    order, rejections, relevance = await _ranked(case, ctx, ranking=candidate)
    if check == "safety":
        decision = "blocked" if not order else f"select:{order[0]}"
        detail = "rejected=" + ",".join(f"{k}:{v}" for k, v in sorted(rejections.items()))
        return _verdict(case, decision=decision, detail=detail)
    preferred = _keys(extra, "preferred")
    decision = f"select:{order[0]}" if order else DECISION_NO_CANDIDATE
    agreement = "agrees" if preferred and order and order[0] == preferred[0] else "disagrees"
    return _verdict(
        case,
        decision=decision,
        detail=f"{agreement}; order=" + ",".join(order),
        confidence=relevance.get(order[0]) if order else None,
        ranking=RankingObservation(predicted=order, preferred=preferred) if preferred else None,
    )


# --------------------------------------------------------------------------- #
# reflection-grounding (hard fail): the anti-fabrication gate
# --------------------------------------------------------------------------- #
def _rederive(
    snapshot: Sequence[Mapping[str, Any]], *, min_sample: int
) -> dict[str, dict[str, Any]]:
    """Re-derive the per-decision-class counts and rates from the raw snapshot rows.

    One row = one reviewed plan: its decision class, the editorial action, whether the
    deployment was later rolled back and whether the measured outcome was non-negative. Every
    number a proposal's evidence may carry is a count or a ratio of counts over these rows, so
    a claim that does not re-derive here was not computed from the data.
    """
    buckets: dict[str, dict[str, int]] = {}
    for row in snapshot:
        klass = DecisionClass(
            relationship_type=row.get("relationship_type"),
            anchor_source=row["anchor_source"],
            channels=tuple(row["channels"]),
        ).canonical
        bucket = buckets.setdefault(
            klass,
            {
                "sample_count": 0,
                "approvals": 0,
                "edits": 0,
                "rejections": 0,
                "rollbacks": 0,
                "positive_outcomes": 0,
            },
        )
        bucket["sample_count"] += 1
        action = str(row.get("action", ""))
        if action == "approve":
            bucket["approvals"] += 1
        elif action == "reject":
            bucket["rejections"] += 1
        elif action in ("edit_anchor", "replace_target"):
            bucket["edits"] += 1
            bucket["approvals"] += 1
        if row.get("rolled_back"):
            bucket["rollbacks"] += 1
        if row.get("positive_outcome"):
            bucket["positive_outcomes"] += 1
    out: dict[str, dict[str, Any]] = {}
    for klass, counts in buckets.items():
        n = counts["sample_count"]
        out[klass] = {
            **counts,
            "approval_rate": None if n == 0 else round(counts["approvals"] / n, 6),
            "edit_rate": None if n == 0 else round(counts["edits"] / n, 6),
            "reject_rate": None if n == 0 else round(counts["rejections"] / n, 6),
            "rollback_rate": None if n == 0 else round(counts["rollbacks"] / n, 6),
            "positive_outcome_rate": None if n == 0 else round(counts["positive_outcomes"] / n, 6),
            "sufficient": n >= min_sample,
        }
    return out


_GROUNDED_FIELDS: Final = (
    "sample_count",
    "approvals",
    "edits",
    "rejections",
    "rollbacks",
    "positive_outcomes",
    "approval_rate",
    "edit_rate",
    "reject_rate",
    "rollback_rate",
    "positive_outcome_rate",
    "sufficient",
)


def _score_reflection_grounding(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    extra = _extra(case)
    snapshot = [
        mapping_of(row, "snapshot[]") for row in sequence_of(require(extra, "snapshot"), "snapshot")
    ]
    truth = _rederive(snapshot, min_sample=int(extra.get("min_sample_count", 30)))
    claimed_raw = sequence_of(require(extra, "claimed"), "claimed")
    claims: list[DecisionClassObservation] = []
    for raw in claimed_raw:
        try:
            claims.append(DecisionClassObservation.model_validate(mapping_of(raw, "claimed[]")))
        except ValidationError as exc:
            return _verdict(
                case,
                decision="not_reproducible:invalid_observation",
                detail=f"{exc.error_count()} validation error(s)",
            )
    for claim in claims:
        if claim.decision_class not in truth:
            return _verdict(
                case,
                decision=f"not_reproducible:absent_class:{claim.decision_class}",
                detail="the claimed class is not in the snapshot at all",
            )
        derived = truth[claim.decision_class]
        for name in _GROUNDED_FIELDS:
            claimed_value = getattr(claim, name)
            expected_value = derived[name]
            if isinstance(claimed_value, float) and isinstance(expected_value, float):
                if abs(claimed_value - expected_value) > 1e-6:
                    return _verdict(
                        case,
                        decision=f"not_reproducible:{claim.decision_class}:{name}",
                        detail=f"claimed={claimed_value} derived={expected_value}",
                    )
            elif claimed_value != expected_value:
                return _verdict(
                    case,
                    decision=f"not_reproducible:{claim.decision_class}:{name}",
                    detail=f"claimed={claimed_value!r} derived={expected_value!r}",
                )
    if bool(extra.get("require_every_class", False)):
        missing = sorted(set(truth) - {claim.decision_class for claim in claims})
        if missing:
            return _verdict(
                case,
                decision=f"not_reproducible:omitted_class:{missing[0]}",
                detail="the snapshot holds a class the evidence silently drops",
            )
    total = int(extra.get("claimed_sample_count_total", -1))
    if total >= 0 and total != sum(claim.sample_count for claim in claims):
        return _verdict(
            case,
            decision="not_reproducible:sample_count_total",
            detail=f"claimed={total} derived={sum(c.sample_count for c in claims)}",
        )
    return _verdict(
        case, decision="rederives", detail=f"classes={len(claims)} rows={len(snapshot)}"
    )


# --------------------------------------------------------------------------- #
# proposal-safety (hard fail): the anti-injection gate
# --------------------------------------------------------------------------- #
_PROPOSER: Final = re.compile(PROPOSER_PATTERN)
"""The frozen ``learning_proposals.proposed_by`` shape. A proposal can never claim a human
author -- which is why this suite checks the pattern and not a hand-written regex."""


def _score_proposal_body(case: EvalCase, extra: Mapping[str, Any]) -> CaseOutcome:
    """Build the proposal body the case describes. A refusal is the PASSING outcome."""
    raw = mapping_of(require(extra, "proposal"), "proposal")
    kind = str(raw.get("kind", ProposalKind.POLICY.value))
    try:
        if kind == ProposalKind.SKILL.value:
            SkillProposalBody.model_validate(dict(raw))
        else:
            changes = [
                PolicyChange.model_validate(dict(mapping_of(item, "changes[]")))
                for item in sequence_of(raw.get("changes", ()), "changes")
            ]
            PolicyProposalBody.model_validate({**dict(raw), "changes": changes})
    except ValidationError as exc:
        return _verdict(
            case,
            decision=f"refused:{_refusal_of(exc)}",
            detail=f"{exc.error_count()} validation error(s)",
        )
    return _verdict(case, decision="accepted", detail="the proposal body validated")


def _refusal_of(exc: ValidationError) -> str:
    """A fixed refusal code from the contract's own message, never the message itself."""
    messages = " ".join(str(error.get("msg", "")) for error in exc.errors())
    if "frozen field" in messages:
        return "frozen_path"
    if "change allowlist" in messages:
        return "not_allowlisted"
    if "cross_check" in messages or "unloadable" in messages:
        return "cross_check"
    if "does not belong to the" in messages:
        return "wrong_target_kind"
    if "must change something" in messages:
        return "no_change"
    return "invalid"


def _score_proposal_safety(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    del ctx
    extra = _extra(case)
    check = str(extra.get("check", "proposal"))
    if check == "proposal":
        return _score_proposal_body(case, extra)
    if check == "prompt_payload":
        violations = code_only_violations(extra.get("payload"))
        if violations:
            return _verdict(
                case,
                decision="refused:not_code",
                detail=f"{len(violations)} non-code field(s): {violations[0]}",
            )
        return _verdict(case, decision="payload_accepted", detail="every field is a code token")
    if check == "proposer":
        actor = str(require(extra, "proposed_by"))
        if _PROPOSER.fullmatch(actor) is None:
            return _verdict(
                case,
                decision="refused:non_system_proposer",
                detail="a proposal can never claim a human author",
            )
        return _verdict(case, decision="proposer_accepted", detail="system or agent actor")
    raise FixtureError(f"proposal-safety: unknown check {check!r}")


# --------------------------------------------------------------------------- #
# Verdict assembly
# --------------------------------------------------------------------------- #
def _verdict(
    case: EvalCase,
    *,
    decision: str,
    detail: str = "",
    selected: set[str] | None = None,
    confidence: float | None = None,
    ranking: RankingObservation | None = None,
    critic_observation: tuple[str, str] | None = None,
    extra_must_not: tuple[str, ...] = (),
) -> CaseOutcome:
    """Compare the produced decision with the case's expectation.

    ``selected`` is every decision the run actually made, when a case can produce more than
    one (a ranking case proposes up to ``max_plans_per_source`` destinations). A ``must_not``
    entry hit by ANY of them is a zero-tolerance violation, which is stricter than only
    checking the top decision -- and strictness is the right default for a safety list.
    """
    produced = selected if selected is not None else {decision}
    must_not = set(case.expected.must_not)
    hit = bool(produced & must_not) or bool(set(extra_must_not) & must_not)
    passing = {case.expected.decision, *case.expected.acceptable_alternatives}
    passed = decision in passing and not hit
    return CaseOutcome(
        case_id=case.case_id,
        suite=case.suite,
        decision=decision,
        passed=passed,
        must_not_hit=hit,
        detail=detail[:400],
        decision_class=case.decision_class.canonical if case.decision_class else None,
        confidence=confidence,
        ranking=ranking,
        critic_observation=critic_observation,
    )


# --------------------------------------------------------------------------- #
# rollback-correctness (hard fail): ADR-0014 section 8 + ADR-0018 section 4
# --------------------------------------------------------------------------- #
_UNRELATED_PARAGRAPH: Final = "<p>An unrelated paragraph an editor added after the deployment.</p>"
_EDITOR_ANCHOR: Final = "EV market"


def _append_unrelated(html: str) -> str:
    head, sep, tail = html.rpartition("</div>")
    return f"{head}{_UNRELATED_PARAGRAPH}{sep}{tail}" if sep else html + _UNRELATED_PARAGRAPH


def _after_apply_edit(kind: str, html: str, *, href: str, anchor: str, link_id: str) -> str:
    """What a human editor (or a CMS post-processor) does to the page AFTER our deployment.

    Each edit is deterministic string surgery on the exact markup the production mutator
    emits, and a no-op edit is a fixture error: a case that claims an editor changed the link
    when nothing changed would pass for the wrong reason.
    """
    if kind == "unrelated_edit":
        return _append_unrelated(html)
    if kind == "editor_removed_link":
        pattern = rf'<a[^>]*data-ken-link-id="{re.escape(link_id)}"[^>]*>(.*?)</a>'
        return re.sub(pattern, r"\1", html, count=1)
    if kind == "editor_changed_href":
        return html.replace(f'href="{href}"', f'href="{href}-moved"', 1)
    if kind == "editor_changed_anchor":
        return html.replace(f">{anchor}</a>", f">{_EDITOR_ANCHOR}</a>", 1)
    if kind == "hidden_attribute":
        return html.replace(f'<a href="{href}"', f'<a hidden href="{href}"', 1)
    raise FixtureError(f"rollback-correctness: unknown after_apply {kind!r}")


def _offline_sessionmaker(ctx: ScoringContext) -> async_sessionmaker[AsyncSession]:
    """A sessionmaker over an engine that is NEVER connected.

    The autonomy classes want one in their constructors, and the decision logic exercised
    here is pure: no scorer opens a session. Creating an async engine does not connect, and
    the address is the discard port on loopback, so even a bug that tried to use it would fail
    rather than reach a real database.
    """
    cached = ctx.cache.get("offline_sessionmaker")
    if cached is None:
        engine = create_async_engine("postgresql+asyncpg://eval:eval@127.0.0.1:9/eval-offline")
        cached = async_sessionmaker(engine, expire_on_commit=False)
        ctx.cache["offline_sessionmaker"] = cached
    return cast("async_sessionmaker[AsyncSession]", cached)


async def _score_rollback_correctness(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    check = str(_extra(case).get("check", "deployment"))
    if check == "deployment":
        return await _score_rollback_deployment(case, ctx)
    if check == "budget":
        return _score_retry_budget(case, ctx)
    raise FixtureError(f"rollback-correctness: unknown check {check!r}")


async def _score_rollback_deployment(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    """Apply a link through the real engine and mock CMS, optionally verify and edit the page,
    roll back, and report WHAT STATE THE PAGE IS LEFT IN.

    The decision is ``[verify_failed>]<rollback statuses>:<page state>``. The page state is
    compared byte for byte against the pre-deployment HTML (``original``), against that HTML
    with the same later edit applied (``original_with_later_edit``), or against the page as it
    stood just before the rollback (``page_untouched``, which is what a refused rollback must
    leave).

    DB-bound parts NOT covered here (they are exercised by ``tests/integration``): the
    deployment SERVICE's own mapping of a CMS error onto ``rollback_conflict`` /
    ``nothing_to_roll_back`` (``deployment/service.py:1862-1871``) is mirrored by the two
    ``except`` clauses below, and its "no live plans" refusal (``service.py:1699``) is not
    reproducible without plan rows.
    """
    extra = _extra(case)
    pages = page_fixtures(extra, taxonomy=ctx.taxonomy)
    source = pages[str(require(extra, "source"))]
    target = pages[str(require(extra, "target"))]
    original = source.body_html
    anchor = str(extra.get("anchor_text", "electric vehicle"))
    block_id = str(extra.get("block_id", "b1"))
    block = next((b for b in blocks_of(original) if b.block_id == block_id), None)
    if block is None:
        raise FixtureError(f"the source has no block {block_id!r}")
    change = LinkChange(
        change_id=eval_uuid("change", case.case_id),
        operation=LinkOperation.INSERT,
        target_page_id=str(target.page_id),
        href=target.canonical_url,
        anchor_text=anchor,
        locator=LinkLocator(
            block_selector=block_id,
            occurrence_index=int(extra.get("occurrence_index", 0)),
            context_hash=context_hash(block.text),
        ),
    )
    operation_id = eval_uuid("operation", case.case_id)
    source_id = str(source.page_id)
    provider = MockCMSProvider(
        [
            CMSPage(
                page_id=source_id,
                url=source.canonical_url,
                canonical_url=source.canonical_url,
                title=source.title or source.key,
                body_html=original,
                version="v1",
            ),
            CMSPage(
                page_id=str(target.page_id),
                url=target.canonical_url,
                canonical_url=target.canonical_url,
                title=target.title or target.key,
                body_html=target.body_html or "<div><p>Target page.</p></div>",
                version="v1",
            ),
        ],
        clock=lambda: _EPOCH,
        html_engine=ChangeEngine(
            HtmlMutator(), excluded_ancestors=ctx.recommendation.placement.excluded_ancestors
        ),
    )

    async def apply(operation: uuid.UUID, change_id: uuid.UUID) -> Any:
        page = await provider.get_page(page_id=source_id)
        try:
            return await provider.apply_link_changes(
                request=LinkChangeRequest(
                    operation_id=operation,
                    source_page_id=source_id,
                    expected_version=page.version,
                    changes=[change.model_copy(update={"change_id": change_id})],
                    actor="system:eval",
                )
            )
        except (CMSValidationError, CMSVersionConflictError) as exc:
            raise FixtureError("the fixture's own deployment was refused") from exc

    try:
        if bool(extra.get("already_deployed_first", False)):
            await apply(eval_uuid("operation", f"{case.case_id}:first"), change.change_id)
        deployment = await apply(operation_id, change.change_id)
        after_apply = (await provider.get_page(page_id=source_id)).body_html
        kind = str(extra.get("after_apply", "none"))

        def edit(html: str) -> str:
            if kind == "none":
                return html
            return _after_apply_edit(
                kind,
                html,
                href=target.canonical_url,
                anchor=anchor,
                link_id=str(change.change_id),
            )

        if kind != "none":
            edited = edit(after_apply)
            if edited == after_apply:
                raise FixtureError("the after_apply edit changed nothing; the case is vacuous")
            provider.edit_page(page_id=source_id, body_html=edited)
        live = (await provider.get_page(page_id=source_id)).body_html

        prefix = ""
        if str(extra.get("rollback_on", "always")) == "verification_failure":
            insertion = insertion_for(change, _located(original, change))
            checks = HtmlLinkVerifier().verify_links(
                live,
                [insertion],
                target_reachable={change.change_id: bool(extra.get("target_reachable", True))},
            )
            failed = [c.check.value for c in checks if c.outcome is VerificationOutcome.FAILED]
            if not failed:
                return _verdict(
                    case,
                    decision="verify_passed:kept",
                    detail="verification passed, so nothing is rolled back",
                )
            prefix = "verify_failed>"

        deployment_id = (
            str(deployment.deployment_id)
            if bool(extra.get("deployment_known", True))
            else "dep-that-was-never-created"
        )
        statuses: list[str] = []
        for _ in range(max(1, int(extra.get("rollback_times", 1)))):
            try:
                result = await provider.rollback(
                    deployment_id=deployment_id, operation_id=operation_id, actor="system:eval"
                )
            except CMSValidationError:
                statuses.append("rollback_conflict")
                break
            except CMSNotFoundError:
                statuses.append("nothing_to_roll_back")
                break
            statuses.append(str(result.status))
        final = (await provider.get_page(page_id=source_id)).body_html
    finally:
        await provider.aclose()

    if final == original:
        state = "original"
    elif kind == "unrelated_edit" and final == edit(original):
        state = "original_with_later_edit"
    elif final == live:
        state = "page_untouched"
    else:
        state = "other"
    decision = f"{prefix}{'>'.join(statuses)}:{state}"
    return _verdict(
        case,
        decision=decision,
        detail=f"link_still_present={'data-ken-link-id' in final}",
        selected={decision, *statuses, state},
    )


def _located(html: str, change: LinkChange) -> Located:
    found = locate(html, change)
    if isinstance(found, Located):
        return found
    raise FixtureError("the fixture's change cannot be located in the original page")


def _score_retry_budget(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    """The REAL ``DatabaseRetryBudgetGate.verdict`` over budget states built by the REAL
    ``budget_from_attempts``. Each step is one (source, target) pair's recorded counts.

    NOT covered here: the SQL that COUNTS ``publish_attempts`` per pair
    (``repo.budget_counts``) and the append in the deployment-intent transaction -- those are
    database behaviour and belong to ``tests/integration``. What this measures is the rule
    applied to those counts, which is the part that must be identical everywhere.
    """
    extra = _extra(case)
    max_attempts = int(extra.get("max_attempts", 3))
    settings = ctx.settings.model_copy(update={"autonomy_publish_max_attempts": max_attempts})
    # The gate takes the EFFECTIVE retry budget, exactly as the runtime builds it: the
    # versioned autonomy policy tightened (never loosened) by the environment. The case's
    # `max_attempts` reaches it through `settings`, so a case may only make the budget
    # stricter than the policy -- which is the only thing the runtime can do either.
    gate = DatabaseRetryBudgetGate(
        sessionmaker=_offline_sessionmaker(ctx),
        retry=effective_retry(load_configured_autonomy_policy(ctx.settings), settings),
        clock=lambda: _EPOCH,
    )
    outcomes: list[str] = []
    for raw in sequence_of(require(extra, "steps"), "steps"):
        step = mapping_of(raw, "steps[]")
        remaining = step.get("cooldown_seconds_remaining")
        state = budget_from_attempts(
            source_page_id=eval_uuid("page", str(require(step, "source"))),
            target_page_id=eval_uuid("page", str(require(step, "target"))),
            attempts=int(step.get("attempts", 0)),
            failures=int(step.get("failures", 0)),
            max_attempts=max_attempts,
            cooldown_until=(
                None if remaining is None else _EPOCH + timedelta(seconds=float(remaining))
            ),
            last_attempt_at=None,
        )
        verdict = gate.verdict(state)
        outcomes.append("allowed" if verdict.allowed else "refused:" + "+".join(verdict.reasons))
    decision = ",".join(outcomes)
    return _verdict(case, decision=decision, selected={decision, *outcomes})


# --------------------------------------------------------------------------- #
# autonomy-classification (hard fail): ADR-0018 sections 1-3
# --------------------------------------------------------------------------- #
_CLEAN_FACTS: Final[Mapping[str, Any]] = {
    "source_page_version": 3,
    "source_current_version": 3,
    "target_page_version": 2,
    "target_current_version": 2,
    "source_protected": False,
    "target_protected": False,
    "target_published": True,
    "target_gone": False,
    "target_http_status": 200,
    "target_canonical_ok": True,
    "target_redirect_supported": True,
    "target_noindex": False,
    "target_thin": False,
    "source_page_type": "insight",
    "target_page_type": "report",
    "page_types_compatible": True,
    "destination_internal": True,
    "destination_allowed_by_policy": True,
    "already_linked": False,
    "duplicate_allowed": False,
    "anchor_valid": True,
    "rollback_metadata_present": True,
    "vector_fresh": True,
    "policy_version": 1,
    "confidence": 0.8,
    "calibration_version": "eval-cal-1",
}
"""A plan on which NO block code fires. Every autonomy case starts here and overrides only
what it is about, so a case can be read as a diff from 'nothing is wrong'."""

_RISK_SIGNAL_FIELDS: Final = (
    "commercial_page",
    "hub_destination",
    "protection_nearby",
    "sensitive_anchor",
    "prior_failure",
    "measured_rollback",
    "stale_target_version",
)


def _autonomy_engine(ctx: ScoringContext, *, default_profile: str) -> DatabaseAutonomyEngine:
    """The REAL engine over an unconnected sessionmaker (see :func:`_offline_sessionmaker`).

    Only its pure methods are called (``_assemble``, ``_decide``, ``_target_status``): they
    take already-gathered inputs and open no session, which is why the case supplies the
    facts the SQL snapshot (``load_plan_facts``) would have produced.
    """
    key = f"autonomy_engine:{default_profile}"
    cached = ctx.cache.get(key)
    if cached is None:
        sessionmaker = _offline_sessionmaker(ctx)
        settings = ctx.settings.model_copy(
            update={"autonomy_verification_profile": default_profile}
        )
        # The versioned autonomy policy every floor comes from. Loaded from the configured
        # path, so the eval measures the policy that is actually in force.
        autonomy_policy = load_configured_autonomy_policy(ctx.settings)
        blocks = DeterministicBlocks(
            sessionmaker=sessionmaker,
            settings=settings,
            policy=ctx.recommendation,
            taxonomy=ctx.taxonomy,
            autonomy_policy=autonomy_policy,
            clock=lambda: _EPOCH,
        )
        cached = DatabaseAutonomyEngine(
            sessionmaker=sessionmaker,
            settings=settings,
            policy=ctx.recommendation,
            taxonomy=ctx.taxonomy,
            autonomy_policy=autonomy_policy,
            blocks=blocks,
            risk=DeterministicRiskClassifier(
                sessionmaker=sessionmaker,
                taxonomy=ctx.taxonomy,
                clock=lambda: _EPOCH,
                policy=autonomy_policy.risk,
            ),
            # `policy=None`: no learning policy offline, so every class reads UNCALIBRATED,
            # which is the conservative side and keeps the report independent of what
            # happens to be on disk. The floor still comes from the autonomy policy.
            calibration=EvaluationCalibrationReader(
                sessionmaker=sessionmaker,
                policy=None,
                clock=lambda: _EPOCH,
                min_positive_outcome_rate=autonomy_policy.confidence.min_positive_outcome_rate,
            ),
            clock=lambda: _EPOCH,
        )
        ctx.cache[key] = cached
    return cast("DatabaseAutonomyEngine", cached)


def _plan_facts(case: EvalCase, overrides: Mapping[str, Any]) -> PlanFacts:
    values: dict[str, Any] = {**_CLEAN_FACTS, **overrides}
    source_id = eval_uuid("page", "autonomy-source")
    same_page = bool(values.pop("same_page", False))
    return PlanFacts(
        plan_id=eval_uuid("plan", case.case_id),
        source_page_id=source_id,
        target_page_id=source_id if same_page else eval_uuid("page", "autonomy-target"),
        **values,
    )


def _critic_verdict(case: EvalCase, extra: Mapping[str, Any]) -> CriticVerdict | None:
    """The critic's verdict: a RECORDED model answer if the case carries one (replayed through
    the contract's own ``CriticAssessment``, exactly as the agent node would), else declared."""
    recorded = [
        item for item in case.inputs.recorded_model_responses if item.role is ModelRole.REASONING
    ]
    if recorded:
        assessment = CriticAssessment.model_validate(recorded[0].response)
        if not assessment.critiques:
            raise FixtureError("the recorded critic answer holds no critique")
        return assessment.critiques[0].verdict
    declared = extra.get("critic_verdict")
    return None if declared is None else CriticVerdict(str(declared))


def _score_risk_tier(case: EvalCase, extra: Mapping[str, Any]) -> CaseOutcome:
    """``tier_from_signals`` and ``raise_to`` on their own: the raise-only half of the rule
    that a BEHAVIOURAL signal can block or defer a publish and can never justify one.

    ``RiskSignals`` has no field a good outcome could set, so the monotone direction is
    structural; this checks the arithmetic on top of it, including that a floor never LOWERS
    a tier.
    """
    declared = mapping_of(extra.get("risk", {}), "risk")
    tier = tier_from_signals(
        RiskSignals(
            uncalibrated=bool(declared.get("uncalibrated", False)),
            **{name: bool(declared.get(name, False)) for name in _RISK_SIGNAL_FIELDS},
        )
    )
    floor = extra.get("floor")
    if floor is not None:
        tier = raise_to(tier, RiskTier(str(floor)))
    return _verdict(case, decision=f"tier:{tier.value}")


def _score_autonomy_classification(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    """Classify one plan under one or more modes, through the production decision code.

    Real code driven: ``evaluate_blocks`` (the closed block set), ``critic_outcome_of``,
    ``tier_from_signals`` / ``raise_to``, ``DatabaseAutonomyEngine._assemble`` (which calls
    ``decision_for``, the confidence/calibration floors, ``profile_for`` and builds the
    contract-validated ``AutonomyEvaluation``), and ``target_status_for`` / the engine's own
    ``_target_status``.

    The ``_gather`` glue between them (critic outcome -> tier raise -> ``extra_verification``)
    is three lines in the engine that need a database session, so it is mirrored below and
    marked. NOT covered: ``load_plan_facts`` (the SQL snapshot that produces the facts) and
    the recording of the row -- ``tests/integration`` owns those.
    """
    extra = _extra(case)
    if str(extra.get("check", "plan")) == "risk":
        return _score_risk_tier(case, extra)
    facts = _plan_facts(case, mapping_of(extra.get("facts", {}), "facts"))
    engine = _autonomy_engine(ctx, default_profile=str(extra.get("default_profile", "standard")))
    blocks = evaluate_blocks(facts)

    verdict = _critic_verdict(case, extra)
    handed_off = extra.get("handed_off")
    critic = critic_outcome_of(verdict, handed_off=None if handed_off is None else bool(handed_off))
    if critic is CriticOutcome.NO_ACTION:
        # engine._gather raises CriticDeclinedError here: there is nothing to decide.
        return _verdict(
            case,
            decision=f"critic_declined[{','.join(code.value for code in blocks)}]",
            detail="critic outcome is no_action: an earlier outcome than a decision",
        )

    version = extra.get("calibration_version", facts.calibration_version)
    calibrated = CalibratedConfidence(
        value=float(extra.get("confidence", facts.confidence)),
        calibration_version=None if version is None else str(version),
    )
    signals = {
        name: bool(mapping_of(extra.get("risk", {}), "risk").get(name, False))
        for name in _RISK_SIGNAL_FIELDS
    }
    tier = tier_from_signals(RiskSignals(uncalibrated=not calibrated.is_calibrated, **signals))
    # --- mirrors DatabaseAutonomyEngine._gather steps 4-5 (they need a session) -----------
    if verdict is CriticVerdict.UNCERTAIN:
        tier = raise_to(tier, RiskTier.MEDIUM)
    extra_verification = verdict is CriticVerdict.UNCERTAIN or not calibrated.is_calibrated
    # ---------------------------------------------------------------------------------------
    inputs = _Inputs(
        facts=facts,
        identity=PlanIdentity(
            plan_id=facts.plan_id,
            source_page_id=facts.source_page_id,
            target_page_id=facts.target_page_id,
            status="proposed",
            anchor_source="target_topic",
            relationship_type="same_market",
            channels=("semantic",),
            source_url="https://research.example.com/source",
            target_url="https://research.example.com/target",
            anchor_text="electric vehicle",
            reasoning_model=None,
        ),
        blocks=blocks,
        critic=critic,
        verdict=verdict,
        calibrated=calibrated,
        tier=tier,
        extra_verification=extra_verification,
    )

    show = str(extra.get("show", "decision"))
    # A case's `status_source` is read no longer: see the note in the loop below. Datasets
    # still carry it, and loading them is unaffected.
    parts: list[str] = []
    tokens: set[str] = set()
    for name in sequence_of(extra.get("modes", ["shadow", "guarded", "autonomous"]), "modes"):
        mode = AutonomyMode(str(name))
        evaluation = engine._assemble(inputs, mode=mode).evaluation
        # `status_source` once chose between the engine's own `_target_status` method and
        # the frozen helper, so a case could catch the two disagreeing. The engine no
        # longer has that method: it calls `target_status_for` directly
        # (ken_services/autonomy/engine.py), so the two cannot diverge by construction and
        # both sources read the one function. The parameter is kept so the datasets still
        # load unchanged.
        status = target_status_for(
            decision=evaluation.decision, mode=mode, blocks=evaluation.blocks
        )
        token = f"{mode.value}:{evaluation.decision.value}"
        if evaluation.would_have_decision is not None:
            token += f"(would={evaluation.would_have_decision.value})"
        if show in ("status", "full"):
            token += f"/{status.value if status is not None else 'none'}"
        if show == "full":
            token += f"/{evaluation.verification_profile.value}"
        parts.append(token)
        tokens.update({token, f"{mode.value}:{evaluation.decision.value}"})
    codes = ",".join(code.value for code in blocks)
    decision = f"[{codes}] " + ",".join(parts)
    return _verdict(
        case,
        decision=decision,
        detail=f"tier={tier.value} critic={critic.value} calibrated={calibrated.is_calibrated}",
        selected={decision, *tokens},
    )


SCORERS: Final[Mapping[EvalSuite, Any]] = {
    EvalSuite.DESTINATION_SELECTION: _score_destination_selection,
    EvalSuite.ANCHOR_SELECTION: _score_anchor_selection,
    EvalSuite.RELATIONSHIP_DETECTION: _score_relationship_detection,
    EvalSuite.CRITIC: _score_critic,
    EvalSuite.NO_ACTION: _score_no_action,
    EvalSuite.PROTECTED_CONTENT: _score_protected_content,
    EvalSuite.STALE_PAGE_VERSION: _score_stale_page_version,
    EvalSuite.POLICY_REGRESSION: _score_policy_regression,
    EvalSuite.REFLECTION_GROUNDING: _score_reflection_grounding,
    EvalSuite.PROPOSAL_SAFETY: _score_proposal_safety,
    EvalSuite.ROLLBACK_CORRECTNESS: _score_rollback_correctness,
    EvalSuite.AUTONOMY_CLASSIFICATION: _score_autonomy_classification,
}
"""Suite -> executor. ``editorial-agreement`` is DEFERRED (ADR-0016 15.3): it needs held-out
human decisions that do not exist, so it has no scorer and no dataset."""

_ASYNC_SUITES: Final = frozenset(
    suite for suite, scorer in SCORERS.items() if inspect.iscoroutinefunction(scorer)
)
"""DERIVED, not declared: a hand-maintained list would silently return a coroutine the day a
scorer gained an ``await``, and every case in that suite would then "pass" without running."""


async def score_case(case: EvalCase, ctx: ScoringContext) -> CaseOutcome:
    """Execute one case and compare its decision with the recorded expectation.

    A fixture problem (:class:`FixtureError`) FAILS the case with a fixed decision string
    rather than aborting the run: an unrunnable case is a failing case, never a skipped one,
    because a suite that silently drops cases is a gate that cannot fail.
    """
    scorer = SCORERS.get(case.suite)
    if scorer is None:
        return CaseOutcome(
            case_id=case.case_id,
            suite=case.suite,
            decision="suite_has_no_scorer",
            passed=False,
            must_not_hit=False,
            detail="this suite is declared in the vocabulary and not implemented",
            decision_class=case.decision_class.canonical if case.decision_class else None,
        )
    try:
        if case.suite in _ASYNC_SUITES:
            awaited: CaseOutcome = await scorer(case, ctx)
            return awaited
        computed: CaseOutcome = scorer(case, ctx)
        return computed
    except ValidationError as exc:
        # The production code produced (or was handed) something its own contract
        # refuses. That is a FINDING about the code under test, so it fails the case
        # with a fixed decision rather than aborting the run.
        return CaseOutcome(
            case_id=case.case_id,
            suite=case.suite,
            decision="contract_violation",
            passed=False,
            must_not_hit=False,
            detail=f"{exc.error_count()} contract validation error(s)",
            decision_class=case.decision_class.canonical if case.decision_class else None,
        )
    except (FixtureError, KeyError) as exc:
        return CaseOutcome(
            case_id=case.case_id,
            suite=case.suite,
            decision="fixture_error",
            passed=False,
            must_not_hit=False,
            detail=f"{type(exc).__name__}: {exc}"[:400],
            decision_class=case.decision_class.canonical if case.decision_class else None,
        )
