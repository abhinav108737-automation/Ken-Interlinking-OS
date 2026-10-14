"""``check_plan_freshness`` over the spec-derived fixtures (ADR-0011 section 6).

Each case in ``tests/fixtures/demo/stale_context_cases.json`` is a plan proposed on
``plan_html`` (source version 1) and checked against ``current_html``. Blocks come from the
Phase 1 ``extract_blocks``; the plan locator is built from ``plan_html`` exactly as the
engine records it (block found by anchor + occurrence index, ``context_hash`` = sha256 of
the block text) and cross-checked against the fixture's ``plan_locator``.

The freshness VERDICT (fresh / rebasable / stale) is the safety property and is gated for
every case. The reason codes are diagnostics and are gated separately.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from ken_contracts.recommendations import FreshnessCheck, PlacementLocator, PlanFreshness
from ken_services.inventory.normalize import extract_blocks
from ken_services.recommendations.freshness import check_plan_freshness
from tests.unit.recommendation_evals.judgments import stale_cases

CASES: list[dict[str, Any]] = stale_cases()["cases"]
IDS = [case["id"] for case in CASES]
VOCABULARY = frozenset(stale_cases()["reason_vocabulary"])

_ANCHOR_MOVED_GAP = (
    "Engine reports context_changed, not anchor_moved, when the anchor moved or vanished inside "
    "an EDITED block: its anchor_moved fires only when the context hash still matches (which a "
    "consistent locator cannot produce). Verdict is stale either way (safe); only the "
    "diagnostic reason differs. Reported to the orchestrator; remove once decided."
)
KNOWN_REASON_MISMATCHES: dict[str, str] = {}
# Resolved by the orchestrator: an edited block whose anchor moved or vanished now reports
# context_changed AND anchor_moved (freshness.py). _ANCHOR_MOVED_GAP kept for the record.
"""Case id -> why the engine's reason code differs from the fixture (reported, not hidden).
Strict xfail: if the engine starts reporting the fixture's reason, the XPASS fails and the
entry must be removed."""


def _reason_param(case: dict[str, Any]) -> Any:
    marks = (
        [pytest.mark.xfail(strict=True, reason=KNOWN_REASON_MISMATCHES[case["id"]])]
        if case["id"] in KNOWN_REASON_MISMATCHES
        else []
    )
    return pytest.param(case, id=case["id"], marks=marks)


def _locator(case: dict[str, Any]) -> PlacementLocator:
    anchor = case["anchor"].lower()
    hits = [
        (block, start)
        for block in extract_blocks(case["plan_html"])
        for start in range(len(block.text))
        if block.text.lower().startswith(anchor, start)
    ]
    block, start = hits[case["occurrence_index"]]
    expected = case["plan_locator"]
    assert (block.block_id, block.kind, start, start + len(anchor)) == (
        expected["block_id"],
        expected["block_kind"],
        expected["start"],
        expected["end"],
    )
    assert block.text[start : start + len(anchor)] == expected["anchor_text_verbatim"]
    return PlacementLocator(
        block_id=block.block_id,
        block_kind=block.kind,
        occurrence_index=case["occurrence_index"],
        start=start,
        end=start + len(anchor),
        context_hash=hashlib.sha256(block.text.encode("utf-8")).hexdigest(),
    )


def _check(case: dict[str, Any]) -> FreshnessCheck:
    locator = _locator(case)
    return check_plan_freshness(
        plan_source_version=stale_cases()["plan_version"],
        current_version=case["current_version"],
        locator=locator,
        anchor_text=case["plan_locator"]["anchor_text_verbatim"],
        current_blocks=extract_blocks(case["current_html"]),
    )


def test_fixture_covers_every_verdict_and_reason() -> None:
    assert {case["expected"] for case in CASES} == {v.value for v in PlanFreshness}
    named = {r for case in CASES for r in (*case["reasons_include"], *case["reasons_include_any"])}
    assert named == VOCABULARY
    assert len(IDS) == len(set(IDS))
    assert any(case["expect_block_id_changed"] for case in CASES)


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_freshness_verdict(case: dict[str, Any]) -> None:
    result = _check(case)
    assert result.freshness.value == case["expected"], (case["why"], result)


@pytest.mark.parametrize("case", [_reason_param(c) for c in CASES])
def test_freshness_reasons(case: dict[str, Any]) -> None:
    reasons = set(_check(case).reasons)
    assert reasons <= VOCABULARY, reasons
    assert set(case["reasons_include"]) <= reasons, (case["why"], reasons)
    if case["reasons_include_any"]:
        assert reasons & set(case["reasons_include_any"]), (case["why"], reasons)
    assert not reasons & set(case["reasons_exclude"]), (case["why"], reasons)


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c["id"]) for c in CASES if c["expected"] == "rebasable"],
)
def test_rebasable_block_is_found_by_hash_not_by_id(case: dict[str, Any]) -> None:
    """The current block with the plan's hash is where Phase 6 will rebase the anchor."""
    locator = _locator(case)
    current = extract_blocks(case["current_html"])
    matches = [
        b for b in current if hashlib.sha256(b.text.encode()).hexdigest() == locator.context_hash
    ]
    assert [b.block_id for b in matches] == [case["current_block_id"]]
    assert (
        matches[0].text[locator.start : locator.end] == case["plan_locator"]["anchor_text_verbatim"]
    )
    assert (matches[0].block_id != locator.block_id) is case["expect_block_id_changed"]
