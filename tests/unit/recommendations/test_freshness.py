"""ADR-0011 section 6: fresh / rebasable / stale, with the block found by its text."""

from __future__ import annotations

from ken_contracts.recommendations import FreshnessCheck, PlacementLocator, PlanFreshness
from ken_services.recommendations.freshness import check_plan_freshness
from ken_services.recommendations.placement import find_placement
from tests.unit.recommendations._support import SOLAR, blocks, policy

INTRO = "<p>Opening paragraph about nothing in particular.</p>"
TARGET_P = "<p>Demand for solar power is rising.</p>"
V1 = INTRO + TARGET_P


def _locator(body: str = V1) -> PlacementLocator:
    found = find_placement(
        blocks=blocks(body),
        phrases=(SOLAR,),
        links_per_block={},
        blocks_linking_target=(),
        taken_spans=(),
        salience={},
        policy=policy(),
    )
    assert found is not None
    return found.locator


def _check(
    current_body: str,
    *,
    locator: PlacementLocator | None = None,
    anchor: str = "solar power",
    plan_version: int = 1,
    current_version: int = 2,
    **kwargs: object,
) -> FreshnessCheck:
    return check_plan_freshness(
        plan_source_version=plan_version,
        current_version=current_version,
        locator=locator or _locator(),
        anchor_text=anchor,
        current_blocks=blocks(current_body),
        **kwargs,  # type: ignore[arg-type]
    )


STALE = PlanFreshness.STALE


def test_same_version_is_fresh_without_looking_at_blocks() -> None:
    result = _check("", plan_version=3, current_version=3)
    assert result.freshness is PlanFreshness.FRESH
    assert result.reasons == ()


def test_new_version_with_the_same_block_is_rebasable() -> None:
    result = _check(V1 + "<p>An appended paragraph.</p>")
    assert result.freshness is PlanFreshness.REBASABLE
    assert result.reasons == ("version_changed",)


def test_block_moved_new_id_same_text_is_rebasable() -> None:
    moved = INTRO + "<p>A new paragraph inserted above.</p><h2>New heading</h2>" + TARGET_P
    assert blocks(moved)[3].text == blocks(V1)[1].text  # the block is now b3, not b1
    assert _check(moved).freshness is PlanFreshness.REBASABLE


def test_edited_block_text_is_stale_context_changed() -> None:
    edited = INTRO + "<p>Demand for solar power is rising quickly.</p>"
    assert _check(edited) == FreshnessCheck(
        freshness=STALE, reasons=("version_changed", "context_changed")
    )


def test_removed_block_is_stale_block_missing() -> None:
    assert _check("<p>Only one paragraph now.</p>") == FreshnessCheck(
        freshness=STALE, reasons=("version_changed", "block_missing")
    )


def test_anchor_not_at_its_offsets_is_stale_anchor_moved() -> None:
    locator = _locator()
    shifted = locator.model_copy(update={"start": locator.start + 1, "end": locator.end + 1})
    assert _check(V1, locator=shifted) == FreshnessCheck(
        freshness=STALE, reasons=("version_changed", "anchor_moved")
    )
    assert _check(V1, anchor="wind power").reasons == ("version_changed", "anchor_moved")


def test_same_text_but_the_anchor_is_now_inside_a_link_is_stale() -> None:
    wrapped = INTRO + '<p>Demand for <a href="/other">solar power</a> is rising.</p>'
    assert blocks(wrapped)[1].text == blocks(V1)[1].text  # same hash
    assert _check(wrapped).reasons == ("version_changed", "context_changed")


def test_same_text_moved_into_a_quote_or_table_or_other_kind_is_stale() -> None:
    for changed in (
        INTRO + "<blockquote><p>Demand for solar power is rising.</p></blockquote>",
        INTRO + "<table><tr><td><p>Demand for solar power is rising.</p></td></tr></table>",
        INTRO + "<ul><li>Demand for solar power is rising.</li></ul>",
    ):
        assert _check(changed).reasons == ("version_changed", "context_changed"), changed


def test_any_block_with_the_hash_can_rebase_the_hint_is_tried_first() -> None:
    twice = INTRO + "<blockquote><p>Demand for solar power is rising.</p></blockquote>" + TARGET_P
    # b1 (the hinted id) is now the quoted copy; b2 is a safe copy of the same text.
    assert _check(twice).freshness is PlanFreshness.REBASABLE


def test_excluded_ancestors_can_be_supplied() -> None:
    styled = INTRO + "<p>Demand for <strong>solar power</strong> is rising.</p>"
    assert _check(styled).freshness is PlanFreshness.REBASABLE
    assert _check(styled, excluded_ancestors=("strong",)).reasons == (
        "version_changed",
        "context_changed",
    )


def test_the_matched_block_id_is_reported() -> None:
    """Phase 6 relocates by this id instead of repeating the search (rebasable: id moved)."""
    locator = _locator()
    assert _check("", locator=locator, plan_version=2, current_version=2).block_id == (
        locator.block_id
    )
    moved = _check("<p>A new opening.</p>" + V1, locator=locator)
    assert moved.freshness is PlanFreshness.REBASABLE
    assert moved.block_id is not None and moved.block_id != locator.block_id
    assert _check(INTRO + "<p>Demand for wind power.</p>", locator=locator).block_id is None
