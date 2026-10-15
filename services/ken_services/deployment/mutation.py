"""Deterministic HTML link mutation: :class:`HtmlMutator` (ADR-0014 sections 2 and 8). Pure.

Implements :class:`ken_services.deployment.protocol.Mutator`. No I/O, no clock, no randomness,
no LLM; total (returns typed issues for any input string, never raises).

**Insert.** For each :class:`LinkInsertion`, in this order (first failure wins):

1. ``href`` passes :func:`href_problem` (scheme, characters, credentials) -> else
   ``href_invalid``; the anchor is non-empty -> else ``anchor_not_found``; a plan id appears
   once per batch -> else ``overlapping_changes``.
2. Locate the block by ``context_hash`` over the inventory block walk of THIS string
   (fragment or full document): none -> ``block_not_found`` (``conflicting_link`` if our
   ``data-ken-link-id`` is on the page); several -> the one already carrying our exact link,
   else the ``block_id`` hint, else ``ambiguous_block``.
3. ``phrase_occurrences(anchor, block.text)[occurrence_index] == (start, end)`` and
   ``block.text[start:end] == anchor_text`` -> else ``anchor_not_found``.
4. Two insertions whose spans intersect in one block -> ``overlapping_changes`` (both).
5. An ``<a>`` already wraps EXACTLY the span: same href and no other plan's id -> already
   applied (a no-op, also without ``data-*``); another href or another plan's
   ``data-ken-link-id`` -> ``conflicting_link``. Our id elsewhere -> ``conflicting_link``.
   An ``<a>`` around part of the span -> ``unsafe_placement``.
6. A hidden ancestor anywhere up to the document (``hidden``, ``aria-hidden="true"``, inline
   ``display:none`` / ``visibility:hidden|collapse`` / ``content-visibility:hidden``) ->
   ``unsafe_placement``; a raw-text ancestor (``xmp``, ``noembed``, ``plaintext``...) ->
   ``unmappable_span``.
7. An excluded element is the block, encloses it, or is an ancestor of the span's text
   (``a`` is always excluded) -> ``unsafe_placement``.
8. The span covers more than one text node (inline tags, ``<br>``, removed comments) ->
   ``anchor_not_in_single_text_node``; ``span_is_safe`` fails -> ``unsafe_placement``.
9. A boundary does not map exactly to the source (inside ``&amp;``, an unsafe NFC cut, an
   unrecorded node) -> ``unmappable_span``; an earlier ``<a>`` never closed by a real
   ``</a>`` (a browser may extend it over our text) -> ``unsafe_placement``.

A document whose walk replay differs from ``extract_blocks`` is unsound: every plan gets
``unmappable_span``.

**Splice.** ``<a href="ESCAPED" data-ken-link-id="UUID">`` before the span and ``</a>`` after
it; every other character is the input's. **Self-check** (else ``self_check_failed`` for
every applied plan): the block list (id, kind, text) is unchanged; the document's
``<a href>`` list equals the input's plus ours; each inserted span is an exact link in its
block with ``a`` in its run; and ``remove_links(output) == input`` byte for byte.

**Remove.** Locates our link like an insertion (block hash, span, href; ``data-ken-link-id``
only a hint, but a link carrying ANOTHER plan's id is never ours) and deletes exactly its
start and end tags. Absent -> already applied (idempotent rollback). Our link changed ->
``rollback_conflict``. See :meth:`HtmlMutator.remove_links`.

All-or-nothing: with any issue, ``html`` is returned unchanged and ``applied`` is empty
(``already_applied`` still lists the no-ops found).
"""

from __future__ import annotations

import itertools
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from bs4 import Tag

from ken_contracts.deployment import SNIPPET_CHARS, DeploymentIssue, DeploymentIssueCode
from ken_services.deployment.href_rules import escape_href, href_problem, hrefs_equivalent
from ken_services.deployment.protocol import LinkInsertion, LinkRemoval, MutationResult
from ken_services.deployment.source_map import (
    LINK_ID_ATTR,
    LinkElement,
    LinkState,
    MappedBlock,
    MappedDocument,
    SpanLocation,
    map_document,
)
from ken_services.recommendations.placement import phrase_occurrences, span_is_safe

__all__ = ["LINK_ANCESTOR", "HtmlMutator", "change_snippets", "link_start_tag"]

LINK_ANCESTOR: Final = "a"
"""Always excluded: a span inside an existing link would nest links."""
END_TAG: Final = "</a>"

_Code = DeploymentIssueCode


def link_start_tag(href: str, plan_id: UUID) -> str:
    """The exact start tag :class:`HtmlMutator` writes (``href`` attribute-escaped)."""
    return f'<a href="{escape_href(href)}" {LINK_ID_ATTR}="{plan_id}">'


def change_snippets(before: str, after: str, *, chars: int = SNIPPET_CHARS) -> tuple[str, str]:
    """Windows of ``before`` / ``after`` around their changed region, ``chars`` of context on
    each side: the dry-run ``ChangePreview.html_before`` / ``html_after`` of ONE insertion
    (call it with a single-insertion result). Display TEXT, never HTML to render. Clamped to
    the contract's limits (``2 * SNIPPET_CHARS + 200`` / ``+ 400``)."""
    limit = min(len(before), len(after))
    prefix = 0
    while prefix < limit and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and before[-1 - suffix] == after[-1 - suffix]:
        suffix += 1
    start = max(0, prefix - chars)
    return (
        before[start : len(before) - suffix + chars][: 2 * SNIPPET_CHARS + 200],
        after[start : len(after) - suffix + chars][: 2 * SNIPPET_CHARS + 400],
    )


@dataclass(frozen=True, slots=True)
class _Located:
    block: MappedBlock
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Splice:
    plan_id: UUID
    block_index: int
    start: int
    end: int
    src_start: int
    src_end: int
    href: str


@dataclass(frozen=True, slots=True)
class _Unwrap:
    plan_id: UUID
    tag: Tag
    ranges: tuple[tuple[int, int], tuple[int, int]]


def _issues(
    plans: Sequence[UUID], codes: dict[int, DeploymentIssueCode]
) -> tuple[DeploymentIssue, ...]:
    return tuple(
        DeploymentIssue(code=codes[i], plan_id=plan) for i, plan in enumerate(plans) if i in codes
    )


def _duplicates(plans: Sequence[UUID]) -> set[int]:
    seen: dict[UUID, int] = {}
    out: set[int] = set()
    for i, plan in enumerate(plans):
        if plan in seen:
            out |= {i, seen[plan]}
        seen.setdefault(plan, i)
    return out


def _anchor_at(block: MappedBlock, anchor: str, occurrence: int, start: int, end: int) -> bool:
    if occurrence < 0 or block.block.text[start:end] != anchor:
        return False
    occurrences = phrase_occurrences(anchor, block.block.text)
    return occurrence < len(occurrences) and occurrences[occurrence] == (start, end)


def _exact_link(doc: MappedDocument, block: MappedBlock, start: int, end: int) -> Tag | None:
    state, tag = doc.link_at(block.locate(start, end, doc))
    return tag if state is LinkState.EXACT else None


def _labels_of(block: MappedBlock, location: SpanLocation) -> frozenset[str]:
    names: set[str] = set()
    for k in location.parts:
        label = block.labels[k]
        if label is not None:
            names |= label
    return frozenset(names)


def _root_name(doc: MappedDocument) -> str | None:
    return None if doc.root.parent is None else doc.root.name


class HtmlMutator:
    """The reference :class:`~ken_services.deployment.protocol.Mutator`. Stateless."""

    # ------------------------------------------------------------------ insert
    def insert_links(
        self,
        html: str,
        insertions: Sequence[LinkInsertion],
        *,
        excluded_ancestors: Collection[str],
    ) -> MutationResult:
        items = tuple(insertions)
        try:
            return self._insert(html, items, frozenset(excluded_ancestors) | {LINK_ANCESTOR})
        except Exception:  # pragma: no cover - totality guard: never raise on any input
            return MutationResult(
                html=html,
                issues=tuple(
                    DeploymentIssue(code=_Code.SELF_CHECK_FAILED, plan_id=i.plan_id) for i in items
                ),
            )

    def _insert(
        self, html: str, items: tuple[LinkInsertion, ...], excluded: frozenset[str]
    ) -> MutationResult:
        if not items:
            return MutationResult(html=html)
        plans = [item.plan_id for item in items]
        codes: dict[int, DeploymentIssueCode] = {}
        for i in _duplicates(plans):
            codes[i] = _Code.OVERLAPPING_CHANGES
        for i, item in enumerate(items):
            if i in codes:
                continue
            if href_problem(item.href) is not None:
                codes[i] = _Code.HREF_INVALID
            elif not item.anchor_text.strip():
                codes[i] = _Code.ANCHOR_NOT_FOUND
        doc = map_document(html)
        if not doc.sound:
            return MutationResult(
                html=html,
                issues=_issues(plans, dict.fromkeys(range(len(items)), _Code.UNMAPPABLE_SPAN)),
            )

        located: dict[int, _Located] = {}
        for i, item in enumerate(items):
            if i in codes:
                continue
            found = self._locate_insertion(doc, item)
            if isinstance(found, _Located):
                located[i] = found
            else:
                codes[i] = found
        for i, a in located.items():
            for j, b in located.items():
                if i < j and a.block is b.block and a.start < b.end and b.start < a.end:
                    codes[i] = codes[j] = _Code.OVERLAPPING_CHANGES

        splices: list[_Splice] = []
        already: list[UUID] = []
        for i, place in located.items():
            if i in codes:
                continue
            outcome = self._plan_insertion(doc, items[i], place, excluded)
            if outcome is None:
                already.append(items[i].plan_id)
            elif isinstance(outcome, _Splice):
                splices.append(outcome)
            else:
                codes[i] = outcome
        already_applied = tuple(p for p in plans if p in set(already))
        if codes:
            return MutationResult(
                html=html, already_applied=already_applied, issues=_issues(plans, codes)
            )
        if not splices:
            return MutationResult(html=html, already_applied=already_applied)

        out = _splice(html, splices)
        if out is None or not self._insert_self_check(html, doc, out, splices):
            failed = {
                i for i, item in enumerate(items) if item.plan_id in {s.plan_id for s in splices}
            }
            return MutationResult(
                html=html,
                already_applied=already_applied,
                issues=_issues(plans, dict.fromkeys(failed, _Code.SELF_CHECK_FAILED)),
            )
        applied_ids = {s.plan_id for s in splices}
        return MutationResult(
            html=out,
            applied=tuple(p for p in plans if p in applied_ids),
            already_applied=already_applied,
        )

    @staticmethod
    def _locate_insertion(
        doc: MappedDocument, item: LinkInsertion
    ) -> _Located | DeploymentIssueCode:
        matches = doc.blocks_with_hash(item.context_hash)
        if not matches:
            return (
                _Code.CONFLICTING_LINK
                if doc.links_with_id(str(item.plan_id))
                else _Code.BLOCK_NOT_FOUND
            )
        valid = [
            block
            for block in matches
            if _anchor_at(block, item.anchor_text, item.occurrence_index, item.start, item.end)
        ]
        chosen: MappedBlock | None = None
        if len(matches) == 1:
            chosen = matches[0]
        else:
            applied = [
                block
                for block in valid
                if (tag := _exact_link(doc, block, item.start, item.end)) is not None
                and hrefs_equivalent(_tag_href(doc, tag), item.href)
            ]
            if len(applied) == 1:
                chosen = applied[0]
            else:
                hinted = [block for block in matches if block.block.block_id == item.block_id_hint]
                if len(hinted) != 1:
                    return _Code.AMBIGUOUS_BLOCK
                chosen = hinted[0]
        if chosen not in valid:
            return _Code.ANCHOR_NOT_FOUND
        return _Located(block=chosen, start=item.start, end=item.end)

    @staticmethod
    def _plan_insertion(
        doc: MappedDocument, item: LinkInsertion, place: _Located, excluded: frozenset[str]
    ) -> _Splice | DeploymentIssueCode | None:
        """A splice, an issue, or ``None`` (our link is already there)."""
        block, start, end = place.block, place.start, place.end
        location = block.locate(start, end, doc)
        ours = doc.links_with_id(str(item.plan_id))
        state, tag = doc.link_at(location)
        if state is LinkState.EXACT and tag is not None:
            link = doc.link_for_tag(tag)
            mine = link is not None and link.link_id in {None, str(item.plan_id)}
            same_href = link is not None and hrefs_equivalent(link.href, item.href)
            if mine and same_href and all(other.tag is tag for other in ours):
                return None
            return _Code.CONFLICTING_LINK  # another href, or another plan's link
        if ours:
            return _Code.CONFLICTING_LINK
        if state is LinkState.PARTIAL:
            return _Code.UNSAFE_PLACEMENT
        if location.problem == "nfc_not_local" or location.problem == "range":
            return _Code.UNMAPPABLE_SPAN
        if any(doc.text_hidden.get(id(node), False) for node in location.nodes):
            return _Code.UNSAFE_PLACEMENT
        if any(doc.text_raw.get(id(node), False) for node in location.nodes):
            return _Code.UNMAPPABLE_SPAN
        content = block.block
        names = {content.kind} | content.enclosing | _labels_of(block, location)
        root_name = _root_name(doc)
        if root_name is not None:
            names.add(root_name)
        if names & excluded:
            return _Code.UNSAFE_PLACEMENT
        if not location.single:
            return _Code.ANCHOR_NOT_IN_SINGLE_TEXT_NODE
        if not span_is_safe(content, start, end, excluded_ancestors=excluded):
            return _Code.UNSAFE_PLACEMENT
        if location.src_start is None or location.src_end is None or location.problem:
            return _Code.UNMAPPABLE_SPAN
        if doc.unclosed_link_before(location.src_start):
            return _Code.UNSAFE_PLACEMENT
        return _Splice(
            plan_id=item.plan_id,
            block_index=block.index,
            start=start,
            end=end,
            src_start=location.src_start,
            src_end=location.src_end,
            href=item.href,
        )

    def _insert_self_check(
        self, html: str, doc: MappedDocument, out: str, splices: list[_Splice]
    ) -> bool:
        new = map_document(out)  # sound => its blocks ARE extract_blocks(out)
        if not new.sound or _block_keys(new) != _block_keys(doc):
            return False
        ids = {str(s.plan_id) for s in splices}
        if [sig for sig in new.a_href_signature() if sig[1] not in ids] != doc.a_href_signature():
            return False
        removals: list[LinkRemoval] = []
        for splice in splices:
            block = new.blocks[splice.block_index]
            tag = _exact_link(new, block, splice.start, splice.end)
            link = None if tag is None else new.link_for_tag(tag)
            if link is None or link.href != splice.href or link.link_id != str(splice.plan_id):
                return False
            run = next(
                (r for r in block.block.runs if r.start <= splice.start and splice.end <= r.end),
                None,
            )
            if run is None or LINK_ANCESTOR not in run.ancestors:
                return False
            removals.append(
                LinkRemoval(
                    plan_id=splice.plan_id,
                    expected_href=splice.href,
                    expected_anchor_text=block.block.text[splice.start : splice.end],
                    context_hash=block.context_hash,
                    block_id_hint=block.block.block_id,
                    start=splice.start,
                    end=splice.end,
                )
            )
        undone, _removed = self._remove_planned(out, new, tuple(removals))
        return undone.ok and len(undone.applied) == len(splices) and undone.html == html

    # ------------------------------------------------------------------ remove
    def remove_links(self, html: str, removals: Sequence[LinkRemoval]) -> MutationResult:
        """Unwrap OUR links (inverse splice) on the CURRENT html, all-or-nothing.

        Per removal: the block(s) with ``context_hash`` whose ``text[start:end]`` is the
        expected anchor, holding an EXACT link with the expected href (``data-ken-link-id``
        only breaks ties and confirms) -> unwrapped. If the block changed but our id is on
        exactly one intact link (expected href and anchor, exact markup) -> unwrapped (safe
        after later edits to the same paragraph). Otherwise: our id elsewhere, a different or
        partial link at the span, or (block gone) any link to our href still in the content
        -> ``rollback_conflict``; nothing of ours there -> already applied.
        """
        items = tuple(removals)
        try:
            doc = map_document(html)
            result, removed = self._remove_planned(html, doc, items)
            if result.applied and not self._remove_self_check(doc, result.html, removed):
                return MutationResult(
                    html=html,
                    issues=tuple(
                        DeploymentIssue(code=_Code.SELF_CHECK_FAILED, plan_id=p)
                        for p in result.applied
                    ),
                )
            return result
        except Exception:  # pragma: no cover - totality guard
            return MutationResult(
                html=html,
                issues=tuple(
                    DeploymentIssue(code=_Code.SELF_CHECK_FAILED, plan_id=r.plan_id) for r in items
                ),
            )

    def _remove_planned(
        self, html: str, doc: MappedDocument, items: tuple[LinkRemoval, ...]
    ) -> tuple[MutationResult, tuple[Tag, ...]]:
        """The removal result and the unwrapped elements (empty unless applied)."""
        if not items:
            return MutationResult(html=html), ()
        plans = [item.plan_id for item in items]
        codes: dict[int, DeploymentIssueCode] = dict.fromkeys(
            _duplicates(plans), _Code.OVERLAPPING_CHANGES
        )
        if not doc.sound:
            unsound = dict.fromkeys(range(len(items)), _Code.UNMAPPABLE_SPAN)
            return MutationResult(html=html, issues=_issues(plans, unsound)), ()
        unwraps: dict[int, _Unwrap] = {}
        absent: list[UUID] = []
        for i, item in enumerate(items):
            if i in codes:
                continue
            outcome = _plan_removal(doc, item)
            if outcome is None:
                absent.append(item.plan_id)
            elif isinstance(outcome, _Unwrap):
                unwraps[i] = outcome
            else:
                codes[i] = outcome
        for i, a in unwraps.items():
            for j, b in unwraps.items():
                if i < j and a.tag is b.tag:
                    codes[i] = codes[j] = _Code.OVERLAPPING_CHANGES
        already_applied = tuple(p for p in plans if p in set(absent))
        if codes:
            issues = _issues(plans, codes)
            return MutationResult(html=html, already_applied=already_applied, issues=issues), ()
        cuts = sorted(r for unwrap in unwraps.values() for r in unwrap.ranges)
        if any(a[1] > b[0] for a, b in itertools.pairwise(cuts)):  # pragma: no cover
            failed = dict.fromkeys(unwraps, _Code.SELF_CHECK_FAILED)
            return MutationResult(html=html, issues=_issues(plans, failed)), ()
        pieces: list[str] = []
        cursor = 0
        for begin, end in cuts:
            pieces.append(html[cursor:begin])
            cursor = end
        pieces.append(html[cursor:])
        removed = {unwrap.plan_id for unwrap in unwraps.values()}
        result = MutationResult(
            html="".join(pieces),
            applied=tuple(p for p in plans if p in removed),
            already_applied=already_applied,
        )
        return result, tuple(unwrap.tag for unwrap in unwraps.values())

    @staticmethod
    def _remove_self_check(doc: MappedDocument, out: str, removed: tuple[Tag, ...]) -> bool:
        """Blocks unchanged; the ``<a href>`` list is the input's minus exactly ``removed``."""
        new = map_document(out)
        if not new.sound or _block_keys(new) != _block_keys(doc):
            return False
        gone = {id(tag) for tag in removed}
        expected = [
            (link.href, link.link_id)
            for link in doc.links
            if link.href is not None and id(link.tag) not in gone
        ]
        return new.a_href_signature() == expected


def _block_keys(doc: MappedDocument) -> list[tuple[str, str, str]]:
    return [(b.block.block_id, b.block.kind, b.block.text) for b in doc.blocks]


def _tag_href(doc: MappedDocument, tag: Tag) -> str | None:
    link = doc.link_for_tag(tag)
    return None if link is None else link.href


def _plan_removal(doc: MappedDocument, item: LinkRemoval) -> _Unwrap | DeploymentIssueCode | None:
    key = str(item.plan_id)
    ours = doc.links_with_id(key)
    matches = [
        block
        for block in doc.blocks_with_hash(item.context_hash)
        if block.block.text[item.start : item.end] == item.expected_anchor_text
        and item.expected_anchor_text
    ]
    exact: list[tuple[MappedBlock, Tag]] = []
    foreign = False
    for block in matches:
        location = block.locate(item.start, item.end, doc)
        if location.problem in {"nfc_not_local", "range"}:
            return _Code.UNMAPPABLE_SPAN
        state, tag = doc.link_at(location)
        link = None if tag is None else doc.link_for_tag(tag)
        if state is LinkState.EXACT and link is not None and link.link_id not in {None, key}:
            continue  # another plan's link occupies the span: ours is not there
        if (
            state is LinkState.EXACT
            and tag is not None
            and link is not None
            and hrefs_equivalent(link.href, item.expected_href)
        ):
            exact.append((block, tag))
            continue
        if state is not LinkState.NONE:
            foreign = True
    if len(exact) > 1:
        tagged = [pair for pair in exact if any(link.tag is pair[1] for link in ours)]
        hinted = [pair for pair in exact if pair[0].block.block_id == item.block_id_hint]
        if len(tagged) == 1:
            exact = tagged
        elif len(hinted) == 1:
            exact = hinted
        else:
            return _Code.AMBIGUOUS_BLOCK
    if len(exact) == 1:
        tag = exact[0][1]
        if any(link.tag is not tag for link in ours):
            return _Code.ROLLBACK_CONFLICT
        return _unwrap(doc, item.plan_id, tag)
    if ours:
        if not matches and len(ours) == 1 and _intact(doc, ours[0], item):
            return _unwrap(doc, item.plan_id, ours[0].tag)
        return _Code.ROLLBACK_CONFLICT
    if foreign:
        return _Code.ROLLBACK_CONFLICT
    if not matches and any(
        link.in_root
        and link.link_id in {None, key}
        and hrefs_equivalent(link.href, item.expected_href)
        for link in doc.links
    ):
        # The block changed and a link to our target remains: it may be ours, edited (its
        # data-* stripped), so removing nothing would silently leave it. Refuse.
        return _Code.ROLLBACK_CONFLICT
    return None


def _intact(doc: MappedDocument, link: LinkElement, item: LinkRemoval) -> bool:
    return (
        hrefs_equivalent(link.href, item.expected_href)
        and link.text == item.expected_anchor_text
        and doc.unwrap_ranges(link.tag) is not None
    )


def _unwrap(doc: MappedDocument, plan_id: UUID, tag: Tag) -> _Unwrap | DeploymentIssueCode:
    ranges = doc.unwrap_ranges(tag)
    if ranges is None:
        return _Code.ROLLBACK_CONFLICT
    return _Unwrap(plan_id=plan_id, tag=tag, ranges=ranges)


def _splice(html: str, splices: list[_Splice]) -> str | None:
    ordered = sorted(splices, key=lambda s: (s.src_start, s.src_end))
    for a, b in itertools.pairwise(ordered):
        if a.src_end > b.src_start:
            return None
    pieces: list[str] = []
    cursor = 0
    for splice in ordered:
        pieces.append(html[cursor : splice.src_start])
        pieces.append(link_start_tag(splice.href, splice.plan_id))
        pieces.append(html[splice.src_start : splice.src_end])
        pieces.append(END_TAG)
        cursor = splice.src_end
    pieces.append(html[cursor:])
    return "".join(pieces)
