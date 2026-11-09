"""Deterministic page analysis from the taxonomy policy (ADR-0010 section 3).

:func:`analyze_page` turns one stored page version into a
:class:`~ken_contracts.intelligence.PageAnalysis`. It is pure: no database, no
network, no model, no clock, no randomness; the same input and policy always
give an equal result.

Scoring (all constants below are part of ``ANALYSIS_ALGORITHM_VERSION``):

* **Mentions** are found by the policy's phrase matchers (whole phrases,
  case-insensitive, NFC, longest match first; see
  :mod:`ken_services.intelligence.text`), separately in the title, the
  headings, the meta description and the body text.
* **Location weights**: title 5, each heading 3, meta description 2, body 1 per
  mention. Body mentions count at most :data:`BODY_MENTION_CAP` times per node,
  so a long page cannot drown its title. ``body_text`` contains the heading
  text too (Phase 1 joins every block), so a heading mention scores 3 + 1.
* **Topic score** of a market node = the weighted sum of its OWN mentions
  (ancestors are not credited with descendants' mentions).
* ``market_id`` = among nodes scoring at least
  :data:`MARKET_CONFIDENCE_THRESHOLD`, the best by (higher score, deeper node,
  id). ``industry_id`` = the root of ``market_id``; with no confident market,
  the highest-scoring industry node (then id); with no industry mention at all,
  the root of the top topic; else ``None``.
* **Topics**: every node with a mention, sorted by (-score, depth, id), top
  :data:`MAX_TOPICS`.
* **Geographies**: regions and countries with score >= 1, sorted (-score, id).
  ``global`` is not listed (it matches generic phrases like "global market").
  Ancestors are not added; relationships handle containment.
* **Entities**: gazetteer mentions in title + meta description + body text,
  sorted (-mentions, id).
* **Page type / intent / commercial importance**: the first page-type rule
  matching ``urlsplit(url).path``, else the policy's default.
* **Keywords**: tokens from :func:`~ken_services.intelligence.text.tokens`,
  scored with the same location weights (uncapped), top :data:`MAX_KEYWORDS`
  by (-score, token).
* **Summary**: leading body sentences, skipping paragraphs that are headings,
  concatenated up to :data:`SUMMARY_MAX_CHARS` and cut at a sentence boundary;
  a first sentence longer than that is cut at a word boundary and ends in "…".
* **Salient blocks**: every non-heading block of
  :func:`~ken_services.inventory.normalize.extract_blocks` with at least
  :data:`MIN_SALIENT_BLOCK_WORDS` words is scored
  ``(market mentions in block + block tokens that are page keywords) /
  sqrt(block word count)``, rounded to 6 places; the top
  :data:`MAX_SALIENT_BLOCKS` with score > 0, by (-score, block order).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID

from ken_contracts.intelligence import (
    EntityMention,
    GeographyMention,
    PageAnalysis,
    SalientBlock,
    TaxonomyPolicy,
    TopicScore,
    analysis_version,
)
from ken_services.intelligence.taxonomy import TaxonomyIndex, taxonomy_index
from ken_services.intelligence.text import PhraseMatcher, fold, tokens, truncate_words
from ken_services.inventory.normalize import HEADING_LEVELS, extract_blocks

__all__ = [
    "BODY_MENTION_CAP",
    "BODY_WEIGHT",
    "HEADING_WEIGHT",
    "MARKET_CONFIDENCE_THRESHOLD",
    "META_WEIGHT",
    "TITLE_WEIGHT",
    "AnalysisInput",
    "analyze_page",
]

TITLE_WEIGHT: Final = 5.0
HEADING_WEIGHT: Final = 3.0
META_WEIGHT: Final = 2.0
BODY_WEIGHT: Final = 1.0
BODY_MENTION_CAP: Final = 10
"""Body mentions counted per node (topics and geographies)."""
MARKET_CONFIDENCE_THRESHOLD: Final = 3.0
"""Minimum topic score for ``market_id``: e.g. one heading mention, or 3 body mentions."""
MAX_TOPICS: Final = 10
MAX_GEOGRAPHIES: Final = 20
MAX_ENTITIES: Final = 20
MAX_KEYWORDS: Final = 15
SUMMARY_MAX_CHARS: Final = 600
MAX_SALIENT_BLOCKS: Final = 3
SALIENT_TEXT_MAX: Final = 600
MIN_SALIENT_BLOCK_WORDS: Final = 5
"""Shorter blocks (breadcrumbs, buttons, list stubs) are never salient."""
SALIENT_SCORE_DIGITS: Final = 6

_SENTENCE_END_RE: Final = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True, slots=True)
class AnalysisInput:
    """The stored facts of one page version that analysis reads."""

    page_id: UUID
    page_version: int
    url: str
    title: str | None
    meta_description: str | None
    headings: tuple[tuple[int, str], ...]
    """``(level, text)`` in document order."""
    body_html: str
    body_text: str
    metadata: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _Fields:
    """The folded text of each scored location."""

    title: str
    meta: str
    headings: tuple[str, ...]
    body: str


def _fields(item: AnalysisInput) -> _Fields:
    return _Fields(
        title=fold(item.title or ""),
        meta=fold(item.meta_description or ""),
        headings=tuple(fold(text) for _level, text in item.headings),
        body=fold(item.body_text),
    )


def _counts(matcher: PhraseMatcher, folded: str) -> Counter[str]:
    return Counter(match.node_id for match in matcher.matches(folded))


def _weighted_scores(matcher: PhraseMatcher, fields: _Fields) -> dict[str, float]:
    scores: dict[str, float] = {}

    def add(counts: Mapping[str, int], weight: float, cap: int | None = None) -> None:
        for node_id, count in counts.items():
            effective = count if cap is None else min(count, cap)
            scores[node_id] = scores.get(node_id, 0.0) + weight * effective

    add(_counts(matcher, fields.title), TITLE_WEIGHT)
    for heading in fields.headings:
        add(_counts(matcher, heading), HEADING_WEIGHT)
    add(_counts(matcher, fields.meta), META_WEIGHT)
    add(_counts(matcher, fields.body), BODY_WEIGHT, BODY_MENTION_CAP)
    return scores


# --------------------------------------------------------------------------- #
# Markets
# --------------------------------------------------------------------------- #
def _topics(index: TaxonomyIndex, scores: Mapping[str, float]) -> list[TopicScore]:
    ranked = sorted(
        (node_id for node_id, score in scores.items() if score > 0),
        key=lambda node_id: (-scores[node_id], index.market_depth(node_id), node_id),
    )
    return [
        TopicScore(
            node_id=node_id,
            label=index.market(node_id).label,
            score=scores[node_id],
            depth=index.market_depth(node_id),
        )
        for node_id in ranked
    ]


def _market_and_industry(
    index: TaxonomyIndex, scores: Mapping[str, float], topics: list[TopicScore]
) -> tuple[str | None, str | None]:
    confident = [n for n, s in scores.items() if s >= MARKET_CONFIDENCE_THRESHOLD]
    if confident:
        market_id = min(confident, key=lambda n: (-scores[n], -index.market_depth(n), n))
        return market_id, index.market_root(market_id)
    roots = [n for n, s in scores.items() if s > 0 and index.market_depth(n) == 0]
    if roots:
        return None, min(roots, key=lambda n: (-scores[n], n))
    if topics:
        return None, index.market_root(topics[0].node_id)
    return None, None


# --------------------------------------------------------------------------- #
# Geographies and entities
# --------------------------------------------------------------------------- #
def _declared_global(
    index: TaxonomyIndex, taxonomy: TaxonomyPolicy, item: AnalysisInput, fields: _Fields
) -> str | None:
    """The global node's id when the page DECLARES itself global, else ``None``.

    A page is global because its own identity says so -- "Global Methanol Market",
    ``/global-oat-milk-market`` -- not because the body happens to use the word. Only the
    title and the URL slug are read, and the body deliberately is not: "global demand rose"
    must not turn a Germany report into a worldwide one.

    Why this exists: :func:`_geographies` lists only ``region`` and ``country`` nodes, so a
    global page carried no global mention at all, and its primary geography became whichever
    place it discussed most. Measured on the 8,858-page inventory, **1,103 pages whose slug
    says "global" were labelled with a specific place** -- ``/global-oat-milk-market`` as
    Asia Pacific, ``/global-trade-management-market`` as North America. That made a page
    link to a regional report under its own name ("The Global [[Methanol Market]]" to an
    Asia Pacific report) and mislabelled global reports inside related-report modules.
    """
    node = next((g for g in taxonomy.geographies if g.kind == "global"), None)
    if node is None:  # a taxonomy need not declare one
        return None
    slug = fold(_collapse_slug(_path_of(item.url)))
    for text in (slug, fields.title):
        qualifier = _leading_geography(index, text)
        if qualifier is not None:
            return node.id if qualifier == node.id else None
    return None


def _leading_geography(index: TaxonomyIndex, folded: str) -> str | None:
    """The FIRST geography named in ``folded``, or ``None`` if it names none.

    Ken's identities put the geography qualifier first -- "/global-oat-milk-market",
    "/india-ev-market", "Global Methanol Market" -- so the earliest mention is the one the
    page is about and any later one is detail. Presence alone is not enough: measured on the
    corpus, a bare "does it say global" test moved 11 pages that also name a specific place,
    and about eight of those were wrong in the opposite direction -- "India Coffee Exports
    Competitive Gap Analysis" and "APAC Biosimilars Market, Global Biopharma" are an India
    page and an APAC page whose titles merely mention the world.

    The slug is consulted before the title because it is the stable identity; the title is
    editorial and may open with a hook rather than a place.
    """
    matches = sorted(index.geography_matcher.matches(folded), key=lambda m: (m.start, m.end))
    return matches[0].node_id if matches else None


def _collapse_slug(path: str) -> str:
    """A URL path as words, so phrase matching can read it ("/global-oat-milk-market" ->
    "global oat milk market")."""
    return " ".join(part for part in re.split(r"[/\-_]+", path) if part)


def _geographies(
    index: TaxonomyIndex, scores: Mapping[str, float], *, declared_global: str | None = None
) -> list[GeographyMention]:
    listed = [
        geo_id
        for geo_id, score in scores.items()
        if score >= 1 and index.geography(geo_id).kind in {"region", "country"}
    ]
    listed.sort(key=lambda geo_id: (-scores[geo_id], geo_id))
    out: list[GeographyMention] = []
    if declared_global is not None:
        # First, and scored above every specific place, so that `primary_geography` reads
        # this page as global however much it discusses one country.
        node = index.geography(declared_global)
        top = max(scores.values(), default=0.0)
        out.append(
            GeographyMention(geo_id=node.id, label=node.label, kind=node.kind, score=top + 1.0)
        )
    for geo_id in listed[: MAX_GEOGRAPHIES - len(out)]:
        node = index.geography(geo_id)
        out.append(
            GeographyMention(geo_id=geo_id, label=node.label, kind=node.kind, score=scores[geo_id])
        )
    return out


def _entities(index: TaxonomyIndex, fields: _Fields) -> list[EntityMention]:
    counts: Counter[str] = Counter()
    for folded in (fields.title, fields.meta, fields.body):
        counts.update(_counts(index.entity_matcher, folded))
    ranked = sorted(counts, key=lambda entity_id: (-counts[entity_id], entity_id))
    out: list[EntityMention] = []
    for entity_id in ranked[:MAX_ENTITIES]:
        entry = index.entity(entity_id)
        out.append(
            EntityMention(
                entity_id=entity_id, label=entry.label, type=entry.type, mentions=counts[entity_id]
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Keywords, summary, salient blocks
# --------------------------------------------------------------------------- #
def _keywords(item: AnalysisInput) -> tuple[str, ...]:
    scores: dict[str, float] = {}

    def add(words: Iterable[str], weight: float) -> None:
        for word in words:
            scores[word] = scores.get(word, 0.0) + weight

    add(tokens(item.title or ""), TITLE_WEIGHT)
    for _level, text in item.headings:
        add(tokens(text), HEADING_WEIGHT)
    add(tokens(item.meta_description or ""), META_WEIGHT)
    add(tokens(item.body_text), BODY_WEIGHT)
    ranked = sorted(scores, key=lambda word: (-scores[word], word))
    return tuple(ranked[:MAX_KEYWORDS])


def _summary(item: AnalysisInput) -> str:
    heading_texts = {" ".join(text.split()) for _level, text in item.headings}
    sentences: list[str] = []
    for line in item.body_text.split("\n"):
        paragraph = " ".join(line.split())
        if not paragraph or paragraph in heading_texts:
            continue
        sentences.extend(s for s in _SENTENCE_END_RE.split(paragraph) if s)
    summary = ""
    for sentence in sentences:
        candidate = f"{summary} {sentence}" if summary else sentence
        if len(candidate) <= SUMMARY_MAX_CHARS:
            summary = candidate
            continue
        if not summary:
            summary = truncate_words(sentence, SUMMARY_MAX_CHARS)
        break
    return summary


def _salient_blocks(
    index: TaxonomyIndex, body_html: str, keywords: tuple[str, ...]
) -> list[SalientBlock]:
    keyword_set = frozenset(keywords)
    scored: list[tuple[float, int, str, str]] = []
    for position, block in enumerate(extract_blocks(body_html)):
        if block.kind in HEADING_LEVELS:
            continue
        words = len(block.text.split())
        if words < MIN_SALIENT_BLOCK_WORDS:
            continue
        topic_hits = len(index.market_matcher.matches(fold(block.text)))
        keyword_hits = sum(1 for token in tokens(block.text) if token in keyword_set)
        score = round((topic_hits + keyword_hits) / math.sqrt(words), SALIENT_SCORE_DIGITS)
        if score > 0:
            scored.append((score, position, block.block_id, block.text))
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return [
        SalientBlock(block_id=block_id, score=score, text=truncate_words(text, SALIENT_TEXT_MAX))
        for score, _position, block_id, text in scored[:MAX_SALIENT_BLOCKS]
    ]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _path_of(url: str) -> str:
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        return "/"


def analyze_page(item: AnalysisInput, *, taxonomy: TaxonomyPolicy) -> PageAnalysis:
    """Analyse one page version. Pure and deterministic."""
    index = taxonomy_index(taxonomy)
    fields = _fields(item)

    market_scores = _weighted_scores(index.market_matcher, fields)
    all_topics = _topics(index, market_scores)
    market_id, industry_id = _market_and_industry(index, market_scores, all_topics)
    geo_scores = _weighted_scores(index.geography_matcher, fields)
    keywords = _keywords(item)
    rule = index.classify_path(_path_of(item.url))

    return PageAnalysis(
        page_id=item.page_id,
        page_version=item.page_version,
        analysis_version=analysis_version(taxonomy.version),
        page_type=rule.id,
        intent=rule.intent,
        commercial_importance=rule.commercial_importance,
        industry_id=industry_id,
        market_id=market_id,
        topics=tuple(all_topics[:MAX_TOPICS]),
        geographies=tuple(
            _geographies(
                index,
                geo_scores,
                declared_global=_declared_global(index, taxonomy, item, fields),
            )
        ),
        entities=tuple(_entities(index, fields)),
        keywords=keywords,
        summary=_summary(item),
        salient_blocks=tuple(_salient_blocks(index, item.body_html, keywords)),
    )
