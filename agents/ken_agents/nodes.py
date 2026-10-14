"""The supervisor's nodes (ADR-0012 section 2). Pure functions of (state, context).

Each node receives the run's :class:`NodeContext` (the ToolBox, the role-scoped model
clients, the policy) and its step's :class:`StepScratch`, and returns its declared
partial-update model. It never receives a session, settings, a provider implementation,
credentials or an actor string. Everything deterministic stays deterministic; a model may
only choose among, and assess, plans named by opaque refs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ken_agents import prompts
from ken_agents.llm import RoleModel
from ken_agents.recording import StepScratch
from ken_agents.state import (
    AgentState,
    CheckInvariantsUpdate,
    CritiqueUpdate,
    DiscoverUpdate,
    FinalizeUpdate,
    LoadPageUpdate,
    PlanReview,
    PlanUpdate,
    PolicyGateUpdate,
    RiskLevel,
    TriageUpdate,
    UnderstandUpdate,
    add_notes,
)
from ken_agents.tools import ToolBox
from ken_contracts.agents import (
    MAX_CANDIDATES_FOR_MODEL,
    MAX_EXPLANATION_CHARS,
    AgentErrorCode,
    AgentOutcome,
    AgentPolicy,
    AgentRunStatus,
    CriticAssessment,
    CriticReason,
    CriticVerdict,
    FallbackReason,
    PlannerSelection,
    TriageDecision,
)
from ken_contracts.recommendations import LinkPlan
from ken_services.agents.protocol import PlanReviewInput

__all__ = [
    "NodeContext",
    "check_invariants",
    "critique",
    "deterministic_review",
    "discover",
    "finalize",
    "load_page",
    "plan",
    "policy_gate",
    "risk_level",
    "triage",
    "understand",
]


@dataclass(frozen=True, slots=True)
class NodeContext:
    """What a node may use. ``planner``/``critic`` are None when no provider is configured."""

    tools: ToolBox
    policy: AgentPolicy
    planner: RoleModel | None
    critic: RoleModel | None


def _clip(text: str, limit: int = MAX_EXPLANATION_CHARS) -> str:
    value = " ".join(text.split())
    return value if len(value) <= limit else value[: limit - 3] + "..."


# --------------------------------------------------------------------------- #
# load_page / triage / understand / discover
# --------------------------------------------------------------------------- #
async def load_page(state: AgentState, ctx: NodeContext, step: StepScratch) -> LoadPageUpdate:
    page = await ctx.tools.get_page(page_id=state.source_page_id)
    if page is None:
        step.fail(AgentErrorCode.PAGE_NOT_FOUND)
        step.output = {"found": False}
        return LoadPageUpdate(errors=[*state.errors, AgentErrorCode.PAGE_NOT_FOUND])
    step.output = {"found": True, "version": page.current_version, "gone": page.gone}
    return LoadPageUpdate(source_page=page)


async def triage(state: AgentState, ctx: NodeContext, step: StepScratch) -> TriageUpdate:
    """Deterministic TRD section 10 decision. Agents never run analysis (``reindex`` ends)."""
    page = state.source_page
    notes: list[str] = []
    decision = TriageDecision.OPTIMIZE
    if page is None or page.gone:
        decision = TriageDecision.NO_ACTION
        notes.append("Source page is gone: nothing to review.")
    elif await ctx.tools.get_analysis(page_id=state.source_page_id) is None:
        decision = TriageDecision.REINDEX
        notes.append("Analysis is missing or stale for the current version: run make analyze.")
    else:
        eligibility = await ctx.tools.get_eligibility(page_id=state.source_page_id)
        if eligibility is None:
            decision = TriageDecision.REINDEX
            notes.append("Eligibility is not computed: run make analyze.")
        elif not eligibility.as_source:
            decision = TriageDecision.NO_ACTION
            reasons = ", ".join(r.value for r in eligibility.source_reasons) or "unspecified"
            notes.append(f"Source is not eligible to carry links ({reasons}).")
        elif not await ctx.tools.list_proposed_plans(source_page_id=state.source_page_id, limit=1):
            decision = TriageDecision.NO_ACTION
    step.output = {"decision": decision.value}
    if notes:
        return TriageUpdate(
            triage_decision=decision, review_notes=add_notes(state.review_notes, notes)
        )
    return TriageUpdate(triage_decision=decision)


async def understand(state: AgentState, ctx: NodeContext, step: StepScratch) -> UnderstandUpdate:
    analysis = await ctx.tools.get_analysis(page_id=state.source_page_id)
    step.output = {
        "found": analysis is not None,
        "page_type": analysis.page_type if analysis else None,
        "intent": analysis.intent.value if analysis else None,
        "market_id": analysis.market_id if analysis else None,
    }
    return UnderstandUpdate(classification=analysis)


async def discover(state: AgentState, ctx: NodeContext, step: StepScratch) -> DiscoverUpdate:
    plans = await ctx.tools.list_proposed_plans(
        source_page_id=state.source_page_id, limit=MAX_CANDIDATES_FOR_MODEL
    )
    step.output = {"count": len(plans), "plan_ids": [str(p.id) for p in plans]}
    return DiscoverUpdate(candidates=plans)


# --------------------------------------------------------------------------- #
# plan (FAST, optional)
# --------------------------------------------------------------------------- #
async def plan(state: AgentState, ctx: NodeContext, step: StepScratch) -> PlanUpdate:
    """Choose which plans to review, at most ``max_plans_per_run``. Rule: engine order."""
    limit = ctx.policy.max_plans_per_run
    offered = state.candidates[:MAX_CANDIDATES_FOR_MODEL]
    refs = prompts.refs_for(offered)
    engine_order = [p.id for p in offered[:limit]]
    output: dict[str, Any] = {"offered": len(offered), "max": limit}

    if not ctx.policy.planner.use_model:
        step.fallback(FallbackReason.MODEL_DISABLED)
        selection = engine_order
    elif ctx.planner is None:
        step.fallback(FallbackReason.MODEL_UNAVAILABLE)
        selection = engine_order
    else:
        answer = await ctx.planner.ask(
            messages=prompts.planner_messages(
                source=state.source_page, refs=refs, max_selected=limit
            ),
            schema=PlannerSelection,
            step=step,
        )
        if answer.value is None:
            step.fallback(answer.fallback_reason or FallbackReason.MODEL_UNAVAILABLE)
            selection = engine_order
        else:
            known = [r for r in answer.value.selected if r in refs]
            output["unknown_refs"] = [r for r in answer.value.selected if r not in refs]
            output["explanation"] = _clip(answer.value.explanation)
            if known:
                output["selected_refs"] = known[:limit]
                selection = [refs[r].id for r in known[:limit]]
            else:  # covered none of the offered refs
                step.fallback(FallbackReason.SCHEMA_VIOLATION)
                selection = engine_order
    output["selected"] = [str(i) for i in selection]
    step.output = output
    return PlanUpdate(ranked_candidates=selection)


# --------------------------------------------------------------------------- #
# critique (REASONING, optional)
# --------------------------------------------------------------------------- #
def deterministic_review(
    plan_: LinkPlan, ref: str, threshold: float, reason: FallbackReason
) -> PlanReview:
    """The rule: ``accept`` at or above the RAW confidence threshold, else ``uncertain``."""
    reasons: tuple[CriticReason, ...]
    if plan_.confidence >= threshold:
        verdict, reasons, cmp = CriticVerdict.ACCEPT, (), ">="
    else:
        verdict, reasons, cmp = CriticVerdict.UNCERTAIN, (CriticReason.LOW_CONFIDENCE,), "<"
    return PlanReview(
        plan_id=plan_.id,
        ref=ref,
        verdict=verdict,
        reasons=reasons,
        explanation=(
            f"Deterministic rule: raw confidence {plan_.confidence:.2f} {cmp} {threshold:.2f}."
        ),
        confidence=plan_.confidence,
        fallback_reason=reason,
        model=None,
    )


async def critique(state: AgentState, ctx: NodeContext, step: StepScratch) -> CritiqueUpdate:
    by_id = {p.id: p for p in state.candidates}
    chosen = [by_id[i] for i in state.ranked_candidates if i in by_id]
    if not chosen:
        step.skip()
        return CritiqueUpdate(critic_result=[])
    refs = prompts.refs_for(chosen)
    threshold = ctx.policy.critic.accept_min_confidence

    def rule_for_all(reason: FallbackReason) -> list[PlanReview]:
        step.fallback(reason)
        return [deterministic_review(p, r, threshold, reason) for r, p in refs.items()]

    output: dict[str, Any] = {"offered": len(refs)}
    if not ctx.policy.critic.use_model:
        reviews = rule_for_all(FallbackReason.MODEL_DISABLED)
    elif ctx.critic is None:
        reviews = rule_for_all(FallbackReason.MODEL_UNAVAILABLE)
    else:
        answer = await ctx.critic.ask(
            messages=prompts.critic_messages(source=state.source_page, refs=refs),
            schema=CriticAssessment,
            step=step,
        )
        if answer.value is None:
            reviews = rule_for_all(answer.fallback_reason or FallbackReason.MODEL_UNAVAILABLE)
        else:
            by_ref = {c.ref: c for c in answer.value.critiques if c.ref in refs}
            output["unknown_refs"] = [c.ref for c in answer.value.critiques if c.ref not in refs]
            output["omitted_refs"] = [r for r in refs if r not in by_ref]
            if not by_ref:  # covered none of the offered refs
                reviews = rule_for_all(FallbackReason.SCHEMA_VIOLATION)
            else:
                reviews = []
                for ref, plan_ in refs.items():
                    critique_ = by_ref.get(ref)
                    if critique_ is None:  # omitted: the deterministic rule, recorded
                        step.fallback(FallbackReason.SCHEMA_VIOLATION)
                        reviews.append(
                            deterministic_review(
                                plan_, ref, threshold, FallbackReason.SCHEMA_VIOLATION
                            )
                        )
                        continue
                    reviews.append(
                        PlanReview(
                            plan_id=plan_.id,
                            ref=ref,
                            verdict=critique_.verdict,
                            reasons=tuple(dict.fromkeys(critique_.reasons)),
                            explanation=_clip(critique_.explanation),
                            confidence=plan_.confidence,
                            fallback_reason=None,
                            model=answer.model,
                        )
                    )
    output["reviews"] = [
        {
            "plan_id": str(r.plan_id),
            "verdict": r.verdict.value,
            "reasons": [x.value for x in r.reasons],
            "fallback_reason": r.fallback_reason.value if r.fallback_reason else None,
        }
        for r in reviews
    ]
    step.output = output
    return CritiqueUpdate(critic_result=reviews)


# --------------------------------------------------------------------------- #
# check_invariants / policy_gate
# --------------------------------------------------------------------------- #
async def check_invariants(
    state: AgentState, ctx: NodeContext, step: StepScratch
) -> CheckInvariantsUpdate:
    if not state.critic_result:
        step.skip()
        return CheckInvariantsUpdate(invariant_problems={})
    problems: dict[Any, tuple[str, ...]] = {}
    notes: list[str] = []
    for review in state.critic_result:
        check = await ctx.tools.check_plan(plan_id=review.plan_id)
        if not check.ok:
            codes = check.problems or ("invariant_violated",)
            problems[review.plan_id] = tuple(codes)
            notes.append(
                f"Plan {review.plan_id} was not handed off: invariant check failed "
                f"({', '.join(codes)})."
            )
    step.output = {
        "checked": len(state.critic_result),
        "failed": {str(k): list(v) for k, v in problems.items()},
    }
    return CheckInvariantsUpdate(
        invariant_problems=problems, review_notes=add_notes(state.review_notes, notes)
    )


async def policy_gate(state: AgentState, ctx: NodeContext, step: StepScratch) -> PolicyGateUpdate:
    """ONE hand-off call with every review whose plan passed its checks (rejects included:
    they are recorded, not handed off). There is no edge to ``approved``."""
    reviews: Sequence[PlanReview] = [
        r for r in state.critic_result if r.plan_id not in state.invariant_problems
    ]
    if not reviews:
        step.skip()
        return PolicyGateUpdate(link_plans=[], recorded_plans=[], conflicts={})
    result = await ctx.tools.hand_off_plans(
        reviews=[
            PlanReviewInput(
                plan_id=r.plan_id,
                verdict=r.verdict,
                reasons=r.reasons,
                explanation=r.explanation,
                fallback_reason=r.fallback_reason,
                model=r.model,
            )
            for r in reviews
        ]
    )
    sent = {r.plan_id for r in reviews}
    handed_off = [i for i in result.handed_off if i in sent]
    conflicts = {k: v for k, v in result.conflicts.items() if k in sent}
    notes = [
        f"Plan {plan_id} changed status concurrently ({code}); it was not handed off."
        for plan_id, code in conflicts.items()
    ]
    step.output = {
        "sent": len(reviews),
        "handed_off": [str(i) for i in handed_off],
        "recorded": len(result.recorded),
        "conflicts": {str(k): v for k, v in conflicts.items()},
    }
    return PolicyGateUpdate(
        link_plans=handed_off,
        recorded_plans=list(result.recorded),
        conflicts=conflicts,
        review_notes=add_notes(state.review_notes, notes),
    )


# --------------------------------------------------------------------------- #
# finalize
# --------------------------------------------------------------------------- #
def risk_level(state: AgentState, threshold: float) -> RiskLevel | None:
    """Deterministic: high on any invariant problem or conflict; medium on any non-accept
    verdict or confidence below the critic threshold; else low. None if nothing reviewed."""
    if not state.critic_result:
        return None
    if state.invariant_problems or state.conflicts:
        return RiskLevel.HIGH
    if any(
        r.verdict is not CriticVerdict.ACCEPT or r.confidence < threshold
        for r in state.critic_result
    ):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


async def finalize(state: AgentState, ctx: NodeContext, step: StepScratch) -> FinalizeUpdate:
    """Outcome rules of ADR-0012 section 2."""
    risk = risk_level(state, ctx.policy.critic.accept_min_confidence)
    handed = set(state.link_plans)
    reviews = state.critic_result
    error_code: AgentErrorCode | None = None
    if state.errors:
        outcome, status = AgentOutcome.FAILED, AgentRunStatus.FAILED
        error_code = state.errors[0]
    elif state.triage_decision is not TriageDecision.OPTIMIZE or not state.candidates:
        outcome, status = AgentOutcome.NO_ACTION, AgentRunStatus.COMPLETED
    elif any(r.verdict is CriticVerdict.ACCEPT and r.plan_id in handed for r in reviews):
        outcome, status = AgentOutcome.RECOMMEND, AgentRunStatus.AWAITING_APPROVAL
    elif handed:
        outcome, status = AgentOutcome.NEEDS_HUMAN_REVIEW, AgentRunStatus.AWAITING_APPROVAL
    elif state.invariant_problems or state.conflicts:
        outcome, status = AgentOutcome.NEEDS_HUMAN_REVIEW, AgentRunStatus.COMPLETED
        if state.conflicts:  # ADR-0012 section 8: nothing handed off after a conflict
            error_code = AgentErrorCode.HANDOFF_CONFLICT
    else:
        outcome, status = AgentOutcome.NO_ACTION, AgentRunStatus.COMPLETED
    step.output = {
        "outcome": outcome.value,
        "status": status.value,
        "risk_level": risk.value if risk else None,
        "handed_off": len(handed),
        "error_code": error_code.value if error_code else None,
    }
    return FinalizeUpdate(outcome=outcome, status=status, risk_level=risk, error_code=error_code)
