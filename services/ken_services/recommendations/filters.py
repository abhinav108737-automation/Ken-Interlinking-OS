"""The ADR-0011 section 3 'pair' stage: cheap, deterministic, first rejection wins.

:func:`pair_rejection` evaluates the pair filters in EXACTLY this order and returns the
first that fires (``None`` = the pair proceeds to scoring):

====  =========================  ===========================================================
1     ``self_link``              target == source
2     ``source_protected``       an active protection on the source
3     ``target_protected``       an active protection on the target (protection is checked
                                 before anything else, so safety evals always see it)
4     ``experiment_locked``      the :class:`~ken_contracts.recommendations.ExperimentGuard`
                                 said an active experiment forbids the pair (never in Phase 3)
5     ``source_ineligible``      ``LinkEligibility.as_source`` is false (computed live)
6     ``target_ineligible``      ``LinkEligibility.as_target`` is false
7     ``already_linked``         the source's current version already links to the target
8     ``duplicate_recommendation``  an open plan, or a plan whose link is on the page
                                  (ADR-0014 s8), holds the pair
9     ``previously_rejected``    a reviewer rejected the pair within the cooldown
10    ``page_type_incompatible`` the target type is not allowed for the source type (an
                                 unknown/missing type on either side is incompatible), or
                                 the intent compatibility is 0 (missing intent = 0)
====  =========================  ===========================================================

The caller computes every boolean of :class:`PairState` from current state; this module only
orders the decision. The experiment hook is async (:meth:`NoExperiments.locked`), so the
caller awaits it and passes the answer in ``experiment_locked``.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ken_contracts.intelligence import Intent, LinkEligibility
from ken_contracts.recommendations import CandidateRejection, RecommendationPolicy

__all__ = ["NoExperiments", "PairState", "intent_compatibility", "pair_rejection"]


@dataclass(frozen=True, slots=True)
class PairState:
    """Current facts about one (source, target) pair, gathered by the caller."""

    source_id: UUID
    target_id: UUID
    source_protected: bool
    target_protected: bool
    experiment_locked: bool
    source_eligibility: LinkEligibility
    target_eligibility: LinkEligibility
    already_linked: bool
    duplicate: bool
    previously_rejected: bool
    source_page_type: str | None
    target_page_type: str | None
    source_intent: Intent | None
    target_intent: Intent | None
    target_path_excluded: bool = False
    """The target's URL path matches ``policy.excluded_target_paths`` (e.g. the home page)."""


def intent_compatibility(
    source: Intent | None, target: Intent | None, *, policy: RecommendationPolicy
) -> float:
    """``intent_compatibility[source][target]``; a missing intent or pair scores 0."""
    if source is None or target is None:
        return 0.0
    return policy.intent_compatibility.get(source, {}).get(target, 0.0)


def _page_types_compatible(state: PairState, policy: RecommendationPolicy) -> bool:
    if state.source_page_type is None or state.target_page_type is None:
        return False
    allowed = policy.page_type_targets.get(state.source_page_type)
    if allowed is None or state.target_page_type not in allowed:
        return False
    return intent_compatibility(state.source_intent, state.target_intent, policy=policy) > 0.0


def pair_rejection(state: PairState, *, policy: RecommendationPolicy) -> CandidateRejection | None:
    """The first pair-stage rejection of ``state`` (module docstring order), else ``None``."""
    R = CandidateRejection  # noqa: N806 - a local alias keeps the order table readable
    if state.source_id == state.target_id:
        return R.SELF_LINK
    if state.source_protected:
        return R.SOURCE_PROTECTED
    if state.target_protected:
        return R.TARGET_PROTECTED
    if state.experiment_locked:
        return R.EXPERIMENT_LOCKED
    if not state.source_eligibility.as_source:
        return R.SOURCE_INELIGIBLE
    if not state.target_eligibility.as_target:
        return R.TARGET_INELIGIBLE
    if state.already_linked:
        return R.ALREADY_LINKED
    if state.duplicate:
        return R.DUPLICATE_RECOMMENDATION
    if state.previously_rejected:
        return R.PREVIOUSLY_REJECTED
    if state.target_path_excluded:
        return R.TARGET_EXCLUDED
    if not _page_types_compatible(state, policy):
        return R.PAGE_TYPE_INCOMPATIBLE
    return None


class NoExperiments:
    """The Phase 3 :class:`~ken_contracts.recommendations.ExperimentGuard`: locks nothing.

    Phase 7 replaces it with a guard backed by the experiment registry; the filter chain
    already consults the hook, so no recommendation code path changes then.
    """

    async def locked(self, *, source_page_id: UUID, target_page_id: UUID) -> bool:
        del source_page_id, target_page_id
        return False
