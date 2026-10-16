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
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import ValidationError

from ken_agents import nodes as agent_nodes
from ken_agents.llm import RoleModel
from ken_agents.recording import StepScratch
from ken_agents.state import AgentState
from ken_agents.tools import ToolBox
from ken_contracts.agents import (
    AgentPolicy,
    AgentTrigger,
    CriticAssessment,
)
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
from ken_services.deployment.mutation import HtmlMutator
from ken_services.deployment.protocol import LinkInsertion
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
