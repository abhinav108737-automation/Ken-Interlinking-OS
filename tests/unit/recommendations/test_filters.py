"""ADR-0011 section 3 'pair' stage: every rejection code and the exact precedence."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from typing import Any

import pytest

from ken_contracts.intelligence import IneligibilityReason, Intent, LinkEligibility
from ken_contracts.recommendations import CandidateRejection, ExperimentGuard
from ken_services.recommendations.filters import (
    NoExperiments,
    PairState,
    intent_compatibility,
    pair_rejection,
)
from tests.unit.recommendations._support import policy, policy_with

R = CandidateRejection
SOURCE, TARGET = uuid.uuid4(), uuid.uuid4()
ELIGIBLE = LinkEligibility(as_source=True, as_target=True, source_reasons=[], target_reasons=[])
INELIGIBLE = LinkEligibility(
    as_source=False,
    as_target=False,
    source_reasons=[IneligibilityReason.THIN_CONTENT],
    target_reasons=[IneligibilityReason.NOINDEX],
)


def _state(**changes: Any) -> PairState:
    base = PairState(
        source_id=SOURCE,
        target_id=TARGET,
        source_protected=False,
        target_protected=False,
        experiment_locked=False,
        source_eligibility=ELIGIBLE,
        target_eligibility=ELIGIBLE,
        already_linked=False,
        duplicate=False,
        previously_rejected=False,
        source_page_type="insight",
        target_page_type="report",
        source_intent=Intent.INFORMATIONAL,
        target_intent=Intent.COMMERCIAL_RESEARCH,
    )
    return dataclasses.replace(base, **changes)


def test_a_clean_pair_passes() -> None:
    assert pair_rejection(_state(), policy=policy()) is None


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"target_id": SOURCE}, R.SELF_LINK),
        ({"source_protected": True}, R.SOURCE_PROTECTED),
        ({"target_protected": True}, R.TARGET_PROTECTED),
        ({"experiment_locked": True}, R.EXPERIMENT_LOCKED),
        ({"source_eligibility": INELIGIBLE}, R.SOURCE_INELIGIBLE),
        ({"target_eligibility": INELIGIBLE}, R.TARGET_INELIGIBLE),
        ({"already_linked": True}, R.ALREADY_LINKED),
        ({"duplicate": True}, R.DUPLICATE_RECOMMENDATION),
        ({"previously_rejected": True}, R.PREVIOUSLY_REJECTED),
        ({"target_page_type": "other"}, R.PAGE_TYPE_INCOMPATIBLE),  # insight -> other: not allowed
        ({"source_page_type": "hub"}, R.PAGE_TYPE_INCOMPATIBLE),  # hub has no targets row
        ({"source_page_type": None}, R.PAGE_TYPE_INCOMPATIBLE),
        ({"target_page_type": None}, R.PAGE_TYPE_INCOMPATIBLE),
        ({"source_intent": Intent.CORPORATE}, R.PAGE_TYPE_INCOMPATIBLE),  # intent row missing
        ({"target_intent": Intent.TRANSACTIONAL}, R.PAGE_TYPE_INCOMPATIBLE),  # pair missing = 0
        ({"source_intent": None}, R.PAGE_TYPE_INCOMPATIBLE),
        ({"target_intent": None}, R.PAGE_TYPE_INCOMPATIBLE),
    ],
)
def test_each_rejection_code(changes: dict[str, Any], code: CandidateRejection) -> None:
    assert pair_rejection(_state(**changes), policy=policy()) is code


def test_explicit_zero_intent_compatibility_is_incompatible() -> None:
    zero = policy_with(
        intent_compatibility={"informational": {"commercial_research": 0.0, "informational": 0.8}}
    )
    assert pair_rejection(_state(), policy=zero) is R.PAGE_TYPE_INCOMPATIBLE


ALL_FAILING: dict[str, Any] = {
    "target_id": SOURCE,
    "source_protected": True,
    "target_protected": True,
    "experiment_locked": True,
    "source_eligibility": INELIGIBLE,
    "target_eligibility": INELIGIBLE,
    "already_linked": True,
    "duplicate": True,
    "previously_rejected": True,
    "target_page_type": "other",
}
ORDER = [
    ("target_id", R.SELF_LINK),
    ("source_protected", R.SOURCE_PROTECTED),
    ("target_protected", R.TARGET_PROTECTED),
    ("experiment_locked", R.EXPERIMENT_LOCKED),
    ("source_eligibility", R.SOURCE_INELIGIBLE),
    ("target_eligibility", R.TARGET_INELIGIBLE),
    ("already_linked", R.ALREADY_LINKED),
    ("duplicate", R.DUPLICATE_RECOMMENDATION),
    ("previously_rejected", R.PREVIOUSLY_REJECTED),
    ("target_page_type", R.PAGE_TYPE_INCOMPATIBLE),
]


def test_precedence_is_exactly_the_adr_order() -> None:
    """Fail everything, then clear one filter at a time from the top: the next one fires."""
    failing = dict(ALL_FAILING)
    for field, code in ORDER:
        assert pair_rejection(_state(**failing), policy=policy()) is code
        del failing[field]
    assert pair_rejection(_state(**failing), policy=policy()) is None


def test_protection_wins_over_ineligibility_even_when_eligibility_lists_protected() -> None:
    protected_too = LinkEligibility(
        as_source=False,
        as_target=False,
        source_reasons=[IneligibilityReason.PROTECTED],
        target_reasons=[IneligibilityReason.PROTECTED],
    )
    state = _state(
        target_protected=True, source_eligibility=protected_too, target_eligibility=protected_too
    )
    assert pair_rejection(state, policy=policy()) is R.TARGET_PROTECTED


def test_rejection_order_matches_the_contract_enum_prefix() -> None:
    assert [code for _field, code in ORDER] == list(CandidateRejection)[: len(ORDER)]


def test_intent_compatibility_reads_the_matrix() -> None:
    p = policy()
    assert intent_compatibility(Intent.INFORMATIONAL, Intent.COMMERCIAL_RESEARCH, policy=p) == 1.0
    assert intent_compatibility(Intent.NAVIGATIONAL, Intent.INFORMATIONAL, policy=p) == 0.6
    assert intent_compatibility(Intent.NAVIGATIONAL, Intent.NAVIGATIONAL, policy=p) == 0.0
    assert intent_compatibility(None, Intent.INFORMATIONAL, policy=p) == 0.0


def test_no_experiments_guard_locks_nothing_and_satisfies_the_protocol() -> None:
    guard = NoExperiments()
    assert isinstance(guard, ExperimentGuard)
    assert asyncio.run(guard.locked(source_page_id=SOURCE, target_page_id=TARGET)) is False


def test_pair_state_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _state().duplicate = True  # type: ignore[misc]


def test_an_excluded_target_path_is_page_type_incompatible() -> None:
    """policy.excluded_target_paths (the home page): never a link target (eval finding)."""
    assert pair_rejection(_state(target_path_excluded=True), policy=policy()) is (
        CandidateRejection.PAGE_TYPE_INCOMPATIBLE
    )
    assert pair_rejection(_state(), policy=policy()) is None
