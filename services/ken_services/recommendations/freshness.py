"""The ADR-0011 section 6 freshness check: fresh / rebasable / stale.

* **fresh** -- the plan's source version IS the current version (reasons ``()``).
* **rebasable** -- the version changed (reason ``version_changed``), but a current block
  has the plan's ``context_hash`` (the identical text), the same ``block_kind``, the anchor
  still at ``text[start:end]``, and the span is still placement-safe (inside one run, no
  excluded ancestor or enclosing element: an editor wrapping the phrase in a link or moving
  the paragraph into a table keeps the text but not the safety). The block id is only a
  HINT: when several blocks share the hash, the hinted one is tried first; a block that
  moved (new id, same text) is still rebasable.
* **stale** -- anything else, with reasons, in this order:

  ``version_changed`` always, then exactly one of
  ``block_missing``   no block has the hash, and no block has the hinted id;
  ``context_changed`` no block has the hash but the hinted id exists (its text changed),
                      or the hash matches only blocks whose kind or safety changed;
  ``anchor_moved``    a hash-matching block exists but ``text[start:end]`` is not the anchor.

Pure: the caller supplies ``extract_blocks`` of the CURRENT version's body.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from ken_contracts.recommendations import (
    FreshnessCheck,
    PlacementLocator,
    PlacementPolicy,
    PlanFreshness,
)
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import ContentBlock
from ken_services.recommendations.placement import span_is_safe

__all__ = ["DEFAULT_EXCLUDED_ANCESTORS", "check_plan_freshness"]

DEFAULT_EXCLUDED_ANCESTORS: tuple[str, ...] = PlacementPolicy().excluded_ancestors
"""The contract default; pass the policy's list when it differs."""


def check_plan_freshness(
    *,
    plan_source_version: int,
    current_version: int,
    locator: PlacementLocator,
    anchor_text: str,
    current_blocks: Sequence[ContentBlock],
    excluded_ancestors: Collection[str] = DEFAULT_EXCLUDED_ANCESTORS,
) -> FreshnessCheck:
    """Whether a plan still applies to the source as it is now (module docstring)."""
    if plan_source_version == current_version:
        return FreshnessCheck(freshness=PlanFreshness.FRESH, block_id=locator.block_id)
    matching = [b for b in current_blocks if sha256_hex(b.text) == locator.context_hash]
    matching.sort(key=lambda b: b.block_id != locator.block_id)  # the hint first (stable)
    if not matching:
        hinted = next((b for b in current_blocks if b.block_id == locator.block_id), None)
        if hinted is None:
            return FreshnessCheck(
                freshness=PlanFreshness.STALE, reasons=("version_changed", "block_missing")
            )
        reasons: tuple[str, ...] = ("version_changed", "context_changed")
        if hinted.text[locator.start : locator.end] != anchor_text:
            reasons = (*reasons, "anchor_moved")  # the edit also moved or removed the anchor
        return FreshnessCheck(freshness=PlanFreshness.STALE, reasons=reasons)
    anchored = [b for b in matching if b.text[locator.start : locator.end] == anchor_text]
    if not anchored:
        return FreshnessCheck(
            freshness=PlanFreshness.STALE, reasons=("version_changed", "anchor_moved")
        )
    for block in anchored:
        if block.kind == locator.block_kind and span_is_safe(
            block, locator.start, locator.end, excluded_ancestors=excluded_ancestors
        ):
            return FreshnessCheck(
                freshness=PlanFreshness.REBASABLE,
                reasons=("version_changed",),
                block_id=block.block_id,
            )
    return FreshnessCheck(
        freshness=PlanFreshness.STALE, reasons=("version_changed", "context_changed")
    )
