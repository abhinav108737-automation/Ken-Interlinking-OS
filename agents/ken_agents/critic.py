"""The critic's deterministic layer and its autonomy-level outcome (ADR-0012, ADR-0018).

The critic is a SIGNAL for the autonomy engine, never a decision (ADR-0018 section 1):

* ``ACCEPT``    -- no defect found. The plan is handed to the autonomy engine, which still runs
  its deterministic blocks FIRST; an accept clears nothing.
* ``REJECT``    -- a concrete defect. The review is recorded and the plan is not handed on; the
  engine turns it into ``BLOCK``.
* ``NO_ACTION`` -- not a defect, simply nothing worth doing: a duplicate of a better
  candidate, a candidate that cannot coexist with it, an ``uncertain`` model answer, or any
  absent / invalid / unavailable model answer (see :func:`critic_outcome`). Recorded, not
  handed on, never a publish. A model problem therefore degrades to NO_ACTION, never ACCEPT.

Two producers feed those outcomes, and only ever in ONE direction:

1. a model (optional; REASONING role) judges semantics, naturalness, destination fit, ...;
2. deterministic **guards** run AFTER the model and AFTER the rule, over the plans' own stored
   fields. A guard can only move an outcome toward ``REJECT`` / ``NO_ACTION`` -- it never
   upgrades -- so the model cannot talk the run out of a guard, and an injected "accept"
   changes nothing a guard decided. Guards read no model output and no page text beyond the
   plan's own anchor/title/locator fields.

Nothing in this module can clear a deterministic safety block: those live in
``ken_services.autonomy.blocks``, are evaluated by the autonomy engine from stored facts, and
this module does not import them (a static test pins that).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ken_contracts.agents import CriticReason, CriticVerdict
from ken_contracts.autonomy import CriticOutcome
from ken_contracts.recommendations import LinkPlan

__all__ = [
    "GENERIC_ANCHORS",
    "Guard",
    "GuardFinding",
    "critic_outcome",
    "guard_for",
    "is_handed_on",
]


class Guard(StrEnum):
    """Stable codes of the deterministic guards (recorded in the step output)."""

    GENERIC_ANCHOR = "generic_anchor"
    """The anchor says nothing about the destination ("click here"): misleading by omission."""
    DUPLICATE_DESTINATION = "duplicate_destination"
    """An earlier-ranked candidate in this review already links to the same page."""
    CANNIBALISATION = "cannibalisation"
    """An earlier-ranked candidate uses the same anchor text for another destination."""
    OVERLAPPING_PLACEMENT = "overlapping_placement"
    """An earlier-ranked candidate occupies the same span of the same block."""


GENERIC_ANCHORS: Final = frozenset(
    {
        "click here",
        "here",
        "read more",
        "learn more",
        "more",
        "this",
        "this page",
        "this link",
        "this report",
        "link",
        "see more",
    }
)
"""Anchors that name nothing. Lower-cased, whitespace-collapsed, punctuation-stripped."""


@dataclass(frozen=True, slots=True)
class GuardFinding:
    guard: Guard
    outcome: CriticOutcome
    reason: CriticReason
    explanation: str


def _norm(text: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text.casefold()).split())


def critic_outcome(verdict: object) -> CriticOutcome:
    """THE mapping from the model-boundary verdict to the autonomy-level outcome. Pure, total.

    ============  ==========  =====================================================
    verdict       outcome     why
    ============  ==========  =====================================================
    ``accept``    ACCEPT      the only input that can ever produce ACCEPT
    ``reject``    REJECT      a concrete defect
    ``uncertain`` NO_ACTION   a doubt is not a licence: nothing worth doing *yet*
    anything else NO_ACTION   absent, unparseable or unknown is never permissive
    ============  ==========  =====================================================

    ``uncertain`` becomes NO_ACTION rather than REJECT because no defect was found, so there
    is nothing to record as blocked (ADR-0018 section 1: NO_ACTION "never becomes a
    publish"); an operator can still override a plan that stayed ``proposed``. Compared by
    identity with the enum members, so a look-alike string or object is NO_ACTION too.
    """
    if verdict is CriticVerdict.ACCEPT:
        return CriticOutcome.ACCEPT
    if verdict is CriticVerdict.REJECT:
        return CriticOutcome.REJECT
    return CriticOutcome.NO_ACTION


def is_handed_on(outcome: CriticOutcome) -> bool:
    """Only ACCEPT is passed to the autonomy engine as a candidate to decide."""
    return outcome is CriticOutcome.ACCEPT


def _overlap(a: LinkPlan, b: LinkPlan) -> bool:
    la, lb = a.locator, b.locator
    return la.block_id == lb.block_id and la.start < lb.end and lb.start < la.end


def guard_for(plan: LinkPlan, surviving: Sequence[LinkPlan]) -> GuardFinding | None:
    """The first guard that applies to ``plan``, or ``None``.

    ``surviving`` are the better-ranked candidates of this review that are still ACCEPT after
    the model and the earlier guards: a candidate that was itself rejected or declined does
    not shadow a later one. A guard only ever moves an outcome toward REJECT / NO_ACTION."""
    anchor = _norm(plan.anchor_text)
    if anchor in GENERIC_ANCHORS:
        return GuardFinding(
            Guard.GENERIC_ANCHOR,
            CriticOutcome.REJECT,
            CriticReason.MISLEADING_ANCHOR,
            "Deterministic guard: the anchor text does not describe the destination.",
        )
    for earlier in surviving:
        if earlier.target_page_id == plan.target_page_id:
            return GuardFinding(
                Guard.DUPLICATE_DESTINATION,
                CriticOutcome.NO_ACTION,
                CriticReason.OVER_LINKING,
                "Deterministic guard: a better-ranked candidate already links to this page.",
            )
        if _norm(earlier.anchor_text) == anchor:
            return GuardFinding(
                Guard.CANNIBALISATION,
                CriticOutcome.NO_ACTION,
                CriticReason.OVER_LINKING,
                "Deterministic guard: a better-ranked candidate uses the same anchor "
                "for another page.",
            )
        if _overlap(earlier, plan):
            return GuardFinding(
                Guard.OVERLAPPING_PLACEMENT,
                CriticOutcome.NO_ACTION,
                CriticReason.OVER_LINKING,
                "Deterministic guard: a better-ranked candidate occupies the same text.",
            )
    return None
