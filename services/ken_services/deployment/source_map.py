"""Exact source positions for the inventory block walk (ADR-0014 sections 2 and 8). Pure.

:func:`map_document` parses an HTML string ONCE with the inventory's parser (BeautifulSoup over
stdlib ``html.parser`` with ``convert_charrefs=False``, exactly as ``make_soup`` builds it),
instrumented to record where every token came from:

* every text node keeps its **pieces**: verbatim data runs (``source[a:b]`` == the decoded
  text) and character/entity references (atomic: a boundary may fall before or after one,
  never inside);
* every start tag keeps its source offset and verbatim text, and every ``<a>`` records
  whether a real ``</a>`` end tag closed it (``html.parser`` silently closes an unclosed
  ``<a>`` at its parent's end tag, a browser may not).

It then selects the content root and strips boilerplate with the normaliser's own functions
and replays the normaliser's block walk, keeping, for every part of a block's raw text, the
text node it came from. The result is cross-checked against :func:`extract_blocks` on the
same string: any difference (id, kind, text, runs, enclosing) makes the document
**unsound**, and callers refuse instead of guessing.

Block-text offsets map back to source offsets in three exact steps:

1. collapsed text -> NFC text: the ``i``-th non-space character of the block text is a fixed
   character of ``NFC(raw)`` (a start boundary is placed before the anchor's first character,
   an end boundary after its last, so collapsed whitespace never needs mapping);
2. NFC text -> raw text of ONE part: NFC must be part-local for the block, and inside the
   part a boundary must be a safe cut (``NFC(raw[:r]) + NFC(raw[r:]) == NFC(raw)``);
3. raw part -> source: through the text node's pieces.

Anything uncertain returns ``None``; nothing here guesses or raises on malformed input.
"""

from __future__ import annotations

import bisect
import itertools
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, cast

from bs4 import BeautifulSoup, Tag
from bs4.builder._htmlparser import BeautifulSoupHTMLParser, HTMLParserTreeBuilder
from bs4.element import NavigableString, PageElement
from bs4.exceptions import ParserRejectedMarkup

from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import (
    _BREAKING_TAGS,
    ANCHOR_SCAN_MAX_NODES,
    BLOCK_TAGS,
    CONTAINER_TAGS,
    DOCUMENT_BLOCK_KIND,
    ContentBlock,
    _attr,
    _collapse,
    _is_text,
    _scrub,
    _select_root,
    _strip_boilerplate,
    _text_of,
    _text_runs,
    extract_blocks,
)

__all__ = [
    "LINK_ID_ATTR",
    "RAW_TEXT_TAGS",
    "LinkElement",
    "LinkState",
    "MappedBlock",
    "MappedDocument",
    "SpanLocation",
    "map_document",
    "script_double_escapes",
    "style_hides",
]

LINK_ID_ATTR: Final = "data-ken-link-id"
RAW_TEXT_TAGS: Final = frozenset(
    {
        "xmp",
        "noembed",
        "noframes",
        "plaintext",
        "textarea",
        "title",
        "iframe",
        "script",
        "style",
        "template",
        "noscript",
    }
)
"""Elements whose content ``html.parser`` reads as raw text: markup spliced inside them would
be text, never an element."""
NFC_CHUNK_MAX: Final = 512
"""A non-NFC run longer than this is never searched for a safe cut (it is refused)."""
_END_A_RE: Final = re.compile(r"</[aA][\t\n\r\f ]*>")
_CSS_COMMENT_RE: Final = re.compile(r"/\*.*?\*/", re.DOTALL)
_IMPORTANT_RE: Final = re.compile(r"!\s*important\s*$")
_NON_ASCII_RE: Final = re.compile(r"[^\x00-\x7f]+")
_SCRIPT_DOUBLE_ESCAPE_RE: Final = re.compile(r"<!--.*?<script", re.IGNORECASE | re.DOTALL)
"""HTML5 "script data double escaped" entry: inside ``<script>`` raw text, ``<!--`` and later
``<script``. A browser then keeps reading script text past the first ``</script>``, while
``html.parser`` ends the element there (a parser differential). Deliberately conservative:
an intervening ``-->`` (which would leave the escaped state) is NOT honoured."""


# --------------------------------------------------------------------------- #
# Style and hidden-ancestor rules
# --------------------------------------------------------------------------- #
def style_hides(style: str | None) -> bool:
    """An inline ``style`` hides its element: ``display:none``, ``visibility:hidden`` or
    ``collapse``, ``content-visibility:hidden``. Case- and whitespace-insensitive, CSS
    comments and ``!important`` ignored. Not modelled: opacity, zero size, off-screen
    positioning, stylesheets (ADR-0014 section 6)."""
    if not style:
        return False
    for declaration in _CSS_COMMENT_RE.sub("", style).split(";"):
        name, separator, value = declaration.partition(":")
        if not separator:
            continue
        prop = "".join(name.split()).lower()
        val = _IMPORTANT_RE.sub("", " ".join(value.split()).lower()).strip()
        if prop == "display" and val == "none":
            return True
        if prop == "visibility" and val in {"hidden", "collapse"}:
            return True
        if prop == "content-visibility" and val == "hidden":
            return True
    return False


def script_double_escapes(text: str) -> bool:
    """``<script>`` raw text that may enter the HTML5 double-escaped state, where a browser
    and ``html.parser`` disagree on where the element ends (the document is then unsound)."""
    return _SCRIPT_DOUBLE_ESCAPE_RE.search(text) is not None


def _self_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden"):
        return True
    if (_attr(tag, "aria-hidden") or "").strip().lower() == "true":
        return True
    return style_hides(_attr(tag, "style"))


# --------------------------------------------------------------------------- #
# Instrumented parse
# --------------------------------------------------------------------------- #
type _RawPiece = tuple[int, int, str, bool]
"""``(source start, source end, decoded text, verbatim)``."""


@dataclass(slots=True)
class TagSource:
    """A start tag's verbatim source: ``source[start:start + len(text)] == text``."""

    start: int
    text: str
    explicitly_closed: bool = False
    """An ``<a>`` closed by a real ``</a>`` end tag (not implicitly by its parent or EOF)."""

    @property
    def end(self) -> int:
        return self.start + len(self.text)


class _RecordingSoup(BeautifulSoup):
    """A BeautifulSoup that records where its strings and tags came from. Tree identical to
    ``make_soup``: it only adds bookkeeping around the stock html.parser builder."""

    def __init__(self, markup: str) -> None:
        self.ken_source = markup
        self.ken_line_starts = [0, *(m.end() for m in re.finditer("\n", markup))]
        self.ken_pending: list[_RawPiece] = []
        self.ken_strings: dict[int, tuple[NavigableString, list[_RawPiece]]] = {}
        self.ken_tags: dict[int, tuple[Tag, TagSource]] = {}
        self.ken_next_tag: TagSource | None = None
        self.ken_explicit_end = False
        self.ken_consistent = True
        super().__init__(markup, builder=_PositionTreeBuilder())

    def endData(self, containerClass: type[NavigableString] | None = None) -> None:  # noqa: N802,N803
        pieces = self.ken_pending
        self.ken_pending = []
        creating = bool(self.current_data)
        super().endData(containerClass)
        if not pieces:
            return
        parent = self.currentTag
        last = parent.contents[-1] if creating and parent is not None and parent.contents else None
        if isinstance(last, NavigableString):
            self.ken_strings[id(last)] = (last, pieces)
        else:  # pragma: no cover - data recorded but no string created (parse_only unused)
            self.ken_consistent = False

    def handle_starttag(
        self,
        name: str,
        namespace: str | None,
        nsprefix: str | None,
        attrs: Any,
        sourceline: int | None = None,
        sourcepos: int | None = None,
        namespaces: dict[str, str] | None = None,
    ) -> Tag | None:
        pending, self.ken_next_tag = self.ken_next_tag, None
        tag = super().handle_starttag(
            name, namespace, nsprefix, attrs, sourceline, sourcepos, namespaces
        )
        if tag is not None and pending is not None:
            self.ken_tags[id(tag)] = (tag, pending)
        return tag

    def handle_endtag(self, name: str, nsprefix: str | None = None) -> None:
        explicit, self.ken_explicit_end = self.ken_explicit_end, False
        if explicit and name == "a" and self.open_tag_counter.get(name):
            # The tag _popToTag will close: the most recent open <a>.
            for tag in reversed(self.tagStack[1:]):
                if tag.name == name and tag.prefix == nsprefix:
                    entry = self.ken_tags.get(id(tag))
                    if entry is not None:
                        entry[1].explicitly_closed = True
                    break
        super().handle_endtag(name, nsprefix)


class _PositionParser(BeautifulSoupHTMLParser):
    def __init__(self, soup: BeautifulSoup, *args: Any, **kwargs: Any) -> None:
        super().__init__(soup, *args, **kwargs)
        self._ref_parts: list[str] | None = None

    @property
    def _rec(self) -> _RecordingSoup:
        return cast(_RecordingSoup, self.soup)

    def _offset(self) -> int:
        line, column = self.getpos()
        starts = self._rec.ken_line_starts
        if not 0 < line <= len(starts):  # pragma: no cover - defensive
            self._rec.ken_consistent = False
            return -1
        return starts[line - 1] + column

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]], handle_empty_element: bool = True
    ) -> None:
        self._rec.ken_next_tag = TagSource(
            start=self._offset(), text=self.get_starttag_text() or ""
        )
        super().handle_starttag(tag, attrs, handle_empty_element)

    def handle_endtag(self, tag: str, check_already_closed: bool = True) -> None:
        # bs4 passes check_already_closed=False for the synthetic end of a void element or
        # of "<x/>" (which a browser does NOT close for <a/>): only a real end tag is True.
        self._rec.ken_explicit_end = check_already_closed
        super().handle_endtag(tag, check_already_closed)

    def handle_data(self, data: str) -> None:
        if self._ref_parts is not None:
            self._ref_parts.append(data)
        else:
            start = self._offset()
            self._rec.ken_pending.append((start, start + len(data), data, True))
        super().handle_data(data)

    def handle_charref(self, name: str) -> None:
        start = self._offset()
        self._ref_parts = []
        try:
            super().handle_charref(name)
        finally:
            parts, self._ref_parts = self._ref_parts, None
        self._record_reference(start, "&#" + name, "".join(parts))

    def handle_entityref(self, name: str) -> None:
        start = self._offset()
        self._ref_parts = []
        try:
            super().handle_entityref(name)
        finally:
            parts, self._ref_parts = self._ref_parts, None
        self._record_reference(start, "&" + name, "".join(parts))

    def _record_reference(self, start: int, text: str, decoded: str) -> None:
        source = self._rec.ken_source
        end = start + len(text)
        if source.startswith(text, start):
            if source.startswith(";", end):
                end += 1
        else:  # pragma: no cover - defensive: html.parser reported a reference elsewhere
            self._rec.ken_consistent = False
        self._rec.ken_pending.append((start, end, decoded, False))


class _PositionTreeBuilder(HTMLParserTreeBuilder):
    def feed(
        self, markup: Any, _parser_class: type[BeautifulSoupHTMLParser] = _PositionParser
    ) -> None:
        super().feed(markup, _parser_class)


def _recorded_soup(source: str) -> tuple[_RecordingSoup, bool]:
    try:
        return _RecordingSoup(source), True
    except ParserRejectedMarkup:  # pragma: no cover - mirrors normalize._parse
        return _RecordingSoup(""), source == ""


# --------------------------------------------------------------------------- #
# Text-node sources
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SourcePiece:
    dec_start: int
    dec_end: int
    src_start: int
    src_end: int
    verbatim: bool


@dataclass(frozen=True, slots=True)
class TextSource:
    pieces: tuple[SourcePiece, ...]
    reliable: bool
    """The pieces decode to exactly the node's string and match the source."""
    contiguous: bool
    """No source gap between consecutive pieces."""

    @property
    def src_start(self) -> int:
        return self.pieces[0].src_start

    @property
    def src_end(self) -> int:
        return self.pieces[-1].src_end

    def to_source(self, offset: int, *, start: bool) -> int | None:
        """Source offset of a boundary at ``offset`` of the decoded string, or ``None`` when
        it falls inside a character/entity reference."""
        for piece in self.pieces:
            if piece.dec_start == piece.dec_end:
                continue
            if piece.dec_start < offset < piece.dec_end:
                if not piece.verbatim:
                    return None
                return piece.src_start + (offset - piece.dec_start)
            if start and offset == piece.dec_start:
                return piece.src_start
            if not start and offset == piece.dec_end:
                return piece.src_end
        return None


def _text_source(node: NavigableString, raw: list[_RawPiece], source: str) -> TextSource:
    pieces: list[SourcePiece] = []
    cursor = 0
    reliable = True
    for src_start, src_end, decoded, verbatim in raw:
        pieces.append(
            SourcePiece(
                dec_start=cursor,
                dec_end=cursor + len(decoded),
                src_start=src_start,
                src_end=src_end,
                verbatim=verbatim,
            )
        )
        cursor += len(decoded)
        in_bounds = 0 <= src_start <= src_end <= len(source)
        matches = (
            source[src_start:src_end] == decoded if verbatim else source.startswith("&", src_start)
        )
        reliable = reliable and in_bounds and matches
    if "".join(piece[2] for piece in raw) != str(node):
        reliable = False  # e.g. bs4 collapsed a whitespace-only string to " "
    contiguous = all(a.src_end == b.src_start for a, b in itertools.pairwise(pieces))
    return TextSource(pieces=tuple(pieces), reliable=reliable, contiguous=contiguous)


# --------------------------------------------------------------------------- #
# NFC boundary mapping
# --------------------------------------------------------------------------- #
def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _chunk_boundary(chunk: str, normal: str, offset: int) -> int | None:
    """The unique ``r`` with ``NFC(chunk[:r]) == normal[:offset]`` and
    ``NFC(chunk[r:]) == normal[offset:]``, or ``None``."""
    if len(chunk) > NFC_CHUNK_MAX:
        return None
    head, tail = normal[:offset], normal[offset:]
    found = [
        r for r in range(len(chunk) + 1) if _nfc(chunk[:r]) == head and _nfc(chunk[r:]) == tail
    ]
    return found[0] if len(found) == 1 else None


def nfc_boundary(raw: str, offset: int) -> int | None:
    """Map a boundary ``offset`` of ``NFC(raw)`` to a safe cut of ``raw``, or ``None``.

    ``raw`` is cut into ASCII stretches (NFC is the identity on them, and no character
    composes with or reorders across an ASCII character that FOLLOWS it) and chunks: one
    maximal non-ASCII run plus the ASCII character before it (its possible base character).
    """
    normal = _nfc(raw)
    if normal == raw:
        return offset if 0 <= offset <= len(raw) else None
    segments: list[tuple[int, int]] = []  # raw [start, end) segments, in order
    cursor = 0
    for match in _NON_ASCII_RE.finditer(raw):
        begin = max(match.start() - 1, cursor)
        if begin > cursor:
            segments.append((cursor, begin))
        segments.append((begin, match.end()))
        cursor = match.end()
    if cursor < len(raw):
        segments.append((cursor, len(raw)))
    pos = 0  # position in ``normal``
    for begin, end in segments:
        piece = raw[begin:end]
        normal_piece = _nfc(piece)
        if normal[pos : pos + len(normal_piece)] != normal_piece:
            return None  # pragma: no cover - NFC was not segment-local after all
        if offset == pos:
            return begin
        if pos < offset < pos + len(normal_piece):
            local = _chunk_boundary(piece, normal_piece, offset - pos)
            return None if local is None else begin + local
        pos += len(normal_piece)
    return len(raw) if offset == pos == len(normal) else None


# --------------------------------------------------------------------------- #
# Mapped blocks
# --------------------------------------------------------------------------- #
class LinkState(StrEnum):
    NONE = "none"
    """No ``<a>`` element encloses any part of the span."""
    EXACT = "exact"
    """One ``<a>`` wraps EXACTLY the span: its only child is one text node equal to it, its
    start tag ends where the text starts and a real ``</a>`` follows the text."""
    PARTIAL = "partial"
    """An ``<a>`` encloses part of the span, or the whole span plus other content."""


@dataclass(frozen=True, slots=True)
class SpanLocation:
    nodes: tuple[NavigableString, ...]
    """Text nodes covering the span, in order (empty when NFC is not part-local)."""
    parts: tuple[int, ...]
    """Indices of the block parts covering the span."""
    single: bool
    node: NavigableString | None = None
    dec_start: int | None = None
    dec_end: int | None = None
    src_start: int | None = None
    src_end: int | None = None
    problem: str | None = None


@dataclass(slots=True)
class MappedBlock:
    index: int
    block: ContentBlock
    context_hash: str
    parts: tuple[str, ...]
    labels: tuple[frozenset[str] | None, ...]
    nodes: tuple[NavigableString | None, ...]
    nfc_parts: tuple[str, ...] = ()
    nfc_starts: tuple[int, ...] = ()
    part_local: bool = False
    x_to_n: tuple[int, ...] = ()
    n_to_x: tuple[int, ...] = ()

    def _part_at(self, n: int) -> int:
        return bisect.bisect_right(self.nfc_starts, n) - 1

    def locate(self, start: int, end: int, doc: MappedDocument) -> SpanLocation:
        text = self.block.text
        if not 0 <= start < end <= len(text) or text[start].isspace() or text[end - 1].isspace():
            return SpanLocation(nodes=(), parts=(), single=False, problem="range")
        if not self.part_local:
            return SpanLocation(nodes=(), parts=(), single=False, problem="nfc_not_local")
        n_start, n_end = self.x_to_n[start], self.x_to_n[end - 1] + 1
        k_start, k_end = self._part_at(n_start), self._part_at(n_end - 1)
        parts = tuple(range(k_start, k_end + 1))
        nodes = tuple(node for k in parts if (node := self.nodes[k]) is not None)
        single = k_start == k_end and self.nodes[k_start] is not None
        if not single:
            return SpanLocation(nodes=nodes, parts=parts, single=False)
        node = self.nodes[k_start]
        raw = self.parts[k_start]
        base = self.nfc_starts[k_start]
        dec_start = nfc_boundary(raw, n_start - base)
        dec_end = nfc_boundary(raw, n_end - base)
        if node is None or dec_start is None or dec_end is None or dec_start >= dec_end:
            return SpanLocation(nodes=nodes, parts=parts, single=True, node=node, problem="nfc")
        source = doc.text_sources.get(id(node))
        src_start = src_end = None
        problem: str | None = None
        if source is None or not source.reliable:
            problem = "unrecorded"
        else:
            src_start = source.to_source(dec_start, start=True)
            src_end = source.to_source(dec_end, start=False)
            if src_start is None or src_end is None:
                problem = "inside_reference"
        return SpanLocation(
            nodes=nodes,
            parts=parts,
            single=True,
            node=node,
            dec_start=dec_start,
            dec_end=dec_end,
            src_start=src_start,
            src_end=src_end,
            problem=problem,
        )

    def x_range(self, node_ids: set[int]) -> tuple[int, int] | None:
        """``[start, end)`` in the block text of the visible characters of these nodes."""
        if not self.part_local:
            return None
        xs: list[int] = []
        for k, node in enumerate(self.nodes):
            if node is None or id(node) not in node_ids:
                continue
            base = self.nfc_starts[k]
            for n in range(base, base + len(self.nfc_parts[k])):
                if self.n_to_x[n] >= 0:
                    xs.append(self.n_to_x[n])
        return (min(xs), max(xs) + 1) if xs else None


def _finish_block(mapped: MappedBlock) -> bool:
    """Fill the NFC and collapse maps; ``False`` if they do not reproduce the block text."""
    raw = "".join(mapped.parts)
    normal = _nfc(raw)
    nfc_parts = tuple(_nfc(part) for part in mapped.parts)
    starts: list[int] = []
    cursor = 0
    for part in nfc_parts:
        starts.append(cursor)
        cursor += len(part)
    mapped.nfc_parts = nfc_parts
    mapped.nfc_starts = tuple(starts)
    mapped.part_local = "".join(nfc_parts) == normal
    x_to_n: list[int] = []
    n_to_x = [-1] * len(normal)
    pending: int | None = None
    for n, char in enumerate(normal):
        if char.isspace():
            if x_to_n and pending is None:
                pending = n
            continue
        if pending is not None:
            x_to_n.append(pending)
            pending = None
        n_to_x[n] = len(x_to_n)
        x_to_n.append(n)
    mapped.x_to_n = tuple(x_to_n)
    mapped.n_to_x = tuple(n_to_x)
    rebuilt = "".join(" " if normal[n].isspace() else normal[n] for n in x_to_n)
    return rebuilt == mapped.block.text


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class LinkElement:
    """An ``<a>`` element of the document as parsed (before boilerplate removal)."""

    tag: Tag
    order: int
    href: str | None
    link_id: str | None
    text: str
    """Collapsed text, as the normaliser computes anchor text."""
    hidden: bool
    """The element or an ancestor is hidden (attribute, aria-hidden, inline style)."""
    nested: bool
    """Inside another ``<a>``, or containing one."""
    in_root: bool
    source: TagSource | None


@dataclass(slots=True)
class MappedDocument:
    source: str
    sound: bool
    """The walk replay equals ``extract_blocks``, every recorded position is consistent and no
    ``<script>`` may double-escape (:func:`script_double_escapes`)."""
    root: Tag
    blocks: tuple[MappedBlock, ...]
    text_sources: dict[int, TextSource]
    tag_sources: dict[int, TagSource]
    links: tuple[LinkElement, ...]
    text_hidden: dict[int, bool]
    text_raw: dict[int, bool]
    text_link: dict[int, Tag | None]
    """Text node id -> nearest enclosing ``<a>`` (anywhere up to the document)."""
    node_block: dict[int, int]
    """Text node id -> index of its block."""
    unclosed_link_starts: tuple[int, ...]
    """Source offsets of ``<a>`` start tags never closed by a real ``</a>``."""
    _keep: list[PageElement] = field(default_factory=list, repr=False)

    def blocks_with_hash(self, context_hash: str) -> list[MappedBlock]:
        return [block for block in self.blocks if block.context_hash == context_hash]

    def links_with_id(self, link_id: str) -> list[LinkElement]:
        return [link for link in self.links if link.link_id == link_id]

    def link_for_tag(self, tag: Tag) -> LinkElement | None:
        return next((link for link in self.links if link.tag is tag), None)

    def unclosed_link_before(self, position: int, *, ignore: Tag | None = None) -> bool:
        ignored = None
        if ignore is not None and (source := self.tag_sources.get(id(ignore))) is not None:
            ignored = source.start
        return any(start < position and start != ignored for start in self.unclosed_link_starts)

    def link_at(self, location: SpanLocation) -> tuple[LinkState, Tag | None]:
        """Which ``<a>`` (if any) encloses the located span."""
        enclosing = [self.text_link.get(id(node)) for node in location.nodes]
        first = next((tag for tag in enclosing if tag is not None), None)
        if first is None:
            return LinkState.NONE, None
        node = location.node
        if (
            location.single
            and node is not None
            and location.dec_start == 0
            and location.dec_end == len(str(node))
            and self.is_exact_link(first, node)
        ):
            return LinkState.EXACT, first
        return LinkState.PARTIAL, first

    def is_exact_link(self, tag: Tag, node: NavigableString) -> bool:
        """``tag`` is ``<a ...>`` + exactly the source of ``node`` + a real ``</a>``."""
        if node.parent is not tag or len(tag.contents) != 1:
            return False
        text = self.text_sources.get(id(node))
        start_tag = self.tag_sources.get(id(tag))
        if text is None or not text.reliable or not text.contiguous or start_tag is None:
            return False
        if start_tag.end != text.src_start or not start_tag.explicitly_closed:
            return False
        return _END_A_RE.match(self.source, text.src_end) is not None

    def unwrap_ranges(self, tag: Tag) -> tuple[tuple[int, int], tuple[int, int]] | None:
        """Source ranges of ``tag``'s start and end tags, when it is an exact link."""
        if len(tag.contents) != 1 or not isinstance(tag.contents[0], NavigableString):
            return None
        node = tag.contents[0]
        if not self.is_exact_link(tag, node):
            return None
        start_tag = self.tag_sources[id(tag)]
        text = self.text_sources[id(node)]
        match = _END_A_RE.match(self.source, text.src_end)
        if match is None:  # pragma: no cover - is_exact_link checked it
            return None
        return (start_tag.start, start_tag.end), (match.start(), match.end())

    def link_span(self, tag: Tag) -> tuple[MappedBlock, tuple[int, int] | None] | None:
        """The block holding ``tag``'s text and the text's ``[start, end)`` in it."""
        if tag.decomposed:
            return None
        ids: set[int] = set()
        for count, node in enumerate(tag.descendants):
            if count >= ANCHOR_SCAN_MAX_NODES:
                break
            if isinstance(node, NavigableString) and id(node) in self.node_block:
                ids.add(id(node))
        if not ids:
            return None
        indices = {self.node_block[i] for i in ids}
        if len(indices) != 1:
            return None
        block = self.blocks[indices.pop()]
        return block, block.x_range(ids)

    def a_href_signature(self) -> list[tuple[str, str | None]]:
        """``(href, link id)`` of every ``<a href>`` in the document, in document order."""
        return [(link.href, link.link_id) for link in self.links if link.href is not None]


@dataclass(slots=True)
class _WalkBlock:
    kind: str
    enclosing: frozenset[str] = frozenset()
    parts: list[str] = field(default_factory=list)
    labels: list[frozenset[str] | None] = field(default_factory=list)
    nodes: list[NavigableString | None] = field(default_factory=list)
    has_anchor: bool = False
    first: int | None = None


def _walk(root: Tag) -> list[_WalkBlock]:
    """``normalize._walk``, also recording each part's text node. Same blocks, same order."""
    root_kind = DOCUMENT_BLOCK_KIND if isinstance(root, BeautifulSoup) else root.name
    root_block = _WalkBlock(kind=root_kind)
    blocks = [root_block]
    empty: frozenset[str] = frozenset()
    context: dict[int, tuple[_WalkBlock, bool, frozenset[str], frozenset[str]]] = {
        id(root): (root_block, root_kind in BLOCK_TAGS, empty, empty)
    }
    for position, node in enumerate(root.descendants):
        parent = node.parent
        if parent is None:  # pragma: no cover - descendants always have parents
            continue
        owner, in_block, inner, path = context[id(parent)]
        if isinstance(node, Tag):
            name = node.name
            if name in _BREAKING_TAGS or name in BLOCK_TAGS or name in CONTAINER_TAGS:
                owner.parts.append(" ")
                owner.labels.append(None)
                owner.nodes.append(None)
            if name in BLOCK_TAGS or (name in CONTAINER_TAGS and not in_block):
                owner = _WalkBlock(kind=name, enclosing=path)
                blocks.append(owner)
                in_block = in_block or name in BLOCK_TAGS
                inner = empty
            else:
                inner = inner | {name}
            context[id(node)] = (owner, in_block, inner, path | {name})
            if name == "a" and node.has_attr("href"):
                owner.has_anchor = True
                if owner.first is None:
                    owner.first = position
        elif _is_text(node):
            text = str(node)
            owner.parts.append(text)
            owner.labels.append(inner)
            owner.nodes.append(cast(NavigableString, node))
            if owner.first is None and text.strip():
                owner.first = position
    ordered = sorted(
        (block for block in blocks if block.first is not None),
        key=lambda block: block.first if block.first is not None else -1,
    )
    return [b for b in ordered if _collapse("".join(b.parts)) or b.has_anchor]


def _same_blocks(mine: tuple[MappedBlock, ...], theirs: tuple[ContentBlock, ...]) -> bool:
    if len(mine) != len(theirs):
        return False
    for mapped, block in zip(mine, theirs, strict=True):
        ours = mapped.block
        if (ours.block_id, ours.kind, ours.text, ours.runs, ours.enclosing) != (
            block.block_id,
            block.kind,
            block.text,
            block.runs,
            block.enclosing,
        ):
            return False
    return True


def map_document(html: str) -> MappedDocument:
    """Parse ``html`` with source positions and replay the inventory block walk. Pure; never
    raises on malformed markup (a parser failure gives an unsound, empty document)."""
    source = _scrub(html)
    soup, parsed = _recorded_soup(source)
    keep: list[PageElement] = list(soup.descendants)  # keeps ids unique while we key by them

    text_sources = {
        key: _text_source(node, raw, source) for key, (node, raw) in soup.ken_strings.items()
    }
    tag_sources = {key: entry for key, (_tag, entry) in soup.ken_tags.items()}
    root = _select_root(soup)

    # Ancestor facts over the WHOLE document, before boilerplate removal, in one pass.
    hidden: dict[int, bool] = {id(soup): False}
    raw_text: dict[int, bool] = {id(soup): False}
    nearest: dict[int, Tag | None] = {id(soup): None}
    has_link_child: set[int] = set()
    text_hidden: dict[int, bool] = {}
    text_raw: dict[int, bool] = {}
    text_link: dict[int, Tag | None] = {}
    a_tags: list[Tag] = []
    script_differential = False
    for node in keep:
        parent = node.parent
        if parent is None:  # pragma: no cover
            continue
        p_hidden = hidden.get(id(parent), False)
        p_raw = raw_text.get(id(parent), False)
        p_link = nearest.get(id(parent))
        if isinstance(node, Tag):
            hidden[id(node)] = p_hidden or _self_hidden(node)
            raw_text[id(node)] = p_raw or node.name in RAW_TEXT_TAGS
            if node.name == "script" and not script_differential:
                # Read before _strip_boilerplate decomposes scripts.
                script_differential = script_double_escapes(
                    "".join(str(child) for child in node.contents)
                )
            if node.name == "a":
                a_tags.append(node)
                if p_link is not None:
                    has_link_child.add(id(p_link))
                nearest[id(node)] = node
            else:
                nearest[id(node)] = p_link
        elif isinstance(node, NavigableString):
            text_hidden[id(node)] = p_hidden
            text_raw[id(node)] = p_raw
            text_link[id(node)] = p_link

    root_ids = {id(root)} | {id(node) for node in root.descendants if isinstance(node, Tag)}
    links = tuple(
        LinkElement(
            tag=tag,
            order=order,
            href=None if (href := _attr(tag, "href")) is None else href.strip(),
            link_id=None if (lid := _attr(tag, LINK_ID_ATTR)) is None else lid.strip().lower(),
            text=_text_of(tag, max_nodes=ANCHOR_SCAN_MAX_NODES),
            hidden=hidden.get(id(tag), False),
            nested=nearest.get(id(tag.parent)) is not None or id(tag) in has_link_child,
            in_root=id(tag) in root_ids,
            source=tag_sources.get(id(tag)),
        )
        for order, tag in enumerate(a_tags)
    )
    unclosed = tuple(
        sorted(
            entry.start
            for tag in a_tags
            if (entry := tag_sources.get(id(tag))) is not None and not entry.explicitly_closed
        )
    )

    _strip_boilerplate(root)
    walked = _walk(root)
    blocks: list[MappedBlock] = []
    node_block: dict[int, int] = {}
    sound = parsed and soup.ken_consistent and not script_differential
    for index, walk_block in enumerate(walked):
        text = _collapse("".join(walk_block.parts))
        content = ContentBlock(
            block_id=f"b{index}",
            kind=walk_block.kind,
            text=text,
            runs=_text_runs(walk_block.parts, walk_block.labels, text),
            enclosing=walk_block.enclosing,
        )
        mapped = MappedBlock(
            index=index,
            block=content,
            context_hash=sha256_hex(text),
            parts=tuple(walk_block.parts),
            labels=tuple(walk_block.labels),
            nodes=tuple(walk_block.nodes),
        )
        sound = _finish_block(mapped) and sound
        for text_node in walk_block.nodes:
            if text_node is not None:
                node_block[id(text_node)] = index
        blocks.append(mapped)
    mapped_blocks = tuple(blocks)
    sound = sound and _same_blocks(mapped_blocks, extract_blocks(html))
    return MappedDocument(
        source=source,
        sound=sound,
        root=root,
        blocks=mapped_blocks,
        text_sources=text_sources,
        tag_sources=tag_sources,
        links=links,
        text_hidden=text_hidden,
        text_raw=text_raw,
        text_link=text_link,
        node_block=node_block,
        unclosed_link_starts=unclosed,
        _keep=keep,
    )
