"""The ADR-0011 section 5 placement: natural anchors only, never fabricated.

**Market-keyword anchors** (:func:`market_keyword_anchors`) are what the ENGINE proposes. The
anchor must be the TARGET's own market keyword -- its specific market phrase plus the word
"market" ("cosmetics market", "contract logistics market") -- derived from the target's
identity (:func:`market_keyword_phrases`: its title core, else its canonical URL slug with
geography prefixes and a trailing "-market" removed) and NEVER from the broad ``market_id``
analysis assigned, which generalises to parents such as "food" and produced anchors like
"food market" pointing at a vending-machine report. A bare noun ("cosmetics", "seed") is not a
rankable anchor and is no longer proposed. When the target's geography differs from the
source's, the anchor must also name it ("India cosmetics market"), so a cross-geography link
reads as one. The phrase must still occur VERBATIM in the source text (below): no sentence is
ever rewritten here, so a pair whose anchor is absent is simply not proposed.

**Topic / title / keyword phrases** (:func:`anchor_phrases`) for a target T, in rank order:

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
import html
import itertools
import re
import unicodedata
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Literal
from urllib.parse import unquote, urlsplit

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
    "CONTEXT_ANCHOR_RANK",
    "MARKET_WORD",
    "MAX_LABEL_WORDS",
    "MAX_SECONDARY_TOPICS",
    "MIN_TITLE_GUARD_WORDS",
    "OPENING_BLOCK_PENALTY",
    "PLACEMENT_QUALITY",
    "PREVIEW_CHARS",
    "AnchorPhrase",
    "AnchorSource",
    "PlacementCandidate",
    "anchor_phrases",
    "anchor_rule_ok",
    "block_is_prose",
    "blocked_section_block_ids",
    "corpus_title_phrases",
    "find_placement",
    "keyword_bigrams",
    "market_core",
    "market_keyword_anchors",
    "market_keyword_phrases",
    "market_phrases",
    "market_subphrases",
    "phrase_occurrences",
    "preview_for",
    "slug_text",
    "span_is_safe",
    "title_core",
]

BLOCKED_INLINE_SECTION_TITLES: Final = frozenset(
    {"market overview", "related report", "related reports"}
)
"""Editorial hard exclusions approved by the owner on 2026-11-20.

Market Overview must remain entirely about the page's own market. Related Reports already
performs the cross-sell job, so placing another inline link there is duplicative and pushy.
"""

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


@lru_cache(maxsize=1 << 17)
def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """The whole-phrase matcher for ``phrase``. Pure, so memoised.

    The cache is load-bearing, not a micro-optimisation. The fragment guard passes every
    market label and alias, and each one again with "market" appended, as
    ``enclosing_phrases``: 21,776 distinct patterns on taxonomy v3. ``re`` keeps its own
    cache of only 512, so without this every call recompiled the lot. Measured over ten
    report sources: 219,678 calls, 11.4 s of a 50.9 s run, and one ``find_placement`` call
    alone cost 7.7 s. The phrases come from a fixed taxonomy, so the cache is bounded in
    practice; the maxsize is a guard, not an expected limit.
    """
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
    # Titles reach us with HTML entities intact ("Mobile Financial Services &amp;
    # Neobanking"), and an anchor must be the words a reader sees, never "&amp;". Unescaping
    # first also lets TITLE_SEPARATORS and the trailing-"&" rule below see a real "&".
    text = _collapse(html.unescape(title))
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


# --------------------------------------------------------------------------- #
# Market-keyword anchors (what the engine proposes)
# --------------------------------------------------------------------------- #
def slug_text(url: str | None) -> str | None:
    """The last meaningful path segment of ``url`` as words ("india-cosmetics-market" ->
    "india cosmetics market"), or ``None``.

    Segments are walked from the end, skipping any without a letter (report ids, years), so a
    trailing numeric id does not hide the slug.
    """
    if not url:
        return None
    try:
        path = urlsplit(url).path
    except ValueError:  # pragma: no cover - a stored canonical URL always parses
        return None
    for segment in reversed([s for s in path.split("/") if s]):
        text = _collapse(re.sub(r"[-_]+", " ", unquote(segment)))
        if any(char.isalpha() for char in text):
            return text
    return None


def market_keyword_phrases(
    *, target_url: str | None, target_title: str | None, taxonomy: TaxonomyPolicy
) -> tuple[str, ...]:
    """The target's own specific market phrase(s), WITHOUT the word "market".

    Derived from the target's identity, best first: its title core, then its URL slug
    (:func:`title_core` does the work on both -- it cuts before "market" / "report" /
    "outlook", drops a leading geography and trims trailing years and connectors). The title
    comes first because it keeps real spelling and hyphenation ("Lithium-ion Batteries"),
    which the slug flattens; the slug rescues a noisy editorial title. Deduplicated
    case-insensitively, first spelling wins.
    """
    geo = _geography_phrases(taxonomy)
    out: list[str] = []
    seen: set[str] = set()
    for raw in (target_title, slug_text(target_url)):
        core = title_core(raw, taxonomy=taxonomy)
        if core is None:
            continue
        key = fold(core)
        # A core that is only a geography ("/india-market") names no market: a single-word
        # leading geography is deliberately never stripped to nothing by ``title_core``.
        if key and key not in seen and key not in geo:
            seen.add(key)
            out.append(core)
    return tuple(out)


def market_core(
    *, target_url: str | None, target_title: str | None, taxonomy: TaxonomyPolicy
) -> str | None:
    """The page's market identity, folded for comparison, or ``None``.

    Two pages whose cores are equal are the SAME market in (usually) different geographies:
    "/germany-ev-market" and "/india-ev-market" both fold to ``ev``. This is the signal
    behind ``GeographyDecision.REGIONAL_SIBLING`` (ADR-0027 §1 amendment).

    It is deliberately derived from the page's own identity rather than from the taxonomy's
    ``market_id`` or its relationship edges: taxonomy v3 declares no market relationships at
    all (NEXT_STEPS item 27), and ``market_id`` is a broad parent that would make
    "food market" equal to a vending-machine report. Measured on the 8,858-page inventory,
    this grouping finds 962 market cores spanning more than one geography, covering 3,186
    pages.
    """
    phrases = market_keyword_phrases(
        target_url=target_url, target_title=target_title, taxonomy=taxonomy
    )
    return fold(phrases[0]) if phrases else None


CONTEXT_ANCHOR_RANK: Final = 2
"""Ranks below this are ADR-0027 market-keyword anchors (the target's whole market phrase);
ranks from here up are ADR-0029 context anchors (a run of that phrase)."""

MARKET_WORD: Final = "market"
"""Every engine anchor ends in this word: an anchor must read as a market, not as a noun."""


def _with_market_word(phrase: str) -> str | None:
    """``phrase`` plus "market", or ``None`` when the phrase carries no market of its own
    (a title that is nothing but "Market" would yield the anchor "market")."""
    if fold(phrase) == MARKET_WORD:
        return None
    return f"{phrase} {MARKET_WORD}"


#: Words an anchor may never begin or end on. A contiguous run of a market phrase is only a
#: usable anchor if it reads as a noun phrase: "and Fleet Management" and "Leasing and" do not.
_EDGE_STOP_WORDS: Final = frozenset(
    {"a", "an", "and", "by", "for", "in", "of", "or", "the", "to", "with"}
)


def market_subphrases(phrase: str) -> tuple[str, ...]:
    """Contiguous multi-word runs of ``phrase``, longest first, shorter than ``phrase`` itself.

    ADR-0029: a report's full market phrase ("Electric Two-Wheeler Adoption Ecosystem") does not
    recur in other reports' prose, but the concepts inside it do ("two-wheeler"). These runs are
    the only context anchors the engine will offer -- still the target's subject, in the target's
    own words, never a synonym or model output.
    """
    words = [w for w in _collapse(phrase).split(" ") if w]
    out: list[str] = []
    for size in range(len(words) - 1, 1, -1):
        for start in range(len(words) - size + 1):
            run = words[start : start + size]
            if fold(run[0]) in _EDGE_STOP_WORDS or fold(run[-1]) in _EDGE_STOP_WORDS:
                continue
            out.append(" ".join(run))
    return tuple(out)


def market_keyword_anchors(
    *,
    target_url: str | None,
    target_title: str | None,
    taxonomy: TaxonomyPolicy,
    policy: RecommendationPolicy,
    geography_terms: Sequence[str] = (),
    require_geography: bool = False,
    allow_context: bool = True,
) -> tuple[AnchorPhrase, ...]:
    """The target's market-keyword anchors, then its context anchors, in rank order. Pure.

    ``geography_terms`` name the target's geography (label and longer aliases, longest first).
    Geography-qualified anchors ("India cosmetics market") rank FIRST because they are the more
    specific link text; with ``require_geography`` they are the only ones offered, which is how
    the engine refuses to label a cross-geography link as a local one. Every phrase passes the
    usual anchor rules (:func:`anchor_rule_ok`), so an overlong editorial phrase is dropped
    rather than truncated.

    **Context anchors (ADR-0029), ranked after every market-keyword form.** The market keyword
    is derived from the target's WHOLE title, and whole titles do not recur in other reports'
    prose -- measured over two live Ken Consulting corpora, 0 of 24 pages named another report's
    market, so the engine proposed nothing at all. A context anchor is a contiguous run of that
    same phrase (:func:`market_subphrases`). Under ``require_geography`` a run is offered either
    geography-qualified, exactly as the keyword is, or bare with ``context_terms`` -- admissible
    only in a block that itself names the target's geography. Honesty about where the link goes
    is preserved either way; "the geography appears somewhere on the page" was rejected.

    ``allow_context=False`` restores the pre-ADR-0029 behaviour, for comparison.
    """
    phrases = market_keyword_phrases(
        target_url=target_url, target_title=target_title, taxonomy=taxonomy
    )
    terms = tuple(term for term in geography_terms if _collapse(term))
    # (rank, text, context_terms). Lower rank wins; keyword forms occupy 0-1, context 2-5.
    groups: list[tuple[int, str, tuple[str, ...]]] = []
    for phrase in phrases:
        core = _with_market_word(phrase)
        if core is None:
            continue
        groups.extend((0, f"{term} {core}", ()) for term in terms)
        if not require_geography:
            groups.append((1, core, ()))
        if not allow_context or len(phrase.split()) > policy.placement.max_anchor_words:
            # A phrase too long to be an anchor in its own right is a noisy editorial title,
            # not a market name ("How a Global Food Leader Found Many Opportunities in KSA
            # Catering"). Slicing it yields prose fragments, so it is never sliced.
            continue
        for run in market_subphrases(phrase):
            for offset, form in ((0, _with_market_word(run)), (2, run)):
                if form is None:
                    continue
                groups.extend((2 + offset, f"{term} {form}", ()) for term in terms)
                if require_geography:
                    # The anchor does not name the geography, so its BLOCK must.
                    groups.append((3 + offset, form, terms))
                else:
                    groups.append((3 + offset, form, ()))
    geo = _geography_phrases(taxonomy)
    seen: set[str] = set()
    out: list[AnchorPhrase] = []
    # Stable on rank alone: insertion order already carries the preferences that matter --
    # the geography LABEL before its aliases (ADR-0027), and longer runs before shorter.
    for rank, text, context in sorted(groups, key=lambda item: item[0]):
        candidate = _collapse(text)
        key = fold(candidate)
        if not candidate or key in seen or key in geo:
            continue
        if not anchor_rule_ok(candidate, "target_topic", policy=policy.placement):
            continue
        if require_geography and rank >= 2 and not context and not terms:
            continue  # a context anchor with no way to name the geography is never offered
        seen.add(key)
        out.append(
            AnchorPhrase(text=candidate, source="target_topic", rank=rank, context_terms=context)
        )
    return tuple(out)


_SENTENCE_END: Final = frozenset(".!?")
"""Sentence-terminal punctuation.

ASCII only, deliberately. Whether the site is multilingual is still an open question
(NEXT_STEPS item 6) and every page in the 8,858-page inventory is English, so CJK and other
scripts' terminators would be speculative. If a non-Latin language is ever ingested, this
set and :func:`block_is_prose` are where it lands: a page whose sentences end in U+3002
would read as one long label, and the length arm of the test is what keeps that from
silently refusing every block."""

MAX_LABEL_WORDS: Final = 25
"""A block longer than this is treated as prose even with no sentence punctuation.

Ken's longest cross-sell titles run to about 16 words ("Indonesia Payments Market Size,
Share & Forecast, By Payment Method, Customer Segment & Distribution Channel"), so the
bound sits above them with room to spare, while no real title approaches 25."""


def block_is_prose(text: str) -> bool:
    """Does this block read as a sentence, rather than as a label or a title?

    An inline contextual anchor belongs only in prose. Ken's report pages end with a
    cross-sell list of other reports, one title per list item -- "Thailand Residential
    Furniture Market", "KSA Modular Kitchen Furniture Market" -- and the engine was
    anchoring on the tail of those titles, so a France report linked to a Germany report
    through text naming Thailand. 185 of 1,028 plans (18%) on the full corpus run were this.

    Two conditions, and a block need satisfy only one: it carries sentence-terminal
    punctuation, or it is longer than :data:`MAX_LABEL_WORDS`. Punctuation alone was too
    blunt -- it rejected long unpunctuated paragraphs, which are not titles -- and length
    alone would admit every short label.

    The test is intrinsic to the block, deliberately NOT a comparison against known page
    titles: those cross-sell entries link to pages OUTSIDE the inventory (the sitemaps cover
    about 18% of the live site), so their titles are simply not available to compare
    against. That is why the corpus-title guard (:func:`corpus_title_phrases`) could not
    catch them, and this can.

    A list item is still prose when it is written as one ("Fleet managers cite the electric
    vehicle market as their main planning input."), so this does not exclude ``li`` as a
    kind -- that is the policy's job (``block_kinds``).

    Erring toward refusal is deliberate: a missed link costs a link, while an anchor in
    another report's title misleads the reader, which is the failure the owner rejected.
    """
    if any(char in _SENTENCE_END for char in text):
        return True
    return len(text.split()) > MAX_LABEL_WORDS


def _section_key(text: str) -> str:
    """Normalised visible heading text for deterministic section matching."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", fold(text)).split())


def blocked_section_block_ids(blocks: Sequence[ContentBlock]) -> frozenset[str]:
    """Blocks governed by a Market Overview or Related Reports heading.

    The heading itself and every following block remain excluded until the next heading.
    Pages without one of these visible headings are unchanged. This is deliberately a hard
    placement rule rather than a score penalty: a high semantic score may not buy its way
    into a section whose editorial purpose forbids cross-market links.
    """
    blocked: set[str] = set()
    inside_blocked_section = False
    for block in blocks:
        if block.kind in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            key = _section_key(block.text)
            inside_blocked_section = any(
                key == title or key.startswith(title + " ")
                for title in BLOCKED_INLINE_SECTION_TITLES
            )
        if inside_blocked_section:
            blocked.add(block.block_id)
    return frozenset(blocked)


MIN_TITLE_GUARD_WORDS: Final = 3
"""A title shorter than this is not used as a fragment guard. A two-word title
("Coffee Market") is often exactly the right anchor, and guarding on it would block the
anchor wherever the title happens to appear."""


def corpus_title_phrases(titles: Iterable[str | None]) -> tuple[str, ...]:
    """Page titles usable as ``find_placement`` ``enclosing_phrases``, deduplicated.

    Ken's reports end in a cross-sell list of other reports, one title per list item. Those
    titles are not taxonomy market labels, so :func:`market_phrases` misses them and the
    engine anchored on their tail -- "Thailand Residential [[Furniture Market]]" pointing at
    a Germany furniture report (185 of 1,028 plans, 18%, on the full corpus run).

    Titles are unescaped (they arrive with ``&amp;`` intact) and cut at the first
    :data:`TITLE_SEPARATORS` match, so "X Market: Outlook to 2030" guards on "X Market"
    rather than on the whole editorial string, which would rarely occur verbatim.

    Only titles of :data:`MIN_TITLE_GUARD_WORDS` words or more are returned: the guard
    blocks an anchor that is a STRICT fragment of one of these, and a short title is usually
    the legitimate anchor itself.
    """
    out: set[str] = set()
    for raw in titles:
        if not raw:
            continue
        text = _collapse(html.unescape(raw))
        cuts = [text.find(sep) for sep in TITLE_SEPARATORS if text.find(sep) > 0]
        if cuts:
            text = text[: min(cuts)]
        text = _collapse(text)
        if len(text.split(" ")) >= MIN_TITLE_GUARD_WORDS:
            out.add(text)
    return tuple(sorted(out))


def market_phrases(taxonomy: TaxonomyPolicy) -> tuple[str, ...]:
    """Every market label and alias, and each one followed by "market" (collapsed): the
    phrases an anchor must not be a strict fragment of (:func:`find_placement`
    ``enclosing_phrases``).

    The ``"<label> market"`` forms matter as much as the bare labels. An anchor like
    "Esports Market" is NOT strictly inside the bare label "cloud gaming and esports" -- it
    overlaps the tail and runs past it -- so without the suffixed form the fragment guard
    misses it and the engine links a slice of a longer market name ("The India Cloud Gaming
    and [[Esports Market]] is projected to ..."), which misreads the sentence.
    """
    bare = {_collapse(p) for m in taxonomy.markets for p in (m.label, *m.aliases) if _collapse(p)}
    return tuple(sorted(bare | {f"{p} {MARKET_WORD}" for p in bare}))


#: Lowercase words that may sit INSIDE a proper name without ending it ("Ministry of Trade",
#: "National Strategy for Data and Artificial Intelligence").
_NAME_CONNECTORS: Final = frozenset({"and", "for", "of", "the", "de", "van", "al"})

_WORD_SPAN: Final = re.compile(r"\S+")


def _names_a_longer_entity(text: str, start: int, end: int) -> bool:
    """``text[start:end]`` is a strict part of a longer Title-Case name in ``text``.

    ADR-0029 context anchors are runs of words, so they can slice through a proper noun that
    the taxonomy has never heard of. Both real cases found on the Saudi corpus, 2026-11-19:

        "National Strategy for Data and Artificial Intelligence"
            -> anchor "Data and Artificial Intelligence"
        "Saudi Data and Artificial Intelligence Authority (SDAIA)"
            -> anchor "Saudi Data and Artificial Intelligence"

    Both would have linked an organisation's name to a market report. The maximal Title-Case
    run containing the span is grown outwards across capitalised words, stepping over lowercase
    connectors; if it is longer than the span, the span is part of a name and not an anchor.

    A lowercase span ("...technologies such as artificial intelligence and cloud computing...")
    starts no run and is unaffected, which is the ordinary topical mention this rule must allow.
    """
    words = [(m.start(), m.end()) for m in _WORD_SPAN.finditer(text)]
    inside = [i for i, (s, e) in enumerate(words) if s < end and start < e]
    if not inside:
        return False
    first, last = inside[0], inside[-1]

    def capitalised(index: int) -> bool:
        word = text[words[index][0] : words[index][1]].lstrip("(\"'“")
        return bool(word) and word[0].isupper()

    def connector(index: int) -> bool:
        return fold(text[words[index][0] : words[index][1]].strip(".,;:()")) in _NAME_CONNECTORS

    if not capitalised(first):
        return False  # the span is not itself part of a Title-Case run
    left = first
    while left > 0:
        if capitalised(left - 1):
            left -= 1
        elif left > 1 and connector(left - 1) and capitalised(left - 2):
            left -= 2
        else:
            break
    right = last
    while right + 1 < len(words):
        if capitalised(right + 1):
            right += 1
        elif right + 2 < len(words) and connector(right + 1) and capitalised(right + 2):
            right += 2
        else:
            break
    # A run that begins at the first word of the block is usually sentence capitalisation
    # rather than a name, so it is not evidence on its own.
    if left == 0 and right == last and first == 0:
        return False
    return (left, right) != (first, last)


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

    Only blocks that read as prose are considered (:func:`block_is_prose`): a cross-sell
    list entry is another report's title, not a sentence, and an inline anchor in one
    misreads it.

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
    section_blocked = blocked_section_block_ids(blocks)
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
        if block.block_id in section_blocked:
            continue
        if not block_is_prose(block.text):
            continue  # a title or label, not a sentence: never host an inline anchor
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
                if phrase.rank >= CONTEXT_ANCHOR_RANK and _names_a_longer_entity(
                    block.text, start, end
                ):
                    continue  # a slice of a proper noun, not a mention of the target
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
