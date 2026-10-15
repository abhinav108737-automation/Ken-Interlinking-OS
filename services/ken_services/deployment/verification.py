"""Deterministic link verification: :class:`HtmlLinkVerifier` (ADR-0014 sections 1.7, 6, 8).

Implements :class:`ken_services.deployment.protocol.LinkVerifier`. Pure; total.

For each plan, ONE :class:`VerificationCheck` per :class:`VerificationCheckName`, in enum
order. The HTML (CMS body or served page, fragment or full document) is read with the same
parse and block walk as the mutator (:mod:`ken_services.deployment.source_map`).

**Candidates** are the ``<a>`` elements inside the content root, read BEFORE boilerplate
removal (so a hidden link is still seen), that carry our ``data-ken-link-id``, or that carry
no such id and whose ``href`` is equivalent to the plan's and whose text is the plan's
anchor -- identity never depends on ``data-*`` (a CMS sanitiser may strip it), but a link
carrying ANOTHER plan's id is that plan's. The **primary** candidate is the exact
link at the expected block and span when there is one, else the first with our id, else the
first.

==================== ======================================================================
check                passed when
==================== ======================================================================
link_present_once    exactly one candidate (``link_missing`` / ``link_duplicated``)
href_matches         the primary's href is equivalent to the plan's (``href_mismatch``)
anchor_matches       the primary's text equals the anchor verbatim (``anchor_mismatch``)
in_expected_block    the primary's block hashes to ``context_hash`` (``block_changed``) and its
                     text sits at ``[start, end)`` (``span_moved``); a link removed with
                     boilerplate (``hidden``) or outside every block is ``not_in_content``
not_hidden           no ``hidden`` / ``aria-hidden="true"`` / inline ``display:none``,
                     ``visibility:hidden|collapse``, ``content-visibility:hidden`` on the
                     link or any ancestor up to the document (``hidden``)
not_nested           no ``<a>`` ancestor or descendant, and no earlier ``<a>`` left unclosed
                     (a browser would extend it) (``nested``)
target_reachable     ``target_reachable[plan]``: True passed, False failed
                     (``target_unreachable``), None or missing skipped (``not_checked``)
==================== ======================================================================

With no candidate, the element checks are ``skipped`` (``link_missing``): the plan already
fails on ``link_present_once``. An UNSOUND document (``MappedDocument.sound`` false: the parse
or the walk replay cannot be trusted, e.g. a ``<script>`` that a browser would double-escape
past its first ``</script>``) never passes: every element check is ``failed``
(``unsound_document``). An overall run passes iff no check is ``failed`` (and, for the
deployment service, none is ``inconclusive``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from uuid import UUID

from ken_contracts.deployment import (
    VerificationCheck,
    VerificationCheckName,
    VerificationOutcome,
)
from ken_services.deployment.href_rules import hrefs_equivalent
from ken_services.deployment.protocol import LinkInsertion
from ken_services.deployment.source_map import (
    LinkElement,
    LinkState,
    MappedDocument,
    map_document,
)

__all__ = ["DETAILS", "HtmlLinkVerifier"]

_Name = VerificationCheckName
_Outcome = VerificationOutcome

DETAILS: Final = frozenset(
    {
        "link_missing",
        "link_duplicated",
        "href_mismatch",
        "anchor_mismatch",
        "block_changed",
        "span_moved",
        "not_in_content",
        "hidden",
        "nested",
        "target_unreachable",
        "not_checked",
        "unsound_document",
        "internal_error",
    }
)
"""The fixed ``detail`` vocabulary."""


def _check(
    plan: UUID, name: VerificationCheckName, ok: bool, detail: str | None = None
) -> VerificationCheck:
    return VerificationCheck(
        plan_id=plan,
        check=name,
        outcome=_Outcome.PASSED if ok else _Outcome.FAILED,
        detail=None if ok else detail,
    )


def _target(plan: UUID, reachable: dict[UUID, bool | None] | None) -> VerificationCheck:
    value = None if reachable is None else reachable.get(plan)
    if value is None:
        return VerificationCheck(
            plan_id=plan,
            check=_Name.TARGET_REACHABLE,
            outcome=_Outcome.SKIPPED,
            detail="not_checked",
        )
    return _check(plan, _Name.TARGET_REACHABLE, value, "target_unreachable")


def _unsound(plan: UUID, reachable: dict[UUID, bool | None] | None) -> list[VerificationCheck]:
    """No position or block fact of an unsound document is trusted: nothing passes."""
    checks = [
        VerificationCheck(
            plan_id=plan, check=name, outcome=_Outcome.FAILED, detail="unsound_document"
        )
        for name in VerificationCheckName
        if name is not _Name.TARGET_REACHABLE
    ]
    checks.append(_target(plan, reachable))
    return checks


def _expected_link(doc: MappedDocument, item: LinkInsertion) -> LinkElement | None:
    """Our exact link at the plan's block and span (the block-hash identity), if any."""
    found: list[LinkElement] = []
    for block in doc.blocks_with_hash(item.context_hash):
        if block.block.text[item.start : item.end] != item.anchor_text:
            continue
        state, tag = doc.link_at(block.locate(item.start, item.end, doc))
        link = None if tag is None else doc.link_for_tag(tag)
        if state is LinkState.EXACT and link is not None and hrefs_equivalent(link.href, item.href):
            found.append(link)
    if len(found) > 1:
        tagged = [link for link in found if link.link_id == str(item.plan_id)]
        found = tagged or found
    return found[0] if found else None


def _in_expected_block(
    doc: MappedDocument, item: LinkInsertion, link: LinkElement
) -> tuple[bool, str | None]:
    located = doc.link_span(link.tag)
    if located is None:
        return False, "not_in_content"
    block, span = located
    if block.context_hash != item.context_hash:
        return False, "block_changed"
    if span is None:  # NFC not part-local: fall back to the text at the span
        return block.block.text[item.start : item.end] == link.text, "span_moved"
    return span == (item.start, item.end), "span_moved"


class HtmlLinkVerifier:
    """The reference :class:`~ken_services.deployment.protocol.LinkVerifier`. Stateless."""

    def verify_links(
        self,
        html: str,
        insertions: Sequence[LinkInsertion],
        *,
        target_reachable: dict[UUID, bool | None] | None = None,
    ) -> list[VerificationCheck]:
        items = tuple(insertions)
        try:
            doc = map_document(html)
            checks: list[VerificationCheck] = []
            for item in items:
                if doc.sound:
                    checks.extend(self._verify_one(doc, item, target_reachable))
                else:
                    checks.extend(_unsound(item.plan_id, target_reachable))
            return checks
        except Exception:  # pragma: no cover - totality guard: never raise on any input
            out: list[VerificationCheck] = []
            for item in items:
                out.extend(
                    VerificationCheck(
                        plan_id=item.plan_id,
                        check=name,
                        outcome=_Outcome.FAILED,
                        detail="internal_error",
                    )
                    for name in VerificationCheckName
                    if name is not _Name.TARGET_REACHABLE
                )
                out.append(_target(item.plan_id, target_reachable))
            return out

    @staticmethod
    def _verify_one(
        doc: MappedDocument,
        item: LinkInsertion,
        target_reachable: dict[UUID, bool | None] | None,
    ) -> list[VerificationCheck]:
        plan = item.plan_id
        plan_key = str(plan)
        candidates = [
            link
            for link in doc.links
            if link.in_root
            and (
                link.link_id == plan_key
                or (
                    link.link_id is None  # another plan's id: that link is not ours
                    and hrefs_equivalent(link.href, item.href)
                    and link.text == item.anchor_text
                )
            )
        ]
        expected = _expected_link(doc, item)
        primary: LinkElement | None = None
        if expected is not None and any(c.tag is expected.tag for c in candidates):
            primary = expected
        elif candidates:
            primary = next((c for c in candidates if c.link_id == plan_key), candidates[0])

        count = len(candidates)
        checks = [
            _check(
                plan,
                _Name.LINK_PRESENT_ONCE,
                count == 1,
                "link_missing" if count == 0 else "link_duplicated",
            )
        ]
        if primary is None:
            checks.extend(
                VerificationCheck(
                    plan_id=plan, check=name, outcome=_Outcome.SKIPPED, detail="link_missing"
                )
                for name in (
                    _Name.HREF_MATCHES,
                    _Name.ANCHOR_MATCHES,
                    _Name.IN_EXPECTED_BLOCK,
                    _Name.NOT_HIDDEN,
                    _Name.NOT_NESTED,
                )
            )
        else:
            in_block, block_detail = _in_expected_block(doc, item, primary)
            start = primary.source.start if primary.source is not None else len(doc.source)
            nested = primary.nested or doc.unclosed_link_before(start, ignore=primary.tag)
            checks.extend(
                [
                    _check(
                        plan,
                        _Name.HREF_MATCHES,
                        hrefs_equivalent(primary.href, item.href),
                        "href_mismatch",
                    ),
                    _check(
                        plan,
                        _Name.ANCHOR_MATCHES,
                        primary.text == item.anchor_text,
                        "anchor_mismatch",
                    ),
                    _check(plan, _Name.IN_EXPECTED_BLOCK, in_block, block_detail),
                    _check(plan, _Name.NOT_HIDDEN, not primary.hidden, "hidden"),
                    _check(plan, _Name.NOT_NESTED, not nested, "nested"),
                ]
            )
        checks.append(_target(plan, target_reachable))
        return checks
