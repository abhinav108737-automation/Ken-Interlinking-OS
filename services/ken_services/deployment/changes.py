"""``LinkChange`` <-> deterministic mutation (ADR-0014 s2): :class:`ChangeEngine`.

The CMS contract carries a change as (block ``context_hash``, ``occurrence_index``,
``anchor_text``, ``href``) plus a ``block_selector`` hint; the pure ``Mutator`` protocol
(``protocol.py``) takes exact block-text offsets. :func:`locate` derives them the way the
engine placed the anchor -- the block whose text hash is ``context_hash`` (every such block
has the SAME text, so the offsets do not depend on which one the mutator picks) and
``phrase_occurrences(anchor, text)[occurrence_index]`` -- and :class:`ChangeEngine` wraps an
injected ``Mutator`` into the provider-facing :class:`~ken_core.providers.cms.html_engine.
LinkHtmlEngine`. Pure; never raises on malformed HTML.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from ken_contracts.cms import LinkChange, LinkOperation, ValidationIssue
from ken_contracts.deployment import DeploymentIssue, DeploymentIssueCode
from ken_core.providers.cms.html_engine import HtmlOutcome
from ken_services.deployment.protocol import LinkInsertion, LinkRemoval, Mutator
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import extract_blocks
from ken_services.recommendations.freshness import DEFAULT_EXCLUDED_ANCESTORS
from ken_services.recommendations.placement import phrase_occurrences

__all__ = [
    "ISSUE_MESSAGES",
    "UNSUPPORTED_OPERATION",
    "ChangeEngine",
    "Located",
    "insertion_for",
    "locate",
    "removal_anchor",
    "removal_for",
    "to_validation_issues",
]

UNSUPPORTED_OPERATION: Final = "unsupported_operation"
_C = DeploymentIssueCode
ISSUE_MESSAGES: Final[dict[str, str]] = {
    _C.BLOCK_NOT_FOUND.value: "No block of the page has the planned block text.",
    _C.AMBIGUOUS_BLOCK.value: "Several blocks have the planned block text.",
    _C.ANCHOR_NOT_FOUND.value: "The anchor text is not at the planned occurrence.",
    _C.ANCHOR_NOT_IN_SINGLE_TEXT_NODE.value: "The anchor does not lie in one text node.",
    _C.UNMAPPABLE_SPAN.value: "The anchor span cannot be mapped to the page HTML.",
    _C.UNSAFE_PLACEMENT.value: "The anchor sits under an excluded, hidden or link ancestor.",
    _C.OVERLAPPING_CHANGES.value: "Two changes overlap.",
    _C.ALREADY_APPLIED.value: "The change is already in that state.",
    _C.CONFLICTING_LINK.value: "A different link already occupies the span.",
    _C.SELF_CHECK_FAILED.value: "The mutation self-check failed.",
    _C.ROLLBACK_CONFLICT.value: "Our link was changed on the page since deployment.",
    UNSUPPORTED_OPERATION: "This change operation is not supported.",
}


@dataclass(frozen=True, slots=True)
class Located:
    block_id_hint: str
    start: int
    end: int


def locate(html: str, change: LinkChange) -> Located | DeploymentIssueCode:
    """Block-text offsets of ``change``'s anchor in ``html`` (module docstring)."""
    context_hash = change.locator.context_hash
    if not context_hash:
        return _C.BLOCK_NOT_FOUND
    anchor = (
        change.anchor_text
        if change.operation is LinkOperation.INSERT
        else (change.previous_anchor_text or change.anchor_text)
    )
    matching = [b for b in extract_blocks(html) if sha256_hex(b.text) == context_hash]
    if not matching:
        return _C.BLOCK_NOT_FOUND
    occurrences = phrase_occurrences(anchor, matching[0].text)
    index = change.locator.occurrence_index
    if index >= len(occurrences):
        return _C.ANCHOR_NOT_FOUND
    start, end = occurrences[index]
    return Located(block_id_hint=change.locator.block_selector, start=start, end=end)


def insertion_for(change: LinkChange, located: Located) -> LinkInsertion:
    return LinkInsertion(
        plan_id=change.change_id,
        href=change.href,
        anchor_text=change.anchor_text,
        context_hash=change.locator.context_hash or "",
        block_id_hint=located.block_id_hint,
        occurrence_index=change.locator.occurrence_index,
        start=located.start,
        end=located.end,
    )


def removal_anchor(change: LinkChange) -> str:
    """The anchor text of OUR link that ``change`` removes (see :func:`removal_for`)."""
    if change.operation is LinkOperation.INSERT:
        return change.anchor_text
    return change.previous_anchor_text or change.anchor_text


def removal_for(change: LinkChange, located: Located) -> LinkRemoval:
    """Our link of ``change``: a REMOVE names it in ``previous_*``; an INSERT (reverted)
    describes it in ``href`` / ``anchor_text``."""
    if change.operation is LinkOperation.INSERT:
        href, anchor = change.href, change.anchor_text
    else:
        href = change.previous_href or change.href
        anchor = change.previous_anchor_text or change.anchor_text
    return LinkRemoval(
        plan_id=change.change_id,
        expected_href=href,
        expected_anchor_text=anchor,
        context_hash=change.locator.context_hash or "",
        block_id_hint=located.block_id_hint,
        start=located.start,
        end=located.end,
    )


def _issue(code: str, change_id: UUID | None) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity="error",
        message=ISSUE_MESSAGES.get(code, "The change was refused."),
        change_id=change_id,
    )


def to_validation_issues(issues: Sequence[DeploymentIssue]) -> tuple[ValidationIssue, ...]:
    return tuple(_issue(DeploymentIssueCode(i.code).value, i.plan_id) for i in issues)


class ChangeEngine:
    """:class:`~ken_core.providers.cms.html_engine.LinkHtmlEngine` over a ``Mutator``."""

    def __init__(
        self,
        mutator: Mutator,
        *,
        excluded_ancestors: Collection[str] = DEFAULT_EXCLUDED_ANCESTORS,
    ) -> None:
        self._mutator = mutator
        self._excluded = tuple(excluded_ancestors)

    def apply(self, html: str, changes: Sequence[LinkChange]) -> HtmlOutcome:
        inserts = [c for c in changes if c.operation is LinkOperation.INSERT]
        removes = [c for c in changes if c.operation is LinkOperation.REMOVE]
        unsupported = [c for c in changes if c.operation is LinkOperation.REPLACE]
        if unsupported:
            return HtmlOutcome(
                html=html,
                issues=tuple(_issue(UNSUPPORTED_OPERATION, c.change_id) for c in unsupported),
            )
        current = html
        applied: list[UUID] = []
        already: list[UUID] = []
        if removes:
            removed = self._remove(current, removes)
            if not removed.ok:
                return HtmlOutcome(html=html, issues=removed.issues)
            current = removed.html
            applied += removed.applied
            already += removed.already
        if inserts:
            inserted = self._insert(current, inserts)
            if not inserted.ok:
                return HtmlOutcome(html=html, issues=inserted.issues)
            current = inserted.html
            applied += inserted.applied
            already += inserted.already
        return HtmlOutcome(html=current, applied=tuple(applied), already=tuple(already))

    def revert(self, html: str, changes: Sequence[LinkChange]) -> HtmlOutcome:
        return self._remove(html, [c for c in changes if c.operation is LinkOperation.INSERT])

    # ----------------------------------------------------------------- internals
    def _insert(self, html: str, changes: Sequence[LinkChange]) -> HtmlOutcome:
        insertions: list[LinkInsertion] = []
        issues: list[ValidationIssue] = []
        for change in changes:
            located = locate(html, change)
            if isinstance(located, DeploymentIssueCode):
                issues.append(_issue(located.value, change.change_id))
            else:
                insertions.append(insertion_for(change, located))
        if issues:
            return HtmlOutcome(html=html, issues=tuple(issues))
        result = self._mutator.insert_links(html, insertions, excluded_ancestors=self._excluded)
        if not result.ok:
            return HtmlOutcome(html=html, issues=to_validation_issues(result.issues))
        return HtmlOutcome(
            html=result.html, applied=tuple(result.applied), already=tuple(result.already_applied)
        )

    def _remove(self, html: str, changes: Sequence[LinkChange]) -> HtmlOutcome:
        removals: list[LinkRemoval] = []
        absent: list[UUID] = []
        for change in changes:
            located = locate(html, change)
            if located is _C.BLOCK_NOT_FOUND:
                # Our link adds no text: when its block text is gone, a later edit changed the
                # paragraph. Only the MUTATOR can tell whether OUR link is still there and
                # intact (found by its id, href and anchor; it refuses with rollback_conflict
                # when it cannot prove that), so hand it the removal with the ORIGINAL block
                # hash; the span offsets are unused when no block has that hash.
                anchor = removal_anchor(change)
                located = Located(
                    block_id_hint=change.locator.block_selector, start=0, end=len(anchor)
                )
            elif isinstance(located, DeploymentIssueCode):
                return HtmlOutcome(
                    html=html, issues=(_issue(_C.ROLLBACK_CONFLICT.value, change.change_id),)
                )
            removals.append(removal_for(change, located))
        if not removals:
            return HtmlOutcome(html=html, already=tuple(absent))
        result = self._mutator.remove_links(html, removals)
        if not result.ok:
            return HtmlOutcome(html=html, issues=to_validation_issues(result.issues))
        return HtmlOutcome(
            html=result.html, applied=tuple(result.applied), already=tuple(result.already_applied)
        )
