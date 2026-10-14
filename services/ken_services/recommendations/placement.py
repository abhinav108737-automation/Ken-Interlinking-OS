"""The ADR-0011 section 5 placement: natural anchors only, never fabricated.

**Anchor phrases** (:func:`anchor_phrases`) for a target T, in rank order:

* rank 0, ``target_topic``: the label and aliases of T's ``market_id`` node;
* ranks 1..k, ``target_topic``: the label and aliases of T's other CONFIDENT top topics at
  depth >= 1 (score >= ``MARKET_CONFIDENCE_THRESHOLD``, at most :data:`MAX_SECONDARY_TOPICS`,
  in analysis order), one rank per topic;
* rank k+1, ``target_title``: T's title core (:func:`title_core`);
* rank k+2, ``target_keyword``: two-word keyword bigrams of T's title (:func:`keyword_bigrams`).

Every phrase is NFC/whitespace-collapsed, deduplicated case-insensitively (the first, i.e.
best-ranked, spelling wins) and dropped unless it passes the anchor rules: word count
(``min_anchor_words`` for topics, ``min_anchor_words_non_topic`` otherwise, at most
``max_anchor_words``), ``min_anchor_chars``, at most :data:`ANCHOR_MAX_CHARS` (the LinkPlan
limit), not a ``forbidden_anchors`` entry, and **never a geography label or alias** ("India"
must not link to a market page). Order: (rank, longer first, folded text).

**Search** (:func:`find_placement`): blocks of an allowed kind, skipping blocks that already
hold ``max_links_per_block`` links, blocks that already link to T, blocks enclosed by an
excluded element (``<td><p>``, ``<blockquote><p>``) and, with ``skip_first_block``, the
opening block. Occurrences are whole-phrase and case-insensitive
(:func:`phrase_occurrences`). A span must lie inside ONE text run with no excluded
ancestor (so never inside an existing link: nested links are impossible), and must not
overlap a span already taken for this source (same ``context_hash``), and must not be a
strict FRAGMENT of a longer market phrase or anchor phrase occurring there ("battery
energy" inside "battery energy storage"). The anchor is copied
VERBATIM from ``block.text[start:end]`` and re-checked against the anchor rules.

**Choice**: the minimum of (phrase rank, anchor length descending, block salience
descending, block order, occurrence index, start). ``placement_quality``: 1.0 topic, 0.9
title, 0.75 keyword, minus :data:`OPENING_BLOCK_PENALTY` in the opening block (the first
non-empty block of an allowed kind: the opening paragraph; headings never count).
"""

from __future__ import annotations

import bisect
import itertools
import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from ken_contracts.intelligence import Intent, PageAnalysis, TaxonomyPolicy
from ken_contracts.recommendations import (
    PlacementLocator,
    PlacementPolicy,
    PlacementPreview,
    RecommendationPolicy,
)
from ken_services.intelligence.analyze import MARKET_CONFIDENCE_THRESHOLD
from ken_services.intelligence.taxonomy import taxonomy_index
from ken_services.intelligence.text import fold, tokens
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import ContentBlock

__all__ = [
    "ANCHOR_MAX_CHARS",
    "MAX_SECONDARY_TOPICS",
    "OPENING_BLOCK_PENALTY",
    "PLACEMENT_QUALITY",
    "PREVIEW_CHARS",
    "AnchorPhrase",
    "AnchorSource",
    "PlacementCandidate",
    "anchor_phrases",
    "anchor_rule_ok",
    "find_placement",
    "keyword_bigrams",
    "market_phrases",
    "phrase_occurrences",
    "preview_for",
    "span_is_safe",
    "title_core",
]

type AnchorSource = Literal["target_topic", "target_title", "target_keyword"]

PLACEMENT_QUALITY: Final[Mapping[str, float]] = {
    "target_topic": 1.0,
    "target_title": 0.9,
    "target_keyword": 0.75,
}
OPENING_BLOCK_PENALTY: Final = 0.1
PREVIEW_CHARS: Final = 200
ANCHOR_MAX_CHARS: Final = 120
"""``LinkPlan.anchor_text`` max length."""
MAX_SECONDARY_TOPICS: Final = 3

TITLE_TAIL_WORDS: Final = frozenset(
    {"market", "markets", "report", "reports", "outlook", "outlooks", "industry", "industries"}
)
"""A title core ends before the first of these (at word index >= 1)."""
TITLE_SEPARATORS: Final = (" | ", " \u2013 ", " \u2014 ", " - ", ": ")
"""A title core ends before the first of these (site names, subtitles)."""
TITLE_TRAILING_CONNECTORS: Final = frozenset({"in", "of", "for", "and", "across", "the", "&"})
GENERIC_TITLE_WORDS: Final = TITLE_TAIL_WORDS | frozenset(
    {"size", "share", "growth", "trends", "trend", "forecast", "analysis", "overview", "outlook"}
)
"""Never part of a keyword bigram."""

_BOUNDARY: Final = "\\w\\-\u2010\u2011"
"""A phrase never starts or ends next to a word character or a hyphen."""
_EDGE_PUNCT_RE: Final = re.compile(r"^[^\w]+|[^\w]+$")


@dataclass(frozen=True, slots=True)
class AnchorPhrase:
    text: str
    source: AnchorSource
    rank: int
    context_terms: tuple[str, ...] = ()
    """If non-empty, the phrase is only a valid anchor in a block that ALSO names one of
    these (whole-phrase, case-insensitive): the surrounding BLOCK (paragraph or list item)
    must support it. Per block, not per sentence."""


@dataclass(frozen=True, slots=True)
class PlacementCandidate:
    anchor_text: str
    """Verbatim ``block.text[locator.start:locator.end]``."""
    anchor_source: AnchorSource
    locator: PlacementLocator
    preview: PlacementPreview
    placement_quality: float


# --------------------------------------------------------------------------- #
# Phrase helpers
# --------------------------------------------------------------------------- #
def _collapse(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    body = " ".join(re.escape(word) for word in _collapse(phrase).split(" "))
    return re.compile(rf"(?<![{_BOUNDARY}]){body}(?![{_BOUNDARY}])", re.IGNORECASE)


def phrase_occurrences(phrase: str, text: str) -> tuple[tuple[int, int], ...]:
    """``(start, end)`` of every whole-phrase, case-insensitive occurrence, in text order.

    ``text`` is a normalised block text (single spaces). Occurrences do not overlap. The
    position of an occurrence in this tuple is its ``occurrence_index``; Phase 6 must locate
    an anchor with this same function.
    """
    if not _collapse(phrase):
        return ()
    return tuple((m.start(), m.end()) for m in _phrase_pattern(phrase).finditer(text))


def _geography_phrases(taxonomy: TaxonomyPolicy) -> frozenset[str]:
    return frozenset(fold(p) for g in taxonomy.geographies for p in (g.label, *g.aliases))


def anchor_rule_ok(text: str, source: str, *, policy: PlacementPolicy) -> bool:
    """The anchor rules of ADR section 5.3 (words, characters, forbidden anchors)."""
    words = len(text.split())
    min_words = (
        policy.min_anchor_words if source == "target_topic" else (policy.min_anchor_words_non_topic)
    )
    if not min_words <= words <= policy.max_anchor_words:
        return False
    if not policy.min_anchor_chars <= len(text) <= ANCHOR_MAX_CHARS:
        return False
    if not any(char.isalnum() for char in text):
        return False
    return fold(text) not in {fold(f) for f in policy.forbidden_anchors}


# --------------------------------------------------------------------------- #
# Title core and keyword bigrams
# --------------------------------------------------------------------------- #
def _strip_leading_geography(text: str, geo: frozenset[str]) -> str:
    words = text.split(" ")
    for size in range(min(4, len(words) - 1), 0, -1):  # longest geography phrase first
        head = fold(" ".join(words[:size]))
        head = re.sub("['\u2019]s$", "", head)
        if head in geo:
            return " ".join(words[size:])
    return text


def _strip_trailing(text: str, geo: frozenset[str]) -> str:
    words = text.split(" ")
    changed = True
    while changed and words:
        changed = False
        for size in range(min(4, len(words)), 0, -1):  # a trailing geography ("... in India")
            if len(words) > size and fold(" ".join(words[-size:])) in geo:
                words = words[:-size]
                changed = True
                break
        while words and (
            fold(_EDGE_PUNCT_RE.sub("", words[-1])) in TITLE_TRAILING_CONNECTORS
            or not any(c.isalpha() for c in words[-1])  # years, ranges, punctuation
        ):
            words = words[:-1]
            changed = True
    return " ".join(words)


def title_core(title: str | None, *, taxonomy: TaxonomyPolicy) -> str | None:
    """The descriptive core of a target title, or ``None``. Deterministic:

    1. NFC + whitespace collapse; cut before the first :data:`TITLE_SEPARATORS` match;
    2. cut before the first :data:`TITLE_TAIL_WORDS` word at word index >= 1
       ("India EV Charging Market Outlook to 2030" -> "India EV Charging");
    3. drop ONE leading geography label/alias (optionally possessive: "India's"), longest
       first ("Southeast Asia EV Charging" -> "EV Charging");
    4. repeatedly drop a trailing geography phrase, trailing connectors (in, of, for, and,
       across, the, &) and trailing tokens without letters (years, ranges, punctuation);
    5. strip edge punctuation. The anchor rules (2..8 words etc.) are applied by
       :func:`anchor_phrases`, not here.
    """
    if not title:
        return None
    text = _collapse(title)
    cuts = [text.find(sep) for sep in TITLE_SEPARATORS if text.find(sep) > 0]
    if cuts:
        text = text[: min(cuts)]
    words = text.split(" ")
    for i, word in enumerate(words):
        if i >= 1 and fold(_EDGE_PUNCT_RE.sub("", word)) in TITLE_TAIL_WORDS:
            words = words[:i]
            break
    geo = _geography_phrases(taxonomy)
    text = _strip_leading_geography(" ".join(words), geo)
    text = _strip_trailing(text, geo)
    text = _EDGE_PUNCT_RE.sub("", text)
    return _collapse(text) or None


def keyword_bigrams(
    title: str | None, *, keywords: Sequence[str], taxonomy: TaxonomyPolicy
) -> tuple[str, ...]:
    """Adjacent word pairs of ``title`` whose words are both target keywords, title order.

    A word qualifies when its keyword tokens (:func:`~ken_services.intelligence.text.tokens`)
    are non-empty and all among ``keywords``, it is not a geography phrase, and it is not a
    :data:`GENERIC_TITLE_WORDS` word. Two words are adjacent only when no punctuation
    separates them (``"EV, Charging"`` is not a pair). Original casing is kept.
    """
    if not title:
        return ()
    keyword_set = frozenset(keywords)
    geo = _geography_phrases(taxonomy)
    words = _collapse(title).split(" ")
    out: list[str] = []
    for left, right in itertools.pairwise(words):
        if re.search(r"[^\w]$", left) or re.search(r"^[^\w]", right):
            continue  # punctuation between the two words
        pair = []
        for word in (left, right):
            bare = _EDGE_PUNCT_RE.sub("", word)
            parts = tokens(bare)
            folded = fold(bare)
            if (
                not parts
                or any(part not in keyword_set for part in parts)
                or folded in geo
                or folded in GENERIC_TITLE_WORDS
            ):
                break
            pair.append(bare)
        else:
            if len(pair) == 2:
                out.append(f"{pair[0]} {pair[1]}")
    return tuple(dict.fromkeys(out))


def anchor_phrases(
    *,
    target: PageAnalysis,
    target_title: str | None,
    taxonomy: TaxonomyPolicy,
    policy: RecommendationPolicy,
    source: PageAnalysis | None = None,
) -> tuple[AnchorPhrase, ...]:
    """The target's anchor candidates in rank order (module docstring). Pure.

    With ``source``, a phrase that names the SOURCE page's own market (its label or
    an alias) can describe that page itself: on the India cold-chain report, "cold
    chain" alone must not link to the UAE cold-chain report (PRD v1.1 section 12.4,
    no misleading anchors). Such a phrase is kept ONLY with ``context_terms`` = the
    target's distinctive geographies (those the source does not cover), so it is a
    valid anchor only in a block that also names one of them ("cold chain capacity
    in the UAE"). If the target has no distinctive geography, the phrase is dropped --
    EXCEPT on a commercial hop (an INFORMATIONAL page linking to the commercial-research
    page on that market), where it is the natural anchor. A title core or keyword bigram
    that is a word fragment of an own-market name is treated as that name.
    """
    index = taxonomy_index(taxonomy)
    geo = _geography_phrases(taxonomy)
    own: set[str] = set()
    distinctive: tuple[str, ...] = ()
    # An article (informational intent) linking to the commercial-research page on the
    # SAME market and geography is the core reader journey -- from discussion to the
    # authoritative report -- not a misleading anchor: "cold chain" in an India vaccine
    # cold-chain article may link to the India cold-chain market report (ADR-0011 s9.1).
    commercial_hop = (
        source is not None
        and target.intent is Intent.COMMERCIAL_RESEARCH
        and source.intent is Intent.INFORMATIONAL
    )
    if source is not None and source.market_id is not None and index.has_market(source.market_id):
        own_node = index.market(source.market_id)
        own = {fold(_collapse(p)) for p in (own_node.label, *own_node.aliases) if _collapse(p)}
        # The source's geographies AND every region containing them are not
        # distinctive: "Asia Pacific" on an India page does not point at Indonesia.
        shared: set[str] = set()
        for mention in source.geographies:
            if index.has_geography(mention.geo_id):
                shared.add(mention.geo_id)
                shared.update(index.geography_ancestors(mention.geo_id))
        distinct_ids: set[str] = set()
        for mention in target.geographies:
            if not index.has_geography(mention.geo_id) or mention.geo_id in shared:
                continue
            distinct_ids.add(mention.geo_id)
            # A distinctive country's regions support it too ("across Southeast Asia").
            distinct_ids.update(
                a for a in index.geography_ancestors(mention.geo_id) if a not in shared
            )
        distinct_ids.discard("global")
        terms: set[str] = set()
        for geo_id in distinct_ids:
            geo_node = index.geography(geo_id)
            terms.update(_collapse(p) for p in (geo_node.label, *geo_node.aliases) if _collapse(p))
        distinctive = tuple(sorted(terms, key=lambda t: (-len(t), fold(t))))
    rules = policy.placement
    groups: list[tuple[AnchorSource, tuple[str, ...]]] = []
    market_id = target.market_id
    if market_id is not None and index.has_market(market_id):
        node = index.market(market_id)
        groups.append(("target_topic", (node.label, *node.aliases)))
    else:
        groups.append(("target_topic", ()))
    secondary = [
        topic
        for topic in target.topics
        if topic.depth >= 1
        and topic.node_id != market_id
        and topic.score >= MARKET_CONFIDENCE_THRESHOLD
        and index.has_market(topic.node_id)
        # Only sub-markets of the target's OWN market: a secondary topic outside it
        # names another page's market (e.g. a cold-chain report's "active
        # pharmaceutical ingredients" mention) and would mislead (eval finding).
        and market_id is not None
        and market_id in index.market_ancestors(topic.node_id)
    ][:MAX_SECONDARY_TOPICS]
    for topic in secondary:
        node = index.market(topic.node_id)
        groups.append(("target_topic", (node.label, *node.aliases)))
    core = title_core(target_title, taxonomy=taxonomy)
    groups.append(("target_title", (core,) if core else ()))
    groups.append(
        (
            "target_keyword",
            keyword_bigrams(target_title, keywords=target.keywords, taxonomy=taxonomy),
        )
    )

    seen: set[str] = set()
    out: list[AnchorPhrase] = []
    for rank, (kind, raw_phrases) in enumerate(groups):
        candidates = sorted(
            {_collapse(p) for p in raw_phrases if _collapse(p)},
            key=lambda p: (-len(p), fold(p), p),
        )
        for phrase in candidates:
            key = fold(phrase)
            if key in seen or key in geo:
                continue
            if not anchor_rule_ok(phrase, kind, policy=rules):
                continue
            context: tuple[str, ...] = ()
            # The source's own market name -- or, for a title core / keyword bigram, a word
            # fragment of one (QA H1). Topic phrases match exactly: a parent or related
            # market's label ("logistics") may share a word with an own alias
            # ("temperature-controlled logistics") and still name a different page.
            own_name = key in own if kind == "target_topic" else _within_any(key, own)
            if own_name:
                if distinctive:
                    context = distinctive  # the block must name the target's geography
                elif not commercial_hop:
                    continue  # self-describing, and nothing distinguishes the target
            seen.add(key)
            out.append(AnchorPhrase(text=phrase, source=kind, rank=rank, context_terms=context))
    return tuple(out)


def _within_any(key: str, phrases: Collection[str]) -> bool:
    """``key`` (folded) equals, or is a contiguous word sub-span of, one of ``phrases``."""
    words = key.split()
    n = len(words)
    for phrase in phrases:
        other = phrase.split()
        if any(other[i : i + n] == words for i in range(len(other) - n + 1)):
            return True
    return False


def market_phrases(taxonomy: TaxonomyPolicy) -> tuple[str, ...]:
    """Every market label and alias (collapsed): the phrases an anchor must not be a
    strict fragment of (:func:`find_placement` ``enclosing_phrases``)."""
    return tuple(
        sorted(
            {_collapse(p) for m in taxonomy.markets for p in (m.label, *m.aliases) if _collapse(p)}
        )
    )


def _is_fragment(
    text: str, start: int, end: int, enclosing: Sequence[tuple[str, re.Pattern[str]]]
) -> bool:
    """``text[start:end]`` lies strictly inside an occurrence of a longer enclosing phrase
    ("battery energy" inside "battery energy storage"): linking it would cut a name."""
    key = fold(text[start:end])
    for folded, pattern in enclosing:
        if len(folded) <= len(key) or key not in folded:
            continue
        for match in pattern.finditer(text):
            if match.start() > start:
                break
            if end <= match.end() and (match.start(), match.end()) != (start, end):
                return True
    return False


# --------------------------------------------------------------------------- #
# Placement search
# --------------------------------------------------------------------------- #
def span_is_safe(
    block: ContentBlock, start: int, end: int, *, excluded_ancestors: Collection[str]
) -> bool:
    """``block.text[start:end]`` lies inside ONE run, and no excluded element encloses it.

    A block without runs (built by hand, not by ``extract_blocks``) is never safe.
    """
    excluded = frozenset(excluded_ancestors)
    if not 0 <= start < end <= len(block.text) or block.enclosing & excluded:
        return False
    starts = [run.start for run in block.runs]
    position = bisect.bisect_right(starts, start) - 1
    if position < 0:
        return False
    run = block.runs[position]
    return run.start <= start and end <= run.end and not (run.ancestors & excluded)


def _trim_before(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    if not text[-limit - 1].isspace() and not cut[0].isspace():  # cut lands mid-word
        space = cut.find(" ")
        if space >= 0 and cut[space + 1 :].strip():  # else: one overlong word, hard-cut
            cut = cut[space + 1 :]
    return cut.lstrip()


def _trim_after(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if not text[limit].isspace() and not cut[-1].isspace():  # cut lands mid-word
        space = cut.rfind(" ")
        if space >= 0 and cut[:space].strip():  # else: one overlong word, hard-cut
            cut = cut[:space]
    return cut.rstrip()


def preview_for(text: str, start: int, end: int, *, limit: int = PREVIEW_CHARS) -> PlacementPreview:
    """Verbatim block text around the anchor: at most ``limit`` chars each side, cut at a
    word boundary (only a single word longer than ``limit`` is hard-cut)."""
    return PlacementPreview(
        before=_trim_before(text[:start], limit),
        anchor=text[start:end],
        after=_trim_after(text[end:], limit),
    )


def find_placement(
    *,
    blocks: Sequence[ContentBlock],
    phrases: Sequence[AnchorPhrase],
    links_per_block: Mapping[str, int],
    blocks_linking_target: Collection[str],
    taken_spans: Collection[tuple[str, int, int]],
    salience: Mapping[str, float],
    policy: RecommendationPolicy,
    enclosing_phrases: Collection[str] = (),
) -> PlacementCandidate | None:
    """The best natural placement of any phrase in the source blocks, or ``None``.

    ``enclosing_phrases`` (with the phrases themselves): a match strictly inside an
    occurrence of a longer one of these is a FRAGMENT and never an anchor.

    ``links_per_block``: block_id -> existing (+ already proposed) links in that block.
    ``blocks_linking_target``: block ids that already link to the target.
    ``taken_spans``: ``(context_hash, start, end)`` already used for this source.
    ``salience``: block_id -> salient-block score of the source analysis (missing = 0).
    """
    rules = policy.placement
    kinds = frozenset(rules.block_kinds)
    excluded = frozenset(rules.excluded_ancestors)
    max_links = policy.selection.max_links_per_block
    opening = next((b.block_id for b in blocks if b.kind in kinds and b.text), None)
    compiled = [
        (phrase, _phrase_pattern(phrase.text)) for phrase in phrases if _collapse(phrase.text)
    ]
    enclosing = [
        (fold(text), _phrase_pattern(text))
        for text in sorted({_collapse(t) for t in (*enclosing_phrases, *(p.text for p in phrases))})
        if text
    ]

    best: tuple[tuple[int, int, float, int, int, int], PlacementCandidate] | None = None
    for order, block in enumerate(blocks):
        if block.kind not in kinds or not block.text:
            continue
        if rules.skip_first_block and block.block_id == opening:
            continue
        if links_per_block.get(block.block_id, 0) >= max_links:
            continue
        if block.block_id in blocks_linking_target or block.enclosing & excluded:
            continue
        context_hash = sha256_hex(block.text)
        taken = [(s, e) for h, s, e in taken_spans if h == context_hash]
        block_salience = float(salience.get(block.block_id, 0.0))
        for phrase, pattern in compiled:
            if phrase.context_terms and not any(
                _phrase_pattern(term).search(block.text) for term in phrase.context_terms
            ):
                continue  # the surrounding text does not support this anchor
            for occurrence, match in enumerate(pattern.finditer(block.text)):
                start, end = match.start(), match.end()
                anchor = block.text[start:end]
                key = (phrase.rank, -len(anchor), -block_salience, order, occurrence, start)
                if best is not None and key >= best[0]:
                    continue
                if not anchor_rule_ok(anchor, phrase.source, policy=rules):
                    continue
                if not span_is_safe(block, start, end, excluded_ancestors=excluded):
                    continue
                if _is_fragment(block.text, start, end, enclosing):
                    continue
                if any(start < t_end and t_start < end for t_start, t_end in taken):
                    continue
                quality = PLACEMENT_QUALITY[phrase.source]
                if block.block_id == opening:
                    quality = round(quality - OPENING_BLOCK_PENALTY, 6)
                candidate = PlacementCandidate(
                    anchor_text=anchor,
                    anchor_source=phrase.source,
                    locator=PlacementLocator(
                        block_id=block.block_id,
                        block_kind=block.kind,
                        occurrence_index=occurrence,
                        start=start,
                        end=end,
                        context_hash=context_hash,
                    ),
                    preview=preview_for(block.text, start, end),
                    placement_quality=quality,
                )
                best = (key, candidate)
    return None if best is None else best[1]
