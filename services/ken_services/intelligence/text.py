"""Shared text primitives for deterministic analysis (ADR-0010 section 3).

Everything here is pure and deterministic:

* :func:`fold` -- the single normal form every matcher compares in: Unicode NFC,
  then ``str.casefold`` (case-insensitive), then whitespace collapsed.
* :class:`PhraseMatcher` -- whole-word / whole-phrase alias matching over a
  vocabulary (one per taxonomy tree), longest match first, non-overlapping.
* :func:`tokens` / :data:`STOPWORDS` -- the keyword tokeniser.
* :func:`truncate_words` -- cut text at a word boundary within a length limit.

Word boundaries are Unicode ``\\w`` boundaries: an alias never matches inside a
longer word (``evs`` does not match in ``devs``). A hyphen is a separator, so
``india`` matches in ``India-based``; but hyphen and space are NOT
interchangeable inside an alias, and neither is "no separator": ``e-commerce``,
``e commerce`` and ``ecommerce`` match each other only when the policy declares
each spelling as an alias.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

__all__ = [
    "ELLIPSIS",
    "STOPWORDS",
    "PhraseMatch",
    "PhraseMatcher",
    "fold",
    "tokens",
    "truncate_words",
]

ELLIPSIS: Final = "…"

_WS_RE: Final = re.compile(r"\s+")
_TOKEN_RE: Final = re.compile(r"[^\W_]+")


def fold(text: str) -> str:
    """NFC, case-folded, whitespace-collapsed: the comparison form of all matching."""
    return _WS_RE.sub(" ", unicodedata.normalize("NFC", text).casefold()).strip()


# --------------------------------------------------------------------------- #
# Phrase matching
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PhraseMatch:
    """One accepted mention: ``folded_text[start:end]`` is an alias of ``node_id``."""

    start: int
    end: int
    node_id: str


@dataclass(frozen=True, slots=True)
class _Alias:
    phrase: str
    """Folded alias; used as a cheap substring pre-filter."""
    pattern: re.Pattern[str]
    node_id: str


def _alias_pattern(phrase: str) -> re.Pattern[str]:
    body = r"\s+".join(re.escape(part) for part in phrase.split(" "))
    return re.compile(rf"(?<!\w){body}(?!\w)")


class PhraseMatcher:
    """Whole-phrase, case-insensitive matcher over one vocabulary.

    ``vocabulary`` maps node id -> phrases (label plus aliases). Matching:

    1. every occurrence of every alias at word boundaries is found;
    2. candidates are accepted longest first (then leftmost, then node id), and a
       candidate overlapping an accepted span is dropped -- so "electric
       two-wheelers" is one mention of the two-wheeler node and never also a
       mention of a shorter alias inside it.

    A phrase declared by two nodes is attributed to the lowest node id (the
    shipped policy declares none; a test checks that).
    """

    __slots__ = ("_aliases",)

    def __init__(self, vocabulary: Mapping[str, Iterable[str]]) -> None:
        owner: dict[str, str] = {}
        for node_id in sorted(vocabulary):
            for raw in vocabulary[node_id]:
                phrase = fold(raw)
                if phrase and phrase not in owner:
                    owner[phrase] = node_id
        self._aliases: tuple[_Alias, ...] = tuple(
            _Alias(phrase=phrase, pattern=_alias_pattern(phrase), node_id=node_id)
            for phrase, node_id in sorted(owner.items())
        )

    def matches(self, folded: str) -> tuple[PhraseMatch, ...]:
        """Accepted mentions in ``folded`` (already :func:`fold`-ed), in text order."""
        if not folded:
            return ()
        candidates: list[PhraseMatch] = []
        for alias in self._aliases:
            if alias.phrase not in folded:
                continue
            candidates.extend(
                PhraseMatch(start=m.start(), end=m.end(), node_id=alias.node_id)
                for m in alias.pattern.finditer(folded)
            )
        candidates.sort(key=lambda c: (c.start - c.end, c.start, c.node_id))
        taken = bytearray(len(folded))
        accepted: list[PhraseMatch] = []
        for cand in candidates:
            if any(taken[cand.start : cand.end]):
                continue
            taken[cand.start : cand.end] = b"\x01" * (cand.end - cand.start)
            accepted.append(cand)
        accepted.sort(key=lambda c: c.start)
        return tuple(accepted)

    def count(self, text: str) -> Counter[str]:
        """Mentions per node id in ``text`` (folded here)."""
        return Counter(match.node_id for match in self.matches(fold(text)))


# --------------------------------------------------------------------------- #
# Keyword tokens
# --------------------------------------------------------------------------- #
STOPWORDS: Final = frozenset(
    """\
    a about above after again against all almost also although always am among an and
    another any anyone anything are around as at back be became because become becomes
    been before being below between both but by can cannot could did do does doing done
    down during each either else enough etc even ever every few for from further get gets
    given gives go going got had has have having he her here hers herself him himself his
    how however i if in including into is it its itself just least less like made make
    makes many may me might more most much must my myself near need needs neither never
    new no nor not now of off often on once one only onto or other others otherwise our
    ours ourselves out over own per perhaps please put rather really same see seen several
    shall she should since so some something still such than that the their theirs them
    themselves then there therefore these they this those though through throughout thus
    to together too toward towards under until up upon us use used uses using very via
    was we well were what whatever when where whereas whether which while who whom whose
    why will with within without would yet you your yours yourself yourselves
    """.split()  # noqa: SIM905 - a word block is easier to review than a 200-item list
)
"""Fixed English stopword list (built in, so keywords never depend on a download)."""

MIN_TOKEN_CHARS: Final = 3


def tokens(text: str) -> list[str]:
    """Keyword tokens of ``text`` in text order.

    Folded runs of letters/digits (underscore and punctuation separate tokens)
    of at least :data:`MIN_TOKEN_CHARS` characters, minus :data:`STOPWORDS` and
    purely numeric tokens (years, figures).
    """
    return [
        token
        for token in _TOKEN_RE.findall(fold(text))
        if len(token) >= MIN_TOKEN_CHARS and token not in STOPWORDS and not token.isdigit()
    ]


# --------------------------------------------------------------------------- #
# Truncation
# --------------------------------------------------------------------------- #
def truncate_words(text: str, limit: int, *, ellipsis: bool = True) -> str:
    """``text`` cut to at most ``limit`` characters, never mid-word.

    When cut, the result ends at the last whitespace before the limit (with
    :data:`ELLIPSIS` appended inside the limit if ``ellipsis``). A single word
    longer than the limit is hard-cut, the only case that splits a word.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    budget = limit - 1 if ellipsis else limit
    if budget <= 0:
        return ELLIPSIS[:limit] if ellipsis else ""
    head = text[: budget + 1]
    cut = max(head.rfind(" "), head.rfind("\n"))
    kept = text[:budget].rstrip() if cut <= 0 else head[:cut].rstrip()
    return kept + ELLIPSIS if ellipsis else kept
