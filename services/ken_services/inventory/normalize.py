"""``SourceDocument -> NormalizedPage``: deterministic, LLM-free normalisation.

ADR-0009 section 2 / TRD.md section 11. :func:`normalize_document` is a pure
function: the same document and scope always give an identical
:class:`~ken_contracts.inventory.NormalizedPage` (and therefore the same
content hash). It never raises on malformed HTML; the only error is a document
whose own URLs cannot be normalised (:class:`NormalizationError`).

Pipeline, in order:

1. Parse once with :func:`ken_services.inventory.html.make_soup` and read the
   canonical, robots and ``<base>`` facts through ``discover_soup`` -- the same
   code the crawler uses, so a page is never crawled under one canonical and
   stored under another.
2. Document-level facts (title, description, language, dates, metadata) are
   read from the whole document, with source assertions taking precedence.
3. The content root (``main`` > ``article`` > ``[role=main]`` > ``body`` >
   document) is selected and structurally unambiguous boilerplate removed.
   There are no heuristics beyond the fixed list in :data:`_REMOVED_TAGS` /
   :data:`_REMOVED_ROLES` and the ``header``/``footer`` rule.
4. The root is walked once, iteratively (no recursion limit on deep markup),
   assigning every text node and every ``<a href>`` to its block: the nearest
   enclosing block tag, else the nearest enclosing container (implicit block).
5. Block ids, headings and links are derived from the walk; the content hash
   is computed last (:mod:`ken_services.inventory.hashing`).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag
from bs4.element import (
    CData,
    Comment,
    Declaration,
    Doctype,
    NavigableString,
    PageElement,
    PreformattedString,
    ProcessingInstruction,
    Script,
    Stylesheet,
    TemplateString,
)
from bs4.exceptions import ParserRejectedMarkup

from ken_contracts.inventory import (
    CONTENT_HASH_VERSION,
    AnchorSource,
    CanonicalStatus,
    ExtractedLink,
    Heading,
    LinkType,
    NormalizedPage,
    PublicationState,
    SiteScope,
    SourceAssertions,
    SourceDocument,
    SourceType,
)
from ken_services.inventory.hashing import content_hash, sha256_hex
from ken_services.inventory.html import discover_soup, make_soup
from ken_services.inventory.urls import InvalidUrlError, in_scope, normalize_url, try_normalize_url

__all__ = ["NormalizationError", "normalize_document"]

BLOCK_TAGS: Final = frozenset(
    {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "blockquote",
        "pre",
        "td",
        "th",
        "dd",
        "dt",
        "figcaption",
        "caption",
    }
)
"""Elements whose text forms one block. Nested blocks: the innermost wins."""

CONTAINER_TAGS: Final = frozenset({"div", "section", "article", "main", "body"})
"""Text directly inside these (and outside any block) forms an implicit block."""

HEADING_LEVELS: Final = {f"h{level}": level for level in range(1, 7)}

_REMOVED_TAGS: Final = frozenset(
    {
        # Non-content and interactive elements.
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "iframe",
        "object",
        "embed",
        "form",
        "button",
        "select",
        "input",
        "textarea",
        # Structural boilerplate.
        "nav",
        "aside",
        # Document metadata: only ever inside the root when the document has no
        # <body> (html.parser does not invent one), and never body content.
        "head",
        "title",
        "meta",
        "link",
        "base",
    }
)
_REMOVED_ROLES: Final = frozenset(
    {"navigation", "banner", "contentinfo", "complementary", "search"}
)
_REMOVED_STRING_TYPES: Final = (Comment, Doctype, Declaration, ProcessingInstruction)

_LANGUAGE_RE: Final = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$")
_METADATA_WHITELIST: Final = (
    "og:title",
    "og:description",
    "og:type",
    "og:url",
    "og:locale",
    "article:published_time",
    "article:modified_time",
)
METADATA_VALUE_MAX: Final = 1000
CONTEXT_TEXT_MAX: Final = 1000
DOCUMENT_BLOCK_KIND: Final = "document"
"""``block_kind`` of the implicit block of a document with no ``<body>``."""


class NormalizationError(ValueError):
    """The document cannot be normalised: its own URL is not a valid http(s) URL.

    The message never contains the URL (it may be long or carry credentials).
    """


# --------------------------------------------------------------------------- #
# Small text and attribute helpers
# --------------------------------------------------------------------------- #
def _collapse(text: str) -> str:
    """NFC-normalise and collapse every run of whitespace to one space."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def _clean(text: str | None) -> str | None:
    """:func:`_collapse`, with ``None`` for missing or blank values."""
    if text is None:
        return None
    return _collapse(text) or None


def _attr(tag: Tag, name: str) -> str | None:
    value = tag.get(name)
    if value is None:
        return None
    if isinstance(value, list):  # multi-valued attributes (class, rel)
        return " ".join(str(part) for part in value)
    return str(value)


def _is_text(node: PageElement) -> bool:
    """Visible character data: what ``get_text`` would return, nothing else."""
    if isinstance(node, CData):
        return True
    return isinstance(node, NavigableString) and not isinstance(
        node, PreformattedString | Script | Stylesheet | TemplateString
    )


# Elements that separate words when rendered, although they are not blocks.
_BREAKING_TAGS: Final = frozenset({"br", "hr", "img", "wbr"})

# Bounds that keep normalisation linear on hostile markup (Phase 1 QA M1):
# nested <a> tags are not auto-closed by html.parser, so an unbounded per-anchor
# descendant scan is O(n^2), and one page could stall a whole run.
MAX_LINKS_PER_PAGE: Final = 10_000
"""Body links kept per page, in document order; the rest are dropped and flagged."""
ANCHOR_SCAN_MAX_NODES: Final = 256
"""Descendant nodes read per anchor when computing its text."""
ANCHOR_TEXT_MAX: Final = 1_000
"""Anchor text longer than this is truncated."""


def _scrub(value: Any) -> Any:
    """Replace NUL with U+FFFD, as browsers do (Phase 1 QA M2).

    PostgreSQL text cannot store NUL: one stray NUL byte would otherwise make a
    page fail to persist on every run, i.e. never be inventoried.
    """
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, dict):
        return {_scrub(key): _scrub(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_scrub(item) for item in value)
    return value


def _text_of(tag: Tag, *, max_nodes: int | None = None) -> str:
    """Collapsed text of ``tag`` as a browser renders it, NFC-normalised.

    Inline elements CONCATENATE (``Hel<b>lo</b>`` is "Hello", not "Hel lo"):
    anchors and body text must quote the page exactly, because later phases
    match anchor text against it and must never fabricate source text (PRD
    section 7.7). Line breaks and block-level children separate words.
    """
    parts: list[str] = []
    for count, node in enumerate(tag.descendants):
        if max_nodes is not None and count >= max_nodes:
            break
        if isinstance(node, Tag):
            if (
                node.name in _BREAKING_TAGS
                or node.name in BLOCK_TAGS
                or node.name in CONTAINER_TAGS
            ):
                parts.append(" ")
        elif _is_text(node):
            parts.append(str(node))
    return _collapse("".join(parts))


def _parse(html: str) -> BeautifulSoup:
    try:
        return make_soup(html)
    except ParserRejectedMarkup:
        # html.parser accepts essentially everything; this keeps the "never
        # raises on malformed HTML" guarantee if a future version does not.
        return make_soup("")


# --------------------------------------------------------------------------- #
# Document-level facts
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _HeadFacts:
    meta: dict[str, str]
    """First ``content`` per lower-cased ``name`` / ``property``."""
    http_equiv_language: str | None
    title_tag: str | None
    html_lang: str | None
    first_h1: str | None


def _head_facts(soup: BeautifulSoup) -> _HeadFacts:
    meta: dict[str, str] = {}
    http_equiv_language: str | None = None
    for tag in soup.find_all("meta"):
        if not isinstance(tag, Tag):
            continue
        content = _attr(tag, "content")
        if content is None:
            continue
        for key_attr in ("name", "property"):
            key = (_attr(tag, key_attr) or "").strip().lower()
            if key and key not in meta:
                meta[key] = content
        equiv = (_attr(tag, "http-equiv") or "").strip().lower()
        if equiv == "content-language" and http_equiv_language is None:
            http_equiv_language = content

    title_tag: str | None = None
    for tag in soup.find_all("title"):
        # An <svg><title> is an image caption, not the document title.
        if isinstance(tag, Tag) and tag.find_parent("svg") is None:
            title_tag = _clean(_text_of(tag))
            if title_tag:
                break

    html_lang: str | None = None
    html_tag = soup.find("html")
    if isinstance(html_tag, Tag):
        html_lang = _attr(html_tag, "lang")

    first_h1: str | None = None
    for tag in soup.find_all("h1"):
        if isinstance(tag, Tag):
            first_h1 = _clean(_text_of(tag))
            if first_h1:
                break

    return _HeadFacts(
        meta=meta,
        http_equiv_language=http_equiv_language,
        title_tag=title_tag,
        html_lang=html_lang,
        first_h1=first_h1,
    )


def _first_language_tag(value: str | None) -> str | None:
    """First tag of a comma-separated list (``Content-Language: en-GB, fr``)."""
    if value is None:
        return None
    return value.split(",", 1)[0]


def _valid_language(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if candidate and _LANGUAGE_RE.match(candidate):
        return candidate.lower()
    return None


def _parse_aware_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _metadata_value(value: str) -> str:
    return _collapse(value)[:METADATA_VALUE_MAX]


# --------------------------------------------------------------------------- #
# Content root and boilerplate removal
# --------------------------------------------------------------------------- #
def _has_role(tag: Tag, roles: frozenset[str]) -> bool:
    return (_attr(tag, "role") or "").strip().lower() in roles


def _select_root(soup: BeautifulSoup) -> Tag:
    for name in ("main", "article"):
        found = soup.find(name)
        if isinstance(found, Tag):
            return found
    found = soup.find(lambda tag: _has_role(tag, frozenset({"main"})))
    if isinstance(found, Tag):
        return found
    body = soup.find("body")
    if isinstance(body, Tag):
        return body
    return soup


def _is_boilerplate(tag: Tag) -> bool:
    name = tag.name
    if name in _REMOVED_TAGS or _has_role(tag, _REMOVED_ROLES):
        return True
    if (_attr(tag, "aria-hidden") or "").strip().lower() == "true" or tag.has_attr("hidden"):
        return True
    return name in {"header", "footer"} and tag.find_parent(["main", "article"]) is None


def _strip_boilerplate(root: Tag) -> None:
    """Remove boilerplate and comments inside ``root`` (never ``root`` itself)."""
    doomed = [tag for tag in root.find_all(True) if _is_boilerplate(tag)]
    for tag in doomed:
        if not tag.decomposed:  # already gone with a removed ancestor
            tag.decompose()
    strings = [node for node in root.descendants if isinstance(node, _REMOVED_STRING_TYPES)]
    for node in strings:
        node.extract()


# --------------------------------------------------------------------------- #
# Block walk
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _Block:
    kind: str
    parts: list[str] = field(default_factory=list)
    has_anchor: bool = False
    first: int | None = None
    """Walk position of the block's first non-blank text or first link."""
    text: str = ""
    block_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Walk:
    blocks: tuple[_Block, ...]
    """Non-empty or link-bearing blocks, ordered by where their content starts."""
    anchors: tuple[tuple[Tag, _Block], ...]
    """Every ``<a href>`` with its block, in document order."""


def _walk(root: Tag) -> _Walk:
    """Assign text and links to blocks in one iterative pre-order pass.

    A block is ordered by the position of its first non-blank text or link, not
    by its start tag, so ``<div><p>A</p>B</div>`` reads "A" then "B" and
    whitespace-only formatting never reorders blocks.
    """
    root_kind = DOCUMENT_BLOCK_KIND if isinstance(root, BeautifulSoup) else root.name
    root_block = _Block(kind=root_kind)
    blocks: list[_Block] = [root_block]
    anchors: list[tuple[Tag, _Block]] = []
    # id(element) -> (owning block, whether a real block tag encloses it)
    context: dict[int, tuple[_Block, bool]] = {id(root): (root_block, root_kind in BLOCK_TAGS)}

    for position, node in enumerate(root.descendants):  # pre-order: parents come first
        parent = node.parent
        if parent is None:
            continue
        owner, in_block = context[id(parent)]
        if isinstance(node, Tag):
            name = node.name
            if name in _BREAKING_TAGS or name in BLOCK_TAGS or name in CONTAINER_TAGS:
                # Rendering breaks here: keep the text runs on either side apart
                # ("a<div>b</div>" -> "a b", "Intro<p>X</p>Outro" -> "Intro Outro").
                owner.parts.append(" ")
            if name in BLOCK_TAGS or (name in CONTAINER_TAGS and not in_block):
                owner = _Block(kind=name)
                blocks.append(owner)
                in_block = in_block or name in BLOCK_TAGS
            context[id(node)] = (owner, in_block)
            if name == "a" and node.has_attr("href"):
                owner.has_anchor = True
                if owner.first is None:
                    owner.first = position
                anchors.append((node, owner))
        elif _is_text(node):
            text = str(node)
            owner.parts.append(text)
            if owner.first is None and text.strip():
                owner.first = position

    kept: list[_Block] = []
    for block in sorted(
        (block for block in blocks if block.first is not None),
        key=lambda block: block.first if block.first is not None else -1,
    ):
        # Inline runs concatenate; separators were inserted above where rendering breaks.
        block.text = _collapse("".join(block.parts))
        if block.text or block.has_anchor:
            block.block_id = f"b{len(kept)}"
            kept.append(block)
    return _Walk(blocks=tuple(kept), anchors=tuple(anchors))


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #
def _classify(href: str, *, base: str, scope: SiteScope) -> tuple[LinkType, str | None]:
    if not href:
        return LinkType.INVALID, None
    if href.startswith("#"):
        return LinkType.FRAGMENT, None
    try:
        scheme = _scheme_of(href)
    except ValueError:
        return LinkType.INVALID, None
    if scheme and scheme not in {"http", "https"}:
        return LinkType.NON_HTTP, None
    resolved = try_normalize_url(href, base=base)
    if resolved is None:
        return LinkType.INVALID, None
    if in_scope(resolved, scope):
        return LinkType.INTERNAL, resolved
    return LinkType.EXTERNAL, resolved


def _scheme_of(href: str) -> str:
    """Lower-cased scheme of ``href`` as ``urlsplit`` reads it ('' if none).

    ``urlsplit`` strips ASCII tab/newline first, so ``java\\tscript:`` is still
    ``javascript``. Raises ``ValueError`` on unparseable input.
    """
    return urlsplit(href).scheme.lower()


def _anchor_text(anchor: Tag) -> tuple[str, AnchorSource]:
    text = _text_of(anchor, max_nodes=ANCHOR_SCAN_MAX_NODES)[:ANCHOR_TEXT_MAX]
    if text:
        return text, "text"
    for img in anchor.find_all("img"):
        if isinstance(img, Tag):
            alt = _collapse(_attr(img, "alt") or "")
            if alt:
                return alt, "img_alt"
    aria = _collapse(_attr(anchor, "aria-label") or "")
    if aria:
        return aria, "aria_label"
    title = _collapse(_attr(anchor, "title") or "")
    if title:
        return title, "title"
    return "", "none"


def _rel_tokens(anchor: Tag) -> tuple[str, ...]:
    raw = _attr(anchor, "rel") or ""
    return tuple(sorted({token.lower() for token in raw.split() if token}))


def _extract_links(walk: _Walk, *, base: str, scope: SiteScope) -> tuple[ExtractedLink, ...]:
    links: list[ExtractedLink] = []
    per_block: dict[int, int] = {}
    block_hashes: dict[int, str] = {}  # one sha256 per block, not one per link
    for ordinal, (anchor, block) in enumerate(walk.anchors[:MAX_LINKS_PER_PAGE]):
        href = (_attr(anchor, "href") or "").strip()
        link_type, resolved = _classify(href, base=base, scope=scope)
        anchor_text, anchor_source = _anchor_text(anchor)
        occurrence = per_block.get(id(block), 0)
        per_block[id(block)] = occurrence + 1
        context_hash = block_hashes.get(id(block))
        if context_hash is None:
            context_hash = block_hashes[id(block)] = sha256_hex(block.text)
        block_id = block.block_id
        if block_id is None:  # pragma: no cover - a link-bearing block always has an id
            raise AssertionError("link-bearing block without an id")
        links.append(
            ExtractedLink(
                ordinal=ordinal,
                href=href,
                resolved_url=resolved,
                link_type=link_type,
                anchor_text=anchor_text,
                anchor_source=anchor_source,
                rel=_rel_tokens(anchor),
                block_id=block_id,
                block_kind=block.kind,
                occurrence_index=occurrence,
                context_text=block.text[:CONTEXT_TEXT_MAX],
                context_hash=context_hash,
            )
        )
    return tuple(links)


# --------------------------------------------------------------------------- #
# Canonical
# --------------------------------------------------------------------------- #
def _canonical(
    candidate: str | None, *, final_url: str, base: str, scope: SiteScope
) -> tuple[str, str | None, CanonicalStatus]:
    """``(identity url, declared canonical, status)``."""
    if candidate is None or not candidate.strip():
        return final_url, None, CanonicalStatus.MISSING
    declared = try_normalize_url(candidate, base=base)
    if declared is None:
        return final_url, None, CanonicalStatus.INVALID
    if not in_scope(declared, scope):
        return final_url, declared, CanonicalStatus.OUT_OF_SCOPE
    if declared == final_url:
        return final_url, declared, CanonicalStatus.SELF
    return declared, declared, CanonicalStatus.DECLARED


def _normalise_own_url(value: str, field_name: str) -> str:
    try:
        return normalize_url(value)
    except InvalidUrlError:
        raise NormalizationError(f"document {field_name} is not a valid http(s) URL") from None


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def normalize_document(doc: SourceDocument, *, scope: SiteScope) -> NormalizedPage:
    """Normalise one source document. Pure and deterministic.

    Raises:
        NormalizationError: ``doc.final_url`` or ``doc.requested_url`` is not a
            valid http(s) URL. Malformed HTML never raises.
    """
    final_url = _normalise_own_url(doc.final_url, "final_url")
    requested_url = _normalise_own_url(doc.requested_url, "requested_url")
    redirect_chain = tuple(
        url for url in (try_normalize_url(hop) for hop in doc.redirect_chain) if url is not None
    )
    assertions = SourceAssertions.model_validate(_scrub(doc.assertions.model_dump()))
    source_id = _scrub(doc.source_id)
    content_language = _scrub(doc.content_language)

    soup = _parse(_scrub(doc.html))
    discovery = discover_soup(soup)
    head = _head_facts(soup)

    # Base URL: an in-scope <base href> wins; an off-site one is ignored.
    base = final_url
    if discovery.base_href is not None:
        declared_base = try_normalize_url(discovery.base_href, base=final_url)
        if declared_base is not None and in_scope(declared_base, scope):
            base = declared_base

    canonical_candidate = assertions.canonical_url
    if canonical_candidate is None or not canonical_candidate.strip():
        canonical_candidate = discovery.canonical_href
    canonical_url, declared_canonical, canonical_status = _canonical(
        canonical_candidate, final_url=final_url, base=base, scope=scope
    )

    # Body: select, strip, walk.
    root = _select_root(soup)
    _strip_boilerplate(root)
    walk = _walk(root)
    body_html = root.decode()
    body_text = "\n\n".join(block.text for block in walk.blocks if block.text)
    headings = tuple(
        Heading(level=HEADING_LEVELS[block.kind], text=block.text, block_id=block.block_id)
        for block in walk.blocks
        if block.kind in HEADING_LEVELS and block.text and block.block_id is not None
    )
    links = _extract_links(walk, base=base, scope=scope)

    # h1: the content's own first h1, else the document's (e.g. in a header).
    body_h1 = next((heading.text for heading in headings if heading.level == 1), None)
    first_h1 = body_h1 or head.first_h1

    # Title: assertion > <title> > og:title > first h1.
    title: str | None = None
    title_source: str | None = None
    for source, value in (
        ("assertion", assertions.title),
        ("title", head.title_tag),
        ("og:title", head.meta.get("og:title")),
        ("h1", first_h1),
    ):
        cleaned = _clean(value)
        if cleaned:
            title, title_source = cleaned, source
            break

    meta_description = (
        _clean(assertions.meta_description)
        or _clean(head.meta.get("description"))
        or _clean(head.meta.get("og:description"))
    )

    language: str | None = None
    for candidate in (
        assertions.language,
        head.html_lang,
        _first_language_tag(content_language),
        _first_language_tag(head.http_equiv_language),
    ):
        language = _valid_language(candidate)
        if language is not None:
            break

    if assertions.publication_state is not None:
        publication_state = assertions.publication_state
    elif doc.source_type is SourceType.PUBLIC_WEB and doc.http_status == 200:
        publication_state = PublicationState.PUBLISHED
    else:
        publication_state = PublicationState.UNKNOWN

    published_at = assertions.published_at or _parse_aware_datetime(
        head.meta.get("article:published_time")
    )
    modified_at = assertions.modified_at or _parse_aware_datetime(
        head.meta.get("article:modified_time")
    )

    metadata: dict[str, str] = {}
    for key in _METADATA_WHITELIST:
        value = head.meta.get(key)
        if value is not None and _collapse(value):
            metadata[key] = _metadata_value(value)
    if title_source is not None:
        metadata["title_source"] = title_source
    if first_h1 is not None:
        metadata["h1"] = _metadata_value(first_h1)
    for key, value in assertions.metadata.items():
        metadata[key] = _metadata_value(value)
    if len(walk.anchors) > MAX_LINKS_PER_PAGE:
        metadata["links_truncated"] = str(len(walk.anchors))
    metadata = dict(sorted(metadata.items()))

    robots_directives = tuple(sorted(discovery.robots))
    word_count = len(body_text.split())

    digest = content_hash(
        title=title,
        canonical_url=canonical_url,
        meta_description=meta_description,
        language=language,
        publication_state=publication_state,
        robots_directives=robots_directives,
        headings=headings,
        body_text=body_text,
        links=links,
    )

    return NormalizedPage(
        source_type=doc.source_type,
        source_id=source_id,
        external_id=assertions.external_id,
        requested_url=requested_url,
        url=final_url,
        redirect_chain=redirect_chain,
        http_status=doc.http_status,
        fetched_at=doc.fetched_at,
        canonical_url=canonical_url,
        declared_canonical_url=declared_canonical,
        canonical_status=canonical_status,
        title=title,
        meta_description=meta_description,
        language=language,
        publication_state=publication_state,
        page_type=assertions.page_type,
        published_at=published_at,
        modified_at=modified_at,
        robots_directives=robots_directives,
        metadata=metadata,
        body_html=body_html,
        body_text=body_text,
        word_count=word_count,
        headings=headings,
        links=links,
        content_hash=digest,
        hash_version=CONTENT_HASH_VERSION,
    )
