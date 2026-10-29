"""Every :class:`~ken_contracts.autonomy.BlockCode` fires from the fact it names, and only
from that fact. One parametrised case per code, so the closed set has no member that nothing
can produce -- a block code nobody can fire is a safety rule that does not exist.
"""

from __future__ import annotations

from typing import Any

import pytest

from ken_contracts.autonomy import BlockCode
from ken_services.autonomy.blocks import BLOCK_ORDER, evaluate_blocks
from tests.unit.autonomy._support import SOURCE, clean_facts

# fact override -> the code it must fire. Several overrides deliberately fire MORE than one
# code (an unpublished destination is usually also policy-disallowed), so each case asserts
# containment and the "clean facts fire nothing" test below carries the exclusivity.
CASES: list[tuple[dict[str, Any], BlockCode]] = [
    ({"target_page_id": SOURCE}, BlockCode.SELF_LINK),
    ({"destination_internal": False}, BlockCode.EXTERNAL_DESTINATION),
    ({"source_protected": True}, BlockCode.PROTECTED_PAGE),
    ({"target_protected": True}, BlockCode.PROTECTED_PAGE),
    ({"target_published": False}, BlockCode.UNPUBLISHED_DESTINATION),
    ({"target_gone": True}, BlockCode.BROKEN_TARGET),
    ({"target_http_status": 404}, BlockCode.BROKEN_TARGET),
    ({"target_http_status": 503}, BlockCode.BROKEN_TARGET),
    ({"source_page_version": 2, "anchor_valid": False}, BlockCode.STALE_SOURCE_VERSION),
    ({"source_current_version": 0}, BlockCode.STALE_SOURCE_VERSION),
    ({"already_linked": True}, BlockCode.DUPLICATE_CONTEXTUAL_LINK),
    ({"page_types_compatible": False}, BlockCode.PROHIBITED_PAGE_TYPE),
    ({"destination_allowed_by_policy": False}, BlockCode.POLICY_DISALLOWED_DESTINATION),
    ({"target_canonical_ok": False}, BlockCode.INVALID_CANONICAL),
    ({"target_redirect_supported": False}, BlockCode.UNSUPPORTED_REDIRECT),
    ({"target_noindex": True}, BlockCode.INVALID_PAGE_STATE),
    ({"target_thin": True}, BlockCode.INVALID_PAGE_STATE),
    ({"anchor_valid": False}, BlockCode.INVALID_ANCHOR),
    ({"rollback_metadata_present": False}, BlockCode.MISSING_ROLLBACK_METADATA),
    ({"vector_fresh": False}, BlockCode.STALE_VECTOR),
]


def test_clean_facts_fire_no_block() -> None:
    assert evaluate_blocks(clean_facts()) == ()


@pytest.mark.parametrize(("override", "code"), CASES, ids=[f"{c.value}" for _o, c in CASES])
def test_each_fact_fires_its_code(override: dict[str, Any], code: BlockCode) -> None:
    assert code in evaluate_blocks(clean_facts(**override))


def test_every_block_code_is_reachable() -> None:
    """The closed set has no unreachable member."""
    reachable = {
        code for override, _c in CASES for code in evaluate_blocks(clean_facts(**override))
    }
    assert reachable == set(BlockCode)


def test_codes_come_back_in_enum_order_and_without_duplicates() -> None:
    facts = clean_facts(
        source_protected=True,
        target_published=False,
        anchor_valid=False,
        vector_fresh=False,
        target_protected=True,
    )
    codes = evaluate_blocks(facts)
    assert len(set(codes)) == len(codes)
    assert list(codes) == [c for c in BLOCK_ORDER if c in set(codes)]
    # The AutonomyEvaluation contract validates exactly this, so the two agree.
    assert codes == tuple(sorted(codes, key=BLOCK_ORDER.index))


def test_a_duplicate_is_allowed_only_when_the_policy_allows_one() -> None:
    assert BlockCode.DUPLICATE_CONTEXTUAL_LINK in evaluate_blocks(clean_facts(already_linked=True))
    assert BlockCode.DUPLICATE_CONTEXTUAL_LINK not in evaluate_blocks(
        clean_facts(already_linked=True, duplicate_allowed=True)
    )


def test_a_destination_never_fetched_over_http_is_not_a_broken_target() -> None:
    """``None`` means the inventory never fetched it (a CMS or file-export import), which is
    not evidence of breakage. Treating it as one would block every CMS-sourced corpus."""
    assert BlockCode.BROKEN_TARGET not in evaluate_blocks(clean_facts(target_http_status=None))


def test_a_rebasable_plan_on_an_older_source_version_is_not_stale() -> None:
    """The source moved but the anchor still sits verbatim in a block that hashes the same:
    that is ``PlanFreshness.REBASABLE``, which ADR-0011 keeps and must not block."""
    facts = clean_facts(source_page_version=2, source_current_version=3, anchor_valid=True)
    assert BlockCode.STALE_SOURCE_VERSION not in evaluate_blocks(facts)


def test_the_evaluator_is_total() -> None:
    """Every input yields codes, never an exception: a malformed plan is BLOCKED rather than
    crashing a publish run."""
    facts = clean_facts(
        source_page_type=None,
        target_page_type=None,
        target_http_status=None,
        source_current_version=0,
        target_current_version=0,
    )
    codes = evaluate_blocks(facts)
    assert codes and all(isinstance(c, BlockCode) for c in codes)
