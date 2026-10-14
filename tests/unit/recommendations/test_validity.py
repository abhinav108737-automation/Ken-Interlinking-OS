"""The shared durable-invalidity rules (``ken_services.recommendations.validity``)."""

from __future__ import annotations

import uuid

import pytest

from ken_contracts.intelligence import IneligibilityReason, LinkEligibility
from ken_services.recommendations import pipeline
from ken_services.recommendations.validity import (
    DURABLE_INELIGIBILITY,
    plan_invalidity,
    source_lock_key,
)

R = IneligibilityReason
OK = LinkEligibility(as_source=True, as_target=True, source_reasons=[], target_reasons=[])


def _el(*reasons: IneligibilityReason) -> LinkEligibility:
    return LinkEligibility(
        as_source=not reasons,
        as_target=not reasons,
        source_reasons=list(reasons),
        target_reasons=list(reasons),
    )


def test_the_pipeline_uses_the_shared_rules() -> None:
    assert pipeline.DURABLE_INELIGIBILITY is DURABLE_INELIGIBILITY
    assert {R.NOT_ANALYZED, R.STALE_ANALYSIS, R.HTTP_NOT_OK}.isdisjoint(DURABLE_INELIGIBILITY)


@pytest.mark.parametrize(
    ("source", "target", "linked", "expected"),
    [
        (OK, OK, False, None),
        (_el(R.STALE_ANALYSIS), _el(R.HTTP_NOT_OK, R.NOT_ANALYZED), False, None),  # lag only
        (_el(R.PROTECTED), OK, False, "source_ineligible (protected)"),
        (_el(R.GONE, R.NOINDEX), OK, False, "source_ineligible (gone, noindex)"),
        (OK, _el(R.THIN_CONTENT), False, "target_ineligible (thin_content)"),
        (OK, None, False, "target_ineligible (unknown)"),
        (None, OK, False, "source_ineligible (unknown)"),
        (OK, OK, True, "already_linked"),
    ],
)
def test_plan_invalidity(
    source: LinkEligibility | None,
    target: LinkEligibility | None,
    linked: bool,
    expected: str | None,
) -> None:
    assert (
        plan_invalidity(source_eligibility=source, target_eligibility=target, already_linked=linked)
        == expected
    )


def test_source_lock_keys_are_stable_signed_64_bit_and_distinct() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert source_lock_key(a) == source_lock_key(a)
    assert source_lock_key(a) != source_lock_key(b)
    assert -(2**63) <= source_lock_key(a) < 2**63
