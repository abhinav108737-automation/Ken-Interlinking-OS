"""Deterministic market-statistic extraction with verbatim provenance (ADR-0023 decision 2).

ADR-0023 lets the system propose a short contextual sentence beside an internal link, but
decision 2 forbids it inventing anything: *every* number in that sentence must come from an
**evidence span** -- a verbatim quotation of the target page, stored with its offsets. This
module is the layer that produces those spans. It is the whole of the "no new facts" rule in
code, and nothing else in the copy pipeline may introduce a number.

**No model is involved and none may be.** ADR-0023 decision 3 ("deterministic checks decide; a
model only drafts") and CLAUDE.md constraint 8 both point the same way: finding a figure in text
is a parsing problem, so it is parsed. :func:`extract_page_evidence` is pure -- no clock, no
network, no randomness, no database -- so the same ``body_text`` always yields the same facts and
a reviewer can re-derive any claim from the stored offsets.

What it returns, all optional (:class:`PageEvidence`):

``market_value``      the page's own market size in its base year;
``forecast_value``    the same market at the end of its own forecast period;
``forecast_cagr``     the growth rate the page forecasts for its own market;
``historical_cagr``   the rate the page reports for its own history -- returned **separately**,
                      never as the forecast, because the corpus states both in one sentence
                      ("forecast CAGR is 5.21% for 2025-2032, compared with a modeled historical
                      CAGR of 4.94%") and conflating them would publish a false claim.

Design rules, in the order they matter:

1. **Refuse rather than guess.** Returning fewer facts costs a sentence; returning a wrong one is
   an editorial failure published under Ken Research's name. Every ambiguity resolves to ``None``,
   with the reason recorded in :attr:`PageEvidence.refusals` so the behaviour is auditable instead
   of merely silent.
2. **Only the page's own market.** These pages quote neighbouring markets, competitors,
   per-country rows and a site-wide promotional strip of *other* markets' CAGRs. A figure drawn
   from prose is therefore accepted only from a sentence that names this page's own market, and a
   "sentence" never crosses a blank line, which is what separates the promotional strip's market
   names from its percentages.
3. **Agreement or nothing.** Where a figure is stated twice -- once in the page's summary card and
   once in its outlook prose -- both must agree. A disagreement refuses the fact; it is a sign the
   page itself is inconsistent, which is precisely when a machine should not choose.
4. **Arithmetic is a check, not a source.** When a value, a forecast value and a forecast CAGR are
   all found, the CAGR implied by the two endpoints must match the stated one within
   :data:`CAGR_TOLERANCE_POINTS`. This catches a figure paired with the wrong year or the wrong
   market far more reliably than any pattern.

Two observed properties of this corpus are encoded deliberately:

* The summary card's projection tile is labelled ``2030 Projection`` on pages whose declared
  forecast period is ``2025-2032`` or ``2026-2031``. **The card's projection year is never read.**
  A forecast year comes from the prose, cross-checked against the declared forecast period.
* A page may state a volume CAGR, a price-and-mix CAGR or a per-segment CAGR beside the market
  one. A prose CAGR carrying any such qualifier is discarded, never reinterpreted.
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal

from pydantic import Field, model_validator

from ken_contracts.common import FrozenModel

__all__ = [
    "CAGR_TOLERANCE_POINTS",
    "CagrFact",
    "CagrKind",
    "EvidenceSource",
    "EvidenceSpan",
    "MoneyFact",
    "MoneyUnit",
    "PageEvidence",
    "Refusal",
    "RefusalCode",
    "extract_page_evidence",
]

CAGR_TOLERANCE_POINTS: Final = Decimal("0.75")
"""How far a stated CAGR may sit from the one implied by the two endpoint values, in percentage
points, before every forecast fact is refused. Published figures are rounded independently (a
value to the nearest million, a rate to two decimals), so an exact match is not expected; 0.75pp
is wide enough for that rounding and narrow enough to catch a figure paired with the wrong year.
A starting value, like ADR-0023's own thresholds."""

_QUALIFIER_WINDOW: Final = 45
"""Characters either side of a prose ``CAGR`` token that are read for its percentage, its period
and its qualifiers. Wide enough to reach across "forecast CAGR of 4.73%", narrow enough that the
next clause's rate is not pulled in."""


class EvidenceSource(StrEnum):
    """Where a span was read from. Recorded so a reviewer knows which part of the page to look at
    and so the summary card and the prose can be told apart when they disagree."""

    SUMMARY_CARD = "summary_card"
    """A labelled tile of the page's own summary card ("Market Value", "Forecast CAGR")."""
    OUTLOOK_PROSE = "outlook_prose"
    """A sentence that names the page's own market."""


class MoneyUnit(StrEnum):
    MILLION = "million"
    BILLION = "billion"
    TRILLION = "trillion"


class CagrKind(StrEnum):
    FORECAST = "forecast"
    HISTORICAL = "historical"


class RefusalCode(StrEnum):
    """Why a candidate fact was dropped. Not errors: each one is the module working as intended,
    and the set is what makes "it extracted nothing" reviewable."""

    NO_SUMMARY_CARD = "no_summary_card"
    """The page has no labelled summary card, so no figure has a label to anchor it."""
    AMBIGUOUS_LABEL = "ambiguous_label"
    """A summary-card label occurs more than once: which tile is the page's own is unknown."""
    NO_SELF_REFERENCE = "no_self_reference"
    """The page's own market name could not be determined, so no prose figure can be attributed."""
    NO_OUTLOOK_SENTENCE = "no_outlook_sentence"
    """No sentence names this page's market and projects it, so there is no prose to read."""
    MIXED_CURRENCY = "mixed_currency"
    """More than one currency in one figure ("AUD 143 million; USD 92 million")."""
    AMBIGUOUS_AMOUNT = "ambiguous_amount"
    """A range, a missing magnitude unit, or several amounts where one was expected."""
    BARE_DOLLAR_SIGN = "bare_dollar_sign"
    """``$`` with no ``USD`` anywhere on the page: the currency is a guess, so it is refused."""
    UNKNOWN_YEAR = "unknown_year"
    """No year could be attached to a figure without inferring one."""
    AMBIGUOUS_FORECAST_YEAR = "ambiguous_forecast_year"
    """The prose states no value at the end of the declared forecast period, or states two."""
    VALUE_DISAGREEMENT = "value_disagreement"
    """The card and the prose state different base-year values."""
    CAGR_DISAGREEMENT = "cagr_disagreement"
    """The card and the prose state different rates for the same kind of CAGR."""
    CAGR_INCONSISTENT_WITH_VALUES = "cagr_inconsistent_with_values"
    """The stated CAGR does not follow from the endpoint values (:data:`CAGR_TOLERANCE_POINTS`)."""
    NO_PERIOD = "no_period"
    """A CAGR with no period: a rate without the years it covers is not a publishable fact."""


class EvidenceSpan(FrozenModel):
    """A verbatim quotation of the supplied text, with the offsets that produced it.

    The invariant the whole design rests on: ``body_text[start:end] == text``. It is checked on
    construction, so a span that does not slice back cannot exist.
    """

    text: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    source: EvidenceSource

    @model_validator(mode="after")
    def _offsets_match_length(self) -> EvidenceSpan:
        if self.end - self.start != len(self.text):
            raise ValueError("span offsets do not cover the span text")
        return self


class MoneyFact(FrozenModel):
    """A market size: currency, amount, magnitude and the year it refers to."""

    currency: str = Field(pattern=r"^[A-Z]{3}$")
    """ISO-4217-style code. ``$`` and ``US$`` normalise to ``USD``, and only when the page also
    writes ``USD`` somewhere -- otherwise the figure is refused rather than assumed."""
    amount: Decimal = Field(gt=0)
    """The amount as written, in the stated ``unit``: ``16500`` for "USD 16,500 Mn"."""
    unit: MoneyUnit
    amount_in_millions: Decimal = Field(gt=0)
    """``amount`` scaled to millions, so two figures written in different magnitudes compare."""
    year: int = Field(ge=1900, le=2100)
    approximate: bool = False
    """The page itself hedged the figure ("approximately", "about", "~"). Copy built from an
    approximate figure must hedge it too."""
    evidence: tuple[EvidenceSpan, ...] = Field(min_length=1)
    """Every span that supports this fact, the first being the primary one a reviewer is shown."""


class CagrFact(FrozenModel):
    """A compound annual growth rate and the period it covers."""

    percent: Decimal = Field(gt=0, le=200)
    kind: CagrKind
    period_start: int = Field(ge=1900, le=2100)
    period_end: int = Field(ge=1900, le=2100)
    approximate: bool = False
    evidence: tuple[EvidenceSpan, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _period_runs_forward(self) -> CagrFact:
        if self.period_end <= self.period_start:
            raise ValueError("CAGR period must end after it starts")
        return self


class Refusal(FrozenModel):
    """One fact that was *not* returned, and why."""

    code: RefusalCode
    field: Literal["market_value", "forecast_value", "forecast_cagr", "historical_cagr"]
    detail: str = ""
    """Short, non-authoritative context for a reviewer. Never a figure to publish."""


class PageEvidence(FrozenModel):
    """Everything this page states about its own market, or nothing, and the reasons."""

    market_value: MoneyFact | None = None
    forecast_value: MoneyFact | None = None
    forecast_cagr: CagrFact | None = None
    historical_cagr: CagrFact | None = None
    market_name: str | None = None
    """The page's own market as the page names it.

    Diagnostic, for attributing prose figures and for a reviewer to see what the extractor took
    the page to be about. **Not copy**: it is derived text, not a verbatim span, so it carries no
    offsets and must never be dropped into a published sentence. A sentence names the market only
    by quoting an evidence span."""
    refusals: tuple[Refusal, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any(
            (self.market_value, self.forecast_value, self.forecast_cagr, self.historical_cagr)
        )

    def spans(self) -> tuple[EvidenceSpan, ...]:
        """Every span behind every returned fact, for storage alongside a copy proposal."""
        facts: tuple[MoneyFact | CagrFact | None, ...] = (
            self.market_value,
            self.forecast_value,
            self.forecast_cagr,
            self.historical_cagr,
        )
        return tuple(span for fact in facts if fact is not None for span in fact.evidence)


# --------------------------------------------------------------------------- #
# Lexicon
#
# Spelling in this corpus is irregular: a dash may be a hyphen or an en dash, a
# magnitude may be "Mn", "million" or a bare "B", and whitespace between tokens
# is sometimes missing entirely ("at18.8% CAGR", "a CAGR of~30%"). The patterns
# tolerate that, but tolerate nothing about *meaning*.
# --------------------------------------------------------------------------- #
_DASH_CODEPOINTS: Final = (0x2D, 0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)
"""Hyphen-minus and the typographic dashes. Built from codepoints rather than written out: these
glyphs are indistinguishable in source, and a period parsed with the wrong dash silently fails."""
_DASH: Final = f"[{re.escape(''.join(chr(code) for code in _DASH_CODEPOINTS))}]"
_APOSTROPHES: Final = re.escape("'" + chr(0x2019))
_YEAR: Final = r"(?:19|20)\d{2}"
_AMOUNT: Final = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"

_UNIT_BY_TOKEN: Final = {
    "mn": MoneyUnit.MILLION,
    "m": MoneyUnit.MILLION,
    "million": MoneyUnit.MILLION,
    "bn": MoneyUnit.BILLION,
    "b": MoneyUnit.BILLION,
    "billion": MoneyUnit.BILLION,
    "tn": MoneyUnit.TRILLION,
    "t": MoneyUnit.TRILLION,
    "trillion": MoneyUnit.TRILLION,
}
_SCALE_IN_MILLIONS: Final = {
    MoneyUnit.MILLION: Decimal(1),
    MoneyUnit.BILLION: Decimal(1_000),
    MoneyUnit.TRILLION: Decimal(1_000_000),
}
_UNIT_PATTERN: Final = "|".join(sorted(_UNIT_BY_TOKEN, key=len, reverse=True))
_CURRENCY_PATTERN: Final = r"US\$|U\.S\.\$|USD|AUD|CAD|EUR|GBP|INR|JPY|CNY|SGD|AED|SAR|ZAR|\$"
_CURRENCY_ALIASES: Final = {"US$": "USD", "U.S.$": "USD", "$": "USD"}
"""``US$`` names its currency unambiguously. A bare ``$`` does not -- it could be Australian or
Canadian -- so it is only read as USD on a page that also writes ``USD``, and refused otherwise."""

_MONEY_RE: Final = re.compile(
    rf"(?P<currency>{_CURRENCY_PATTERN})\s*~?\s*(?P<amount>{_AMOUNT})\s*(?P<unit>{_UNIT_PATTERN})\b",
    re.IGNORECASE,
)
"""A currency, an amount and a magnitude unit. The unit is **required**: "USD 108.09" (a price per
kilogram in this corpus) is not a market size and must not be read as one."""

_MONEY_RANGE_RE: Final = re.compile(
    rf"(?P<currency>{_CURRENCY_PATTERN})\s*~?\s*(?:{_AMOUNT})\s*(?:{_UNIT_PATTERN})?\s*{_DASH}\s*"
    rf"~?\s*(?:{_AMOUNT})\s*(?:{_UNIT_PATTERN})\b",
    re.IGNORECASE,
)
"""A range ("USD 2,000-3,000 million"). Detected only so it can be refused."""

_PERIOD_RE: Final = re.compile(rf"(?P<start>{_YEAR})\s*{_DASH}\s*(?P<end>{_YEAR})")
_PERCENT_RE: Final = re.compile(r"(?P<approx>~\s*)?(?P<pct>\d{1,3}(?:\.\d+)?)\s*%")
_YEAR_RE: Final = re.compile(rf"\b{_YEAR}\b")
_APPROX_RE: Final = re.compile(
    r"approximat|approx\.|about|around|roughly|nearly|circa|~|estimat", re.IGNORECASE
)
"""Stems, not whole words: the corpus hedges with "approximately", "an estimate", "estimated" and
a bare "~", and a figure the page hedged must not be published unhedged."""

_HEDGE_LOOKBACK: Final = 20
"""How far before a figure a hedge is still taken to be its own. Short on purpose: at 28
characters "to about USD 54 Mn by 2031 and USD 65 Mn by 2032" marked the *second* figure
approximate, because the first figure's hedge was still in view."""

_NAME_BODY: Final = rf"[A-Z][\w{_APOSTROPHES}&().,\- ]{{2,70}}?\s+Market"

_MARKET_NAME_RE: Final = re.compile(rf"\bThe\s+(?P<name>{_NAME_BODY})\b")
"""A self-referential market name as this corpus writes it: "The India Cosmetics Market", "The
Global Men's Golf Clubs Market". Capitalised, because a page's own market is a proper noun here;
a lower-case "the cosmetics market in India" is not matched and the page yields nothing."""

_SENTENCE_INITIAL_MARKET_RE: Final = re.compile(
    rf"^(?:The\s+)?(?P<name>[A-Z][A-Za-z{_APOSTROPHES}&()\- ]{{2,70}}?\s+Market)\s+"
    r"(?:is|was|will|has|had)\s"
)
"""The same name opening a sentence with no article, which some pages do ("United States Hydrogen
Compressors Market is projected to expand from ..."). A following copula is required so that the
match is a grammatical subject rather than any capitalised run that happens to end in "Market",
and neither a comma nor a digit may appear in the name: without that, "Through 2031, the Portugal
Seed Market is estimated at ..." yields the name "Through 2031, the Portugal Seed Market"."""

_OWN_SUBJECT_RE: Final = re.compile(r"^(?:The market|The base case|The forecast|Its)\b")
"""Subjects that refer back to the page's own market rather than naming it. The only anaphora
this module resolves, and only in the bounded positions described in
:func:`_self_referential_sentences`."""

_OUTLOOK_LABEL: Final = "Future Outlook"
"""The labelled block in which the page states the trajectory of its own market. A structural
anchor, not a guess: whatever that block says, it says about this page's subject."""

_PROJECTION_WORDS: Final = (
    "project",
    "forecast",
    "modeled to",
    "modelled to",
    "advance",
    "expected to",
    "estimated to",
    "grow",
    "reach",
)
"""Words that mark a sentence as stating a trajectory. Deliberately broad: it is a cheap filter,
not a guarantee, and what is actually returned is decided by the declared forecast period, the
single-value requirement and the arithmetic cross-check."""

_FORECAST_MARKERS: Final = ("forecast", "project", "implie", "will reach", "expected", "resulting")
_HISTORICAL_MARKERS: Final = ("historical", "historic", "recorded", "past", "trailing", "averaged")
_CAGR_DISQUALIFIERS: Final = (
    "volume",
    "price",
    "pricing",
    "mix",
    "tonnage",
    "tonne",
    "metric ton",
    "per kg",
    "per kilogram",
    "segment",
    "subsegment",
    "category",
    "share",
    "margin",
    "penetration",
    "unit demand",
    "headcount",
    "subscriber",
    "seat",
)
"""Words that make a prose CAGR something other than this market's value CAGR. The corpus states
"a volume CAGR of 2.65%" and "a price-and-mix CAGR of 2.49%" in the same paragraph as the market
CAGR; a mention carrying any of these is discarded outright, never reinterpreted."""

_CARD_VALUE_LABEL: Final = "Market Value"
_CARD_FORECAST_CAGR_LABEL: Final = "Forecast CAGR"
_CARD_HISTORICAL_CAGR_LABEL: Final = "Historical CAGR"
_CARD_BASE_YEAR_LABEL: Final = "Base Year"
_CARD_FORECAST_PERIOD_LABEL: Final = "Forecast Period"
_CARD_HISTORICAL_PERIOD_LABEL: Final = "Historical Period"


# --------------------------------------------------------------------------- #
# Internal, offset-preserving helpers
# --------------------------------------------------------------------------- #
class _Blocks:
    """The body split into blank-line-separated blocks, with each block's offset kept.

    Blocks matter because this plain text is a flattened rendering of a structured page: a
    summary-card label and its value are separate blocks, and so are a promotional strip's market
    name and its CAGR. Treating a block boundary as a hard boundary is what stops another market's
    percentage being attached to a name beside it.
    """

    __slots__ = ("items",)

    def __init__(self, text: str) -> None:
        self.items: list[tuple[int, str]] = []
        for match in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]+)*", text):
            self.items.append((match.start(), match.group()))

    def labelled(self, label: str) -> tuple[int, int, str] | None:
        """The single block that is exactly ``label``, and the block after it.

        Returns ``(label_start, value_start, value_text)``, or ``None`` when the label is absent
        or occurs more than once -- a repeated label means the page has more than one card and
        which one describes this market is unknown.
        """
        hits = [i for i, (_, body) in enumerate(self.items) if body.strip() == label]
        if len(hits) != 1:
            return None
        index = hits[0]
        if index + 1 >= len(self.items):
            return None
        label_start, _ = self.items[index]
        value_start, value_text = self.items[index + 1]
        return label_start, value_start, value_text

    def following(self, index_start: int) -> tuple[int, str] | None:
        """The block that begins immediately after the block containing ``index_start``."""
        for position, (start, body) in enumerate(self.items):
            if start <= index_start < start + len(body):
                if position + 1 < len(self.items):
                    return self.items[position + 1]
                return None
        return None


def _sentences(text: str) -> list[tuple[int, str]]:
    """Sentences with their offsets. A sentence never crosses a blank line (see :class:`_Blocks`).

    Deliberately simple: a boundary is ``.``/``!``/``?`` followed by whitespace and a capital or a
    digit. It over-splits on abbreviations, which costs recall and never costs correctness, since
    an over-split sentence simply fails the self-reference test.
    """
    out: list[tuple[int, str]] = []
    for block_start, block in _Blocks(text).items:
        cursor = 0
        for boundary in re.finditer(r"(?<=[.!?])\s+(?=[A-Z0-9])", block):
            out.append((block_start + cursor, block[cursor : boundary.start()]))
            cursor = boundary.end()
        if cursor < len(block):
            out.append((block_start + cursor, block[cursor:]))
    return out


def _span(text: str, start: int, end: int, source: EvidenceSource) -> EvidenceSpan:
    return EvidenceSpan(text=text[start:end], start=start, end=end, source=source)


def _parse_amount(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def _normalise_currency(raw: str) -> str:
    token = raw.upper()
    return _CURRENCY_ALIASES.get(token, token)


def _is_approximate(text: str, start: int, end: int, hedge_end: int) -> bool:
    """Whether the page hedged the figure at ``text[start:end]``.

    Backwards, :data:`_HEDGE_LOOKBACK` characters: a hedge normally precedes its figure
    ("approximately USD 1,395 million", "USD ~ 4 Bn"), and reading further back picks up a hedge
    belonging to an earlier figure. Forwards only as far as ``hedge_end``, which a caller sets to
    the end of a self-contained fragment such as a summary-card tile: the tile "USD 40 million,
    Japan, 2025 estimate" hedges its own figure after it, and copy built from it must hedge it too.
    """
    window = text[max(0, start - _HEDGE_LOOKBACK) : max(end, hedge_end)]
    return _APPROX_RE.search(window) is not None


def _money_in(
    text: str,
    start: int,
    end: int,
    source: EvidenceSource,
    *,
    usd_is_safe: bool,
    hedge_end: int = 0,
) -> tuple[MoneyFact | None, RefusalCode | None]:
    """The single money figure in ``text[start:end]``, or a refusal code.

    No year is attached here: a year is the caller's business, because only the caller knows
    whether it is reading a base-year tile or a forecast clause.
    """
    fragment = text[start:end]
    if _MONEY_RANGE_RE.search(fragment):
        return None, RefusalCode.AMBIGUOUS_AMOUNT
    matches = list(_MONEY_RE.finditer(fragment))
    if not matches:
        return None, RefusalCode.AMBIGUOUS_AMOUNT
    currencies = {_normalise_currency(m.group("currency")) for m in matches}
    if len(currencies) > 1:
        return None, RefusalCode.MIXED_CURRENCY
    if len(matches) > 1:
        return None, RefusalCode.AMBIGUOUS_AMOUNT
    match = matches[0]
    if match.group("currency") == "$" and not usd_is_safe:
        return None, RefusalCode.BARE_DOLLAR_SIGN
    unit = _UNIT_BY_TOKEN[match.group("unit").lower()]
    amount = _parse_amount(match.group("amount"))
    absolute_start = start + match.start()
    absolute_end = start + match.end()
    fact = MoneyFact(
        currency=_normalise_currency(match.group("currency")),
        amount=amount,
        unit=unit,
        amount_in_millions=amount * _SCALE_IN_MILLIONS[unit],
        year=1900,  # replaced by the caller; never returned with this value
        approximate=_is_approximate(text, absolute_start, absolute_end, hedge_end),
        evidence=(_span(text, absolute_start, absolute_end, source),),
    )
    return fact, None


def _with_year_and_span(
    fact: MoneyFact, year: int, evidence: tuple[EvidenceSpan, ...]
) -> MoneyFact:
    return fact.model_copy(update={"year": year, "evidence": evidence})


def _same_money(left: MoneyFact, right: MoneyFact) -> bool:
    return left.currency == right.currency and left.amount_in_millions == right.amount_in_millions


# --------------------------------------------------------------------------- #
# The summary card
# --------------------------------------------------------------------------- #
class _Card(FrozenModel):
    """What the page's labelled summary card states. Every field is independently optional."""

    base_year: int | None = None
    forecast_period: tuple[int, int] | None = None
    historical_period: tuple[int, int] | None = None
    value: MoneyFact | None = None
    value_refusal: RefusalCode | None = None
    forecast_cagr: CagrFact | None = None
    historical_cagr: CagrFact | None = None
    present: bool = False


def _period_from_label(
    text: str, blocks: _Blocks, label: str
) -> tuple[tuple[int, int], EvidenceSpan] | None:
    found = blocks.labelled(label)
    if found is None:
        return None
    label_start, value_start, value_text = found
    match = _PERIOD_RE.search(value_text)
    if match is None:
        return None
    start = int(match.group("start"))
    end = int(match.group("end"))
    if end <= start:
        return None
    span = _span(text, label_start, value_start + len(value_text), EvidenceSource.SUMMARY_CARD)
    return (start, end), span


def _read_card(text: str, blocks: _Blocks, *, usd_is_safe: bool) -> _Card:
    """Read the labelled summary card. Its projection tile is deliberately not read."""
    forecast_period = _period_from_label(text, blocks, _CARD_FORECAST_PERIOD_LABEL)
    historical_period = _period_from_label(text, blocks, _CARD_HISTORICAL_PERIOD_LABEL)

    base_year: int | None = None
    base = blocks.labelled(_CARD_BASE_YEAR_LABEL)
    if base is not None:
        year_match = _YEAR_RE.fullmatch(base[2].strip())
        if year_match is not None:
            base_year = int(year_match.group())

    value: MoneyFact | None = None
    value_refusal: RefusalCode | None = None
    value_block = blocks.labelled(_CARD_VALUE_LABEL)
    if value_block is not None:
        label_start, value_start, value_text = value_block
        parsed, refusal = _money_in(
            text,
            value_start,
            value_start + len(value_text),
            EvidenceSource.SUMMARY_CARD,
            usd_is_safe=usd_is_safe,
            hedge_end=value_start + len(value_text),
        )
        if parsed is None:
            value_refusal = refusal
        else:
            # The year sits either inside the tile ("USD 2,851 million, Asia Pacific, 2025") or in
            # the block immediately after it ("USD 16,500 Mn" / "2025"). The declared base year is
            # the last resort, and only when neither carries one.
            card_end = value_start + len(value_text)
            inline = [int(m.group()) for m in _YEAR_RE.finditer(value_text)]
            year: int | None = None
            if len(set(inline)) == 1:
                year = inline[0]
            elif not inline:
                following = blocks.following(value_start)
                if following is not None:
                    standalone = _YEAR_RE.fullmatch(following[1].strip())
                    if standalone is not None:
                        year = int(standalone.group())
                        card_end = following[0] + len(following[1])
                if year is None:
                    year = base_year
            if year is None:
                value_refusal = RefusalCode.UNKNOWN_YEAR
            else:
                value = _with_year_and_span(
                    parsed,
                    year,
                    (_span(text, label_start, card_end, EvidenceSource.SUMMARY_CARD),),
                )

    forecast_cagr = _card_cagr(
        text,
        blocks,
        label=_CARD_FORECAST_CAGR_LABEL,
        kind=CagrKind.FORECAST,
        period=forecast_period,
        label_precedes_value=False,
    )
    historical_cagr = _card_cagr(
        text,
        blocks,
        label=_CARD_HISTORICAL_CAGR_LABEL,
        kind=CagrKind.HISTORICAL,
        period=historical_period,
        label_precedes_value=True,
    )
    return _Card(
        base_year=base_year,
        forecast_period=forecast_period[0] if forecast_period else None,
        historical_period=historical_period[0] if historical_period else None,
        value=value,
        value_refusal=value_refusal,
        forecast_cagr=forecast_cagr,
        historical_cagr=historical_cagr,
        present=value_block is not None,
    )


def _card_cagr(
    text: str,
    blocks: _Blocks,
    *,
    label: str,
    kind: CagrKind,
    period: tuple[tuple[int, int], EvidenceSpan] | None,
    label_precedes_value: bool,
) -> CagrFact | None:
    """One CAGR tile. The rate and its label are adjacent blocks, in either order depending on the
    tile's rendering, and the period comes from a *different* tile, so it is a second span."""
    if period is None:
        return None
    found = blocks.labelled(label)
    if found is None:
        return None
    label_start, value_start, value_text = found
    if label_precedes_value:
        rate_text = value_text
        span_start, span_end = label_start, value_start + len(value_text)
    else:
        # The rate is the block BEFORE the label; find it by walking the block list backwards.
        previous: tuple[int, str] | None = None
        for start, body in blocks.items:
            if start == label_start:
                break
            previous = (start, body)
        if previous is None:
            return None
        rate_text = previous[1]
        span_start, span_end = previous[0], label_start + len(label)
    match = _PERCENT_RE.fullmatch(rate_text.strip())
    if match is None:
        return None
    return CagrFact(
        percent=Decimal(match.group("pct")),
        kind=kind,
        period_start=period[0][0],
        period_end=period[0][1],
        approximate=match.group("approx") is not None,
        evidence=(
            _span(text, span_start, span_end, EvidenceSource.SUMMARY_CARD),
            period[1],
        ),
    )


# --------------------------------------------------------------------------- #
# Prose
# --------------------------------------------------------------------------- #
def _market_name(text: str, blocks: _Blocks) -> str | None:
    """The page's own market, as the page names it.

    Preference order, strongest first: the market projected in an outlook sentence; the market
    opening the "Market Overview" block; otherwise the most frequent capitalised market name, and
    only when it is named at least three times -- the promotional strip of other markets names
    each of them once.
    """
    for pattern in (_MARKET_NAME_RE, _SENTENCE_INITIAL_MARKET_RE):
        for _, sentence in _sentences(text):
            if any(word in sentence.lower() for word in _PROJECTION_WORDS):
                match = pattern.search(sentence.strip())
                if match is not None:
                    return " ".join(match.group("name").split())

    overview = blocks.labelled("Market Overview")
    if overview is not None:
        match = _MARKET_NAME_RE.match(overview[2].strip())
        if match is not None:
            return " ".join(match.group("name").split())

    counts: dict[str, int] = {}
    for match in _MARKET_NAME_RE.finditer(text):
        name = " ".join(match.group("name").split())
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return None
    best = max(sorted(counts), key=lambda name: counts[name])
    return best if counts[best] >= 3 else None


def _mentions_own_market(sentence: str, market_name: str) -> bool:
    """Whether a sentence names this page's market, in full.

    The full name only. A shortened second reference ("the Cosmetics Market") is deliberately not
    matched: the shorter the needle, the more readily it matches a sentence about a neighbouring
    market, and a missed sentence costs nothing but recall.
    """
    haystack = " ".join(sentence.split()).casefold()
    needle = " ".join(market_name.split()).casefold()
    return needle in haystack


def _names_another_market(sentence: str) -> bool:
    """Whether the sentence names a market by a proper name of its own."""
    stripped = sentence.strip()
    return (
        _MARKET_NAME_RE.search(stripped) is not None
        or _SENTENCE_INITIAL_MARKET_RE.search(stripped) is not None
    )


def _self_referential_sentences(
    text: str, blocks: _Blocks, market_name: str
) -> list[tuple[int, str]]:
    """Sentences that are about this page's own market.

    A sentence qualifies by naming the market in full. Two bounded anaphoric cases also qualify,
    and they are the **only** anaphora this module resolves. In both, the sentence must open with
    an :data:`_OWN_SUBJECT_RE` subject ("The market", "The base case", "The forecast", "Its") and
    must not name any market of its own, so a neighbouring market's figure cannot arrive this way:

    1. **Inside the page's own "Future Outlook" block.** A structural anchor: the corpus writes
       "The market advances from USD 9,960 million in 2025 to ..." and "The base case projects the
       market from USD 40 million in 2025 to ..." there, about the page's subject by construction.
    2. **Immediately after a sentence that named the market in full, in the same block.** The
       corpus splits one trajectory across that pair: "The SEA Digital Identity in Ride-Hailing
       Market is projected to sustain an 18.50% CAGR ... The market is worth USD 34 million in the
       2025 base year and is modeled to reach ... USD 113 million in 2032."

    Refusing both would cost roughly a sixth of all forecast values. Anything looser -- resolving
    "it", or any sentence in the block -- would not be bounded, and whatever these two yield must
    still survive the agreement and arithmetic checks in :func:`extract_page_evidence`.
    """
    own_block: tuple[int, int] | None = None
    labelled = blocks.labelled(_OUTLOOK_LABEL)
    if labelled is not None:
        own_block = (labelled[1], labelled[1] + len(labelled[2]))

    block_starts = {start for start, _ in blocks.items}
    out: list[tuple[int, str]] = []
    previous_is_self = False
    previous_block_start = -1
    for start, sentence in _sentences(text):
        block_start = max((b for b in block_starts if b <= start), default=-1)
        if _mentions_own_market(sentence, market_name):
            out.append((start, sentence))
            previous_is_self = True
        elif (
            _OWN_SUBJECT_RE.match(sentence.strip())
            and not _names_another_market(sentence)
            and (
                (own_block is not None and own_block[0] <= start < own_block[1])
                or (previous_is_self and block_start == previous_block_start)
            )
        ):
            out.append((start, sentence))
        else:
            previous_is_self = False
        previous_block_start = block_start
    return out


def _outlook_sentences(text: str, blocks: _Blocks, market_name: str) -> list[tuple[int, str]]:
    """Every sentence about this market that projects it and states a figure.

    All of them are read, not just the first: the corpus sometimes states one trajectory twice
    ("... expand from USD 730 Mn in 2025 to USD 1,981 Mn in 2031 ..." and "... reach USD 1,981 Mn
    by 2031"). Consistency is then required rather than assumed -- the caller refuses if the
    sentences disagree about the value at the declared forecast year.
    """
    return [
        (start, sentence)
        for start, sentence in _self_referential_sentences(text, blocks, market_name)
        if any(word in sentence.lower() for word in _PROJECTION_WORDS)
        and _MONEY_RE.search(sentence) is not None
    ]


def _money_year_pairs(
    text: str, sentence_start: int, sentence: str, *, usd_is_safe: bool
) -> list[tuple[MoneyFact, int, int, int]]:
    """Every ``(figure, year)`` pair the sentence states, with the span covering both.

    A pair is a money figure and the first year that follows it within 24 characters -- "USD
    16,500 Mn in 2025", "USD 29,586 Mn by 2032", "to USD 3,867 million in 2031". A figure with no
    year nearby yields no pair, and "From the 2025 base of USD 187 million" (year *before* the
    figure) is handled by the leading-base form below.
    """
    pairs: list[tuple[MoneyFact, int, int, int]] = []
    for match in _MONEY_RE.finditer(sentence):
        absolute_start = sentence_start + match.start()
        absolute_end = sentence_start + match.end()
        fact, _ = _money_in(
            text,
            absolute_start,
            absolute_end,
            EvidenceSource.OUTLOOK_PROSE,
            usd_is_safe=usd_is_safe,
        )
        if fact is None:
            continue
        tail = sentence[match.end() : match.end() + 24]
        year_match = _YEAR_RE.search(tail)
        if year_match is not None:
            pairs.append(
                (fact, int(year_match.group()), absolute_start, absolute_end + year_match.end())
            )
            continue
        head = sentence[max(0, match.start() - 34) : match.start()]
        lead = re.search(rf"(?:from|of|in)\s+the\s+({_YEAR})\s+base\s+of\s*$", head)
        if lead is not None:
            pairs.append(
                (
                    fact,
                    int(lead.group(1)),
                    sentence_start + match.start() - (len(head) - lead.start()),
                    absolute_end,
                )
            )
    return pairs


def _prose_cagrs(
    text: str,
    blocks: _Blocks,
    market_name: str,
    *,
    forecast_period: tuple[int, int] | None,
    historical_period: tuple[int, int] | None,
) -> tuple[list[tuple[CagrKind, Decimal, tuple[int, int] | None, int, int, bool]], int]:
    """Every CAGR a sentence about this market states, classified, plus the count discarded.

    For each ``CAGR`` token: the nearest percentage within :data:`_QUALIFIER_WINDOW` is its rate,
    but only when the next-nearest percentage is more than twice as far away -- otherwise which
    rate the token belongs to is a guess. The kind follows from markers in the same window, and
    **only when markers of one kind appear**: the corpus writes "The historical 2020-2025
    trajectory is modeled at 24.00% CAGR, while the locked forecast CAGR is 27.00%", where the
    window around the first rate holds both, so it is left unclassified rather than guessed.

    Where no marker settles it, the period the mention states does -- but only when that period
    matches exactly one of the two the page declares for itself. "expanding at an 8.70% CAGR
    during 2025-2032" carries no marker in its window, yet the page's own "Forecast Period
    2025-2032" tile identifies it.
    """
    found: list[tuple[CagrKind, Decimal, tuple[int, int] | None, int, int, bool]] = []
    discarded = 0
    for sentence_start, sentence in _self_referential_sentences(text, blocks, market_name):
        for token in re.finditer(r"CAGR", sentence):
            left = max(0, token.start() - _QUALIFIER_WINDOW)
            right = min(len(sentence), token.end() + _QUALIFIER_WINDOW)
            window = sentence[left:right]
            lowered = window.lower()
            if any(bad in lowered for bad in _CAGR_DISQUALIFIERS):
                discarded += 1
                continue
            # Distances are measured in SENTENCE coordinates: a percentage's offsets inside
            # `window` must be shifted by `left` before being compared with the token's.
            rates = sorted(
                (
                    (
                        min(
                            abs(left + m.start() - token.start()),
                            abs(left + m.end() - token.end()),
                        ),
                        left + m.start(),
                        left + m.end(),
                        m,
                    )
                    for m in _PERCENT_RE.finditer(window)
                ),
                key=lambda item: (item[0], item[1]),
            )
            if not rates:
                continue
            if len(rates) > 1 and rates[1][0] <= rates[0][0] * 2:
                discarded += 1
                continue
            _, rate_start, rate_end, rate = rates[0]
            period_match = _PERIOD_RE.search(window)
            period: tuple[int, int] | None = None
            if period_match is not None:
                period = (int(period_match.group("start")), int(period_match.group("end")))
            is_forecast = any(marker in lowered for marker in _FORECAST_MARKERS)
            is_historical = any(marker in lowered for marker in _HISTORICAL_MARKERS)
            if is_forecast == is_historical:
                # No marker, or markers of both kinds: fall back to the period the mention states,
                # but only when it matches exactly one of the periods the page declares for itself
                # ("an 8.70% CAGR during 2025-2032" beside "Forecast Period 2025-2032"). Matching
                # both or neither leaves the mention unclassified, and an unclassified mention is
                # ignored -- it neither supplies a fact nor contradicts one.
                matches_forecast = period is not None and period == forecast_period
                matches_historical = period is not None and period == historical_period
                if matches_forecast == matches_historical:
                    continue
                is_forecast, is_historical = matches_forecast, matches_historical
            span_start = sentence_start + min(rate_start, token.start())
            span_end = sentence_start + max(rate_end, token.end())
            found.append(
                (
                    CagrKind.FORECAST if is_forecast else CagrKind.HISTORICAL,
                    Decimal(rate.group("pct")),
                    period,
                    span_start,
                    span_end,
                    rate.group("approx") is not None,
                )
            )
    return found, discarded


def _implied_cagr_points(start_value: Decimal, end_value: Decimal, years: int) -> Decimal:
    """The CAGR implied by two endpoints, in percentage points. Pure arithmetic on Decimals."""
    ratio = float(end_value / start_value)
    return Decimal(str((ratio ** (1.0 / years) - 1.0) * 100.0))


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def extract_page_evidence(body_text: str, *, market_name: str | None = None) -> PageEvidence:
    """Extract the market statistics a page states about its own market.

    :param body_text: the page's plain body text, exactly as stored. Every returned span slices
        out of *this* string: ``body_text[span.start : span.end] == span.text``.
    :param market_name: the page's own market, when the caller already knows it (from the page
        title, say). Supplying it is the stronger attribution; without it the name is derived from
        the text, and when it cannot be derived no prose figure is read at all.

    Pure and total: never raises for ordinary text, and returns an empty :class:`PageEvidence`
    with refusal codes rather than a doubtful fact.
    """
    refusals: list[Refusal] = []

    def refuse(
        code: RefusalCode,
        field: Literal["market_value", "forecast_value", "forecast_cagr", "historical_cagr"],
        detail: str = "",
    ) -> None:
        refusals.append(Refusal(code=code, field=field, detail=detail))

    blocks = _Blocks(body_text)
    usd_is_safe = "USD" in body_text
    card = _read_card(body_text, blocks, usd_is_safe=usd_is_safe)
    name = market_name or _market_name(body_text, blocks)

    if not card.present:
        refuse(RefusalCode.NO_SUMMARY_CARD, "market_value")
    elif card.value is None and card.value_refusal is not None:
        refuse(card.value_refusal, "market_value")

    market_value = card.value
    forecast_value: MoneyFact | None = None
    forecast_cagr = card.forecast_cagr
    historical_cagr = card.historical_cagr

    if name is None:
        refuse(RefusalCode.NO_SELF_REFERENCE, "forecast_value")
        return PageEvidence(
            market_value=market_value,
            forecast_cagr=forecast_cagr,
            historical_cagr=historical_cagr,
            market_name=None,
            refusals=tuple(refusals),
        )

    # ----- the outlook prose: the only trusted source of a forecast year ----- #
    sentences = _outlook_sentences(body_text, blocks, name)
    if not sentences:
        refuse(RefusalCode.NO_OUTLOOK_SENTENCE, "forecast_value")
    else:
        pairs = [
            pair
            for start, sentence in sentences
            for pair in _money_year_pairs(body_text, start, sentence, usd_is_safe=usd_is_safe)
        ]
        if market_value is not None:
            stated = [p for p in pairs if p[1] == market_value.year]
            if stated and not all(_same_money(p[0], market_value) for p in stated):
                refuse(
                    RefusalCode.VALUE_DISAGREEMENT,
                    "market_value",
                    "the summary card and the outlook prose state different base-year values",
                )
                market_value = None
        terminal = card.forecast_period[1] if card.forecast_period else None
        if terminal is None and market_value is not None:
            later = sorted({p[1] for p in pairs if p[1] > market_value.year})
            terminal = later[-1] if len(later) == 1 else None
        if terminal is None:
            refuse(RefusalCode.AMBIGUOUS_FORECAST_YEAR, "forecast_value", "no declared end year")
        else:
            at_terminal = [p for p in pairs if p[1] == terminal]
            unique = {(p[0].currency, p[0].amount_in_millions) for p in at_terminal}
            if len(at_terminal) >= 1 and len(unique) == 1:
                fact, year, span_start, span_end = at_terminal[0]
                forecast_value = _with_year_and_span(
                    fact,
                    year,
                    (_span(body_text, span_start, span_end, EvidenceSource.OUTLOOK_PROSE),),
                )
            else:
                refuse(
                    RefusalCode.AMBIGUOUS_FORECAST_YEAR,
                    "forecast_value",
                    f"{len(unique)} distinct values stated for {terminal}",
                )

    # ----- prose CAGRs: corroborate the card, or stand alone when there is no card ----- #
    prose, _discarded = _prose_cagrs(
        body_text,
        blocks,
        name,
        forecast_period=card.forecast_period,
        historical_period=card.historical_period,
    )
    for kind, percent, period, span_start, span_end, approximate in prose:
        existing = forecast_cagr if kind is CagrKind.FORECAST else historical_cagr
        field: Literal["forecast_cagr", "historical_cagr"] = (
            "forecast_cagr" if kind is CagrKind.FORECAST else "historical_cagr"
        )
        span = _span(body_text, span_start, span_end, EvidenceSource.OUTLOOK_PROSE)
        if existing is not None:
            if existing.percent != percent:
                refuse(
                    RefusalCode.CAGR_DISAGREEMENT,
                    field,
                    f"card states {existing.percent}%, prose states {percent}%",
                )
                if kind is CagrKind.FORECAST:
                    forecast_cagr = None
                else:
                    historical_cagr = None
                continue
            updated = existing.model_copy(update={"evidence": (*existing.evidence, span)})
            if kind is CagrKind.FORECAST:
                forecast_cagr = updated
            else:
                historical_cagr = updated
            continue
        declared = card.forecast_period if kind is CagrKind.FORECAST else card.historical_period
        window = period or declared
        if window is None:
            refuse(RefusalCode.NO_PERIOD, field, "a rate with no period is not publishable")
            continue
        if period is not None and declared is not None and period != declared:
            refuse(RefusalCode.NO_PERIOD, field, "the stated period is not the page's own")
            continue
        fresh = CagrFact(
            percent=percent,
            kind=kind,
            period_start=window[0],
            period_end=window[1],
            approximate=approximate,
            evidence=(span,),
        )
        if kind is CagrKind.FORECAST:
            forecast_cagr = fresh
        else:
            historical_cagr = fresh

    # ----- arithmetic cross-check: the endpoints must produce the stated rate ----- #
    if market_value is not None and forecast_value is not None and forecast_cagr is not None:
        same_currency = forecast_value.currency == market_value.currency
        if forecast_value.year <= market_value.year or not same_currency:
            refuse(
                RefusalCode.AMBIGUOUS_FORECAST_YEAR,
                "forecast_value",
                "the forecast figure is not a later value of the same currency",
            )
            forecast_value = None
        else:
            implied = _implied_cagr_points(
                market_value.amount_in_millions,
                forecast_value.amount_in_millions,
                forecast_value.year - market_value.year,
            )
            if abs(implied - forecast_cagr.percent) > CAGR_TOLERANCE_POINTS:
                refuse(
                    RefusalCode.CAGR_INCONSISTENT_WITH_VALUES,
                    "forecast_cagr",
                    f"endpoints imply {implied.quantize(Decimal('0.01'))}%, "
                    f"the page states {forecast_cagr.percent}%",
                )
                forecast_value = None
                forecast_cagr = None

    return PageEvidence(
        market_value=market_value,
        forecast_value=forecast_value,
        forecast_cagr=forecast_cagr,
        historical_cagr=historical_cagr,
        market_name=name,
        refusals=tuple(refusals),
    )
