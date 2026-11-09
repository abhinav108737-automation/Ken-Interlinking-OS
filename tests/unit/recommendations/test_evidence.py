"""ADR-0023 decision 2: every figure traceable to a verbatim span of the target page.

Two kinds of test, and the second kind matters more.

*What it extracts* is checked against two real pages, with their text pasted in as literals so
nothing here needs a database. *What it refuses* is checked against the ways this corpus can
mislead a parser: a promotional strip of other markets' growth rates, a volume CAGR beside a
market CAGR, a summary-card tile whose year label contradicts the page's own forecast period, a
figure with no magnitude unit, a currency that is only a guess. Returning fewer facts costs a
sentence; returning a wrong one is published under Ken Research's name, so the refusals are the
specification.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from ken_services.recommendations.evidence import (
    CagrKind,
    EvidenceSource,
    EvidenceSpan,
    MoneyUnit,
    PageEvidence,
    RefusalCode,
    extract_page_evidence,
)

# --------------------------------------------------------------------------- #
# Real pages, verbatim.
#
# Trimmed to the blocks that carry the statistics (overview lead, summary card,
# future-outlook paragraph, CAGR and period tiles) in their original order and
# spelling, from ken_real500 on 2026-10-30. Both are pure ASCII as stored.
# --------------------------------------------------------------------------- #
INDIA_COSMETICS = (
    "Market Overview\n\nThe India Cosmetics Market operates through general trade, modern retail,"
    " specialty outlets, digital marketplaces, brand websites and quick-commerce platforms. In"
    " 2025, women represented 70.5% of market value, while male and gender-neutral demand"
    " expanded from smaller bases.\n\nMarket Value\n\nUSD 16,500 Mn\n\n2025\n\nFuture Outlook\n\n"
    "The India Cosmetics Market is projected to reach USD 29,586 Mn by 2032, expanding at an 8.70%"
    " CAGR during 2025-2032. This compares with an estimated historical CAGR of 7.90% during"
    " 2020-2025. Premium skin care, colour cosmetics, men's grooming and digitally native brands"
    " will provide above-market growth.\n\n8.70%\n\nForecast CAGR\n\nUSD 29,586 Mn\n\n"
    "2030 Projection\n\nBase Year\n\n2025\n\nHistorical Period\n\n2020-2025\n\nForecast Period"
    "\n\n2025-2032\n\nHistorical CAGR\n\n7.90%"
)

VEGETABLE_SEED = (
    "Market Overview\n\nThe Asia Pacific Vegetable Seed Market connects breeders, multiplication"
    " contractors, seed processors and distributors with growers purchasing planting material."
    "\n\nMarket Value\n\nUSD 2,851 million, Asia Pacific, 2025\n\nFuture Outlook\n\nThe Asia"
    " Pacific Vegetable Seed Market is projected to advance from USD 2,851 million in 2025 to USD"
    " 3,867 million in 2031 and USD 4,068 million in 2032. Its calculated forecast CAGR is 5.21%"
    " for 2025-2032, compared with a modeled historical CAGR of 4.94% for 2020-2025. Commercial"
    " seed volume reaches an estimated 37,636 metric tons in 2032, producing a volume CAGR of"
    " 2.65% for 2025-2032. Average revenue per kilogram increases to USD 108.09, reflecting a"
    " calculated price-and-mix CAGR of 2.49% over the same period.\n\n5.21%\n\nForecast CAGR\n\n"
    "USD 4,068 million\n\n2030 Projection\n\nBase Year\n\n2025\n\nHistorical Period\n\n2020-2025"
    "\n\nForecast Period\n\n2025-2032\n\nHistorical CAGR\n\n4.94%"
)

PROMOTIONAL_STRIP = (
    "\n\nLive Coverage from 100+ Global Markets\n\nView All Reports\n\nAPAC\n\n"
    "Vietnam Mobile Payments Market\n\nCAGR 27.5%\n\nMEA\n\nKSA Landscaping Market\n\nCAGR 8.9%"
    "\n\nNorth America\n\nCanada EV Market\n\nCAGR 16.6%"
)
"""The site-wide strip of *other* markets' growth rates, present on many pages in the corpus."""


NO_FIGURES_OUTLOOK = "The Example Widgets Market is projected to grow over the coming decade."
"""An outlook with no money figure, so a test can vary the summary card in isolation without the
card-versus-prose agreement check firing on a deliberately mismatched value."""


def _page(
    *,
    overview: str = "The Example Widgets Market serves manufacturers across the region.",
    value: str = "USD 1,000 Mn\n\n2025",
    outlook: str = (
        "The Example Widgets Market is projected to expand from USD 1,000 Mn in 2025 to"
        " USD 2,000 Mn by 2032, representing a 10.4% forecast CAGR."
    ),
    forecast_cagr: str = "10.4%",
    historical_cagr: str = "6.0%",
    base_year: str = "2025",
    historical_period: str = "2020-2025",
    forecast_period: str = "2025-2032",
    suffix: str = "",
) -> str:
    """A minimal page in the corpus's own shape, so one variable can be changed at a time."""
    return (
        f"Market Overview\n\n{overview}\n\n"
        f"Market Value\n\n{value}\n\n"
        f"Future Outlook\n\n{outlook}\n\n"
        f"{forecast_cagr}\n\nForecast CAGR\n\n"
        f"Base Year\n\n{base_year}\n\n"
        f"Historical Period\n\n{historical_period}\n\n"
        f"Forecast Period\n\n{forecast_period}\n\n"
        f"Historical CAGR\n\n{historical_cagr}"
        f"{suffix}"
    )


def _codes(evidence: PageEvidence, field: str) -> set[RefusalCode]:
    return {r.code for r in evidence.refusals if r.field == field}


def _assert_spans_slice_back(body: str, evidence: PageEvidence) -> None:
    """The invariant the whole design rests on: a caller can re-derive every claim."""
    spans = evidence.spans()
    assert spans, "a returned fact must carry at least one span"
    for span in spans:
        assert body[span.start : span.end] == span.text


# --------------------------------------------------------------------------- #
# india-cosmetics-market
# --------------------------------------------------------------------------- #
class TestIndiaCosmetics:
    @pytest.fixture
    def evidence(self) -> PageEvidence:
        return extract_page_evidence(INDIA_COSMETICS)

    def test_market_name_is_the_pages_own(self, evidence: PageEvidence) -> None:
        assert evidence.market_name == "India Cosmetics Market"

    def test_market_value(self, evidence: PageEvidence) -> None:
        value = evidence.market_value
        assert value is not None
        assert value.currency == "USD"
        assert value.amount == Decimal(16500)
        assert value.unit is MoneyUnit.MILLION
        assert value.amount_in_millions == Decimal(16500)
        assert value.year == 2025
        assert value.approximate is False
        assert value.evidence[0].text == "Market Value\n\nUSD 16,500 Mn\n\n2025"
        assert value.evidence[0].source is EvidenceSource.SUMMARY_CARD

    def test_forecast_value(self, evidence: PageEvidence) -> None:
        forecast = evidence.forecast_value
        assert forecast is not None
        assert (forecast.amount, forecast.unit, forecast.year) == (
            Decimal(29586),
            MoneyUnit.MILLION,
            2032,
        )
        assert forecast.evidence[0].text == "USD 29,586 Mn by 2032"
        assert forecast.evidence[0].source is EvidenceSource.OUTLOOK_PROSE

    def test_forecast_year_is_never_taken_from_the_projection_tile(
        self, evidence: PageEvidence
    ) -> None:
        """The tile is labelled "2030 Projection" on a page whose forecast period ends in 2032.

        The label is wrong on nearly every page of this corpus, so the year comes from the prose
        and is cross-checked against the declared forecast period. A 2030 here would be a figure
        published against the wrong year.
        """
        assert "2030 Projection" in INDIA_COSMETICS
        assert evidence.forecast_value is not None
        assert evidence.forecast_value.year == 2032

    def test_forecast_cagr(self, evidence: PageEvidence) -> None:
        cagr = evidence.forecast_cagr
        assert cagr is not None
        assert cagr.percent == Decimal("8.70")
        assert cagr.kind is CagrKind.FORECAST
        assert (cagr.period_start, cagr.period_end) == (2025, 2032)

    def test_forecast_cagr_is_corroborated_by_both_card_and_prose(
        self, evidence: PageEvidence
    ) -> None:
        cagr = evidence.forecast_cagr
        assert cagr is not None
        sources = [span.source for span in cagr.evidence]
        assert EvidenceSource.SUMMARY_CARD in sources
        assert EvidenceSource.OUTLOOK_PROSE in sources
        assert "8.70% CAGR" in next(
            s.text for s in cagr.evidence if s.source is EvidenceSource.OUTLOOK_PROSE
        )

    def test_historical_cagr_is_returned_separately(self, evidence: PageEvidence) -> None:
        historical = evidence.historical_cagr
        assert historical is not None
        assert historical.percent == Decimal("7.90")
        assert historical.kind is CagrKind.HISTORICAL
        assert (historical.period_start, historical.period_end) == (2020, 2025)
        assert evidence.forecast_cagr is not None
        assert evidence.forecast_cagr.percent != historical.percent

    def test_every_span_slices_back(self, evidence: PageEvidence) -> None:
        _assert_spans_slice_back(INDIA_COSMETICS, evidence)

    def test_nothing_is_refused(self, evidence: PageEvidence) -> None:
        assert evidence.refusals == ()


# --------------------------------------------------------------------------- #
# asia-pacific-vegetable-seed-market: the page that states both CAGRs at once
# --------------------------------------------------------------------------- #
class TestVegetableSeed:
    @pytest.fixture
    def evidence(self) -> PageEvidence:
        return extract_page_evidence(VEGETABLE_SEED)

    def test_market_value_with_region_and_year_in_one_tile(self, evidence: PageEvidence) -> None:
        value = evidence.market_value
        assert value is not None
        assert (value.currency, value.amount, value.unit, value.year) == (
            "USD",
            Decimal(2851),
            MoneyUnit.MILLION,
            2025,
        )
        assert value.evidence[0].text == "Market Value\n\nUSD 2,851 million, Asia Pacific, 2025"

    def test_forecast_value_is_the_end_of_the_declared_period_not_the_intermediate_year(
        self, evidence: PageEvidence
    ) -> None:
        """The prose states 2031 *and* 2032; the page's declared forecast period ends in 2032."""
        forecast = evidence.forecast_value
        assert forecast is not None
        assert forecast.year == 2032
        assert forecast.amount == Decimal(4068)
        assert "3,867" not in forecast.evidence[0].text

    def test_forecast_cagr_is_the_forecast_one(self, evidence: PageEvidence) -> None:
        cagr = evidence.forecast_cagr
        assert cagr is not None
        assert cagr.percent == Decimal("5.21")
        assert cagr.kind is CagrKind.FORECAST
        assert (cagr.period_start, cagr.period_end) == (2025, 2032)

    def test_the_historical_rate_is_never_returned_as_the_forecast(
        self, evidence: PageEvidence
    ) -> None:
        """Both rates sit in one sentence. Conflating them would publish a false claim."""
        assert evidence.forecast_cagr is not None
        assert evidence.forecast_cagr.percent != Decimal("4.94")
        assert evidence.historical_cagr is not None
        assert evidence.historical_cagr.percent == Decimal("4.94")
        assert evidence.historical_cagr.kind is CagrKind.HISTORICAL
        assert (evidence.historical_cagr.period_start, evidence.historical_cagr.period_end) == (
            2020,
            2025,
        )

    def test_the_volume_cagr_is_not_returned(self, evidence: PageEvidence) -> None:
        assert "volume CAGR of 2.65%" in VEGETABLE_SEED
        rates = {
            fact.percent
            for fact in (evidence.forecast_cagr, evidence.historical_cagr)
            if fact is not None
        }
        assert Decimal("2.65") not in rates

    def test_the_price_and_mix_cagr_is_not_returned(self, evidence: PageEvidence) -> None:
        assert "price-and-mix CAGR of 2.49%" in VEGETABLE_SEED
        rates = {
            fact.percent
            for fact in (evidence.forecast_cagr, evidence.historical_cagr)
            if fact is not None
        }
        assert Decimal("2.49") not in rates

    def test_a_revenue_per_kilogram_is_not_a_market_value(self, evidence: PageEvidence) -> None:
        """ "USD 108.09" has no magnitude unit, so it can never be read as a market size."""
        assert "USD 108.09" in VEGETABLE_SEED
        amounts = {fact.amount for fact in (evidence.market_value, evidence.forecast_value) if fact}
        assert Decimal("108.09") not in amounts

    def test_every_span_slices_back(self, evidence: PageEvidence) -> None:
        _assert_spans_slice_back(VEGETABLE_SEED, evidence)


# --------------------------------------------------------------------------- #
# Attribution: another market's number is never this page's
# --------------------------------------------------------------------------- #
class TestAttribution:
    def test_the_promotional_strip_does_not_change_anything(self) -> None:
        """A strip of other markets' CAGRs is appended; every fact must be unchanged."""
        plain = extract_page_evidence(INDIA_COSMETICS)
        with_strip = extract_page_evidence(INDIA_COSMETICS + PROMOTIONAL_STRIP)
        for field in ("market_value", "forecast_value", "forecast_cagr", "historical_cagr"):
            before = getattr(plain, field)
            after = getattr(with_strip, field)
            assert before is not None
            assert after is not None
            assert before.model_dump() == after.model_dump()

    def test_another_markets_rate_is_never_returned(self) -> None:
        page = _page(suffix=PROMOTIONAL_STRIP)
        evidence = extract_page_evidence(page)
        rates = {
            fact.percent
            for fact in (evidence.forecast_cagr, evidence.historical_cagr)
            if fact is not None
        }
        assert rates.isdisjoint({Decimal("27.5"), Decimal("8.9"), Decimal("16.6")})

    def test_a_neighbouring_markets_figure_in_prose_is_not_attributed(self) -> None:
        """A cited neighbouring market stating its own size must not become this page's value."""
        page = _page(
            suffix=(
                "\n\nThe adjacent Example Gadgets Market is projected to reach USD 9,999 Mn"
                " by 2032, expanding at a 25.0% CAGR during 2025-2032."
            )
        )
        evidence = extract_page_evidence(page)
        assert evidence.forecast_value is not None
        assert evidence.forecast_value.amount == Decimal(2000)
        assert evidence.forecast_cagr is not None
        assert evidence.forecast_cagr.percent == Decimal("10.4")

    def test_a_page_whose_own_market_cannot_be_named_yields_no_prose_figure(self) -> None:
        page = _page(
            overview="This report covers widgets.",
            outlook="It is projected to reach USD 2,000 Mn by 2032.",
        )
        evidence = extract_page_evidence(page)
        assert evidence.market_name is None
        assert evidence.forecast_value is None
        assert RefusalCode.NO_SELF_REFERENCE in _codes(evidence, "forecast_value")
        assert evidence.market_value is not None, "the labelled card still stands on its own"


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
class TestRefusals:
    def test_a_page_with_no_summary_card_yields_nothing(self) -> None:
        evidence = extract_page_evidence(
            "The Example Widgets Market is projected to reach USD 2,000 Mn by 2032,"
            " expanding at a 10.4% CAGR during 2025-2032."
        )
        assert evidence.market_value is None
        assert RefusalCode.NO_SUMMARY_CARD in _codes(evidence, "market_value")

    def test_empty_text_yields_nothing_and_never_raises(self) -> None:
        evidence = extract_page_evidence("")
        assert evidence.is_empty
        assert evidence.spans() == ()

    def test_a_range_is_refused(self) -> None:
        evidence = extract_page_evidence(_page(value="USD 1,000-1,200 Mn\n\n2025"))
        assert evidence.market_value is None
        assert RefusalCode.AMBIGUOUS_AMOUNT in _codes(evidence, "market_value")

    def test_a_figure_without_a_magnitude_unit_is_refused(self) -> None:
        evidence = extract_page_evidence(_page(value="USD 1,000\n\n2025"))
        assert evidence.market_value is None
        assert RefusalCode.AMBIGUOUS_AMOUNT in _codes(evidence, "market_value")

    def test_two_currencies_in_one_tile_are_refused(self) -> None:
        evidence = extract_page_evidence(_page(value="AUD 143 million; USD 92 million\n\n2025"))
        assert evidence.market_value is None
        assert RefusalCode.MIXED_CURRENCY in _codes(evidence, "market_value")

    def test_a_bare_dollar_sign_without_usd_on_the_page_is_refused(self) -> None:
        """``$`` normalises to USD only where the page writes USD too; otherwise it is a guess."""
        page = _page(
            value="$1,000 Mn\n\n2025",
            outlook=(
                "The Example Widgets Market is projected to expand from $1,000 Mn in 2025 to"
                " $2,000 Mn by 2032, representing a 10.4% forecast CAGR."
            ),
        )
        assert "USD" not in page
        evidence = extract_page_evidence(page)
        assert evidence.market_value is None
        assert evidence.forecast_value is None
        assert RefusalCode.BARE_DOLLAR_SIGN in _codes(evidence, "market_value")

    def test_a_bare_dollar_sign_is_accepted_where_the_page_also_writes_usd(self) -> None:
        evidence = extract_page_evidence(
            _page(
                value="$1,000 Mn\n\n2025",
                outlook=(
                    "The Example Widgets Market is projected to expand from USD 1,000 Mn in 2025"
                    " to $2,000 Mn by 2032, representing a 10.4% forecast CAGR."
                ),
            )
        )
        assert evidence.market_value is not None
        assert evidence.market_value.currency == "USD"

    def test_a_repeated_card_label_is_ambiguous_and_refused(self) -> None:
        evidence = extract_page_evidence(_page(suffix="\n\nMarket Value\n\nUSD 7,000 Mn\n\n2025"))
        assert evidence.market_value is None
        assert RefusalCode.NO_SUMMARY_CARD in _codes(evidence, "market_value")

    def test_card_and_prose_disagreeing_on_the_base_value_refuses_it(self) -> None:
        evidence = extract_page_evidence(
            _page(
                outlook=(
                    "The Example Widgets Market is projected to expand from USD 1,100 Mn in 2025"
                    " to USD 2,000 Mn by 2032, representing a 10.4% forecast CAGR."
                )
            )
        )
        assert evidence.market_value is None
        assert RefusalCode.VALUE_DISAGREEMENT in _codes(evidence, "market_value")

    def test_card_and_prose_disagreeing_on_the_forecast_cagr_refuses_it(self) -> None:
        evidence = extract_page_evidence(
            _page(
                forecast_cagr="12.9%",
                outlook=(
                    "The Example Widgets Market is projected to expand from USD 1,000 Mn in 2025"
                    " to USD 2,000 Mn by 2032, representing a 10.4% forecast CAGR."
                ),
            )
        )
        assert evidence.forecast_cagr is None
        assert RefusalCode.CAGR_DISAGREEMENT in _codes(evidence, "forecast_cagr")

    def test_two_different_values_for_the_forecast_year_refuse_it(self) -> None:
        evidence = extract_page_evidence(
            _page(
                outlook=(
                    "The Example Widgets Market is projected to reach USD 2,000 Mn by 2032."
                    " The market is also projected to reach USD 2,500 Mn by 2032."
                )
            )
        )
        assert evidence.forecast_value is None
        assert RefusalCode.AMBIGUOUS_FORECAST_YEAR in _codes(evidence, "forecast_value")

    def test_a_cagr_that_does_not_follow_from_the_endpoints_refuses_both(self) -> None:
        """A rate paired with the wrong endpoints is the failure mode arithmetic catches."""
        evidence = extract_page_evidence(
            _page(
                forecast_cagr="3.0%",
                outlook=(
                    "The Example Widgets Market is projected to expand from USD 1,000 Mn in 2025"
                    " to USD 2,000 Mn by 2032, representing a 3.0% forecast CAGR."
                ),
            )
        )
        assert evidence.forecast_cagr is None
        assert evidence.forecast_value is None
        assert RefusalCode.CAGR_INCONSISTENT_WITH_VALUES in _codes(evidence, "forecast_cagr")

    def test_a_rate_consistent_with_the_endpoints_survives(self) -> None:
        """The same shape with an honest rate: the check must not refuse everything."""
        evidence = extract_page_evidence(_page())
        assert evidence.forecast_cagr is not None
        assert evidence.forecast_value is not None

    def test_a_cagr_with_no_period_anywhere_is_refused(self) -> None:
        page = (
            "Market Overview\n\nThe Example Widgets Market serves manufacturers.\n\n"
            "Market Value\n\nUSD 1,000 Mn\n\n2025\n\nFuture Outlook\n\n"
            "The Example Widgets Market is projected to grow at a 10.4% forecast CAGR."
        )
        evidence = extract_page_evidence(page)
        assert evidence.forecast_cagr is None
        assert RefusalCode.NO_PERIOD in _codes(evidence, "forecast_cagr")


# --------------------------------------------------------------------------- #
# Shapes the corpus actually uses
# --------------------------------------------------------------------------- #
class TestMoneyShapes:
    @pytest.mark.parametrize(
        ("tile", "currency", "amount", "unit", "in_millions"),
        [
            ("USD 16,500 Mn", "USD", Decimal(16500), MoneyUnit.MILLION, Decimal(16500)),
            ("USD 2,851 million", "USD", Decimal(2851), MoneyUnit.MILLION, Decimal(2851)),
            ("USD 3.53 billion", "USD", Decimal("3.53"), MoneyUnit.BILLION, Decimal(3530)),
            ("USD 25 Bn", "USD", Decimal(25), MoneyUnit.BILLION, Decimal(25000)),
            ("USD 905.3B", "USD", Decimal("905.3"), MoneyUnit.BILLION, Decimal("905300.0")),
            ("US$ 1,200 Mn", "USD", Decimal(1200), MoneyUnit.MILLION, Decimal(1200)),
            ("USD 1.2 trillion", "USD", Decimal("1.2"), MoneyUnit.TRILLION, Decimal(1200000)),
            ("AUD 143 million", "AUD", Decimal(143), MoneyUnit.MILLION, Decimal(143)),
            ("USD 1,662.0 million", "USD", Decimal("1662.0"), MoneyUnit.MILLION, Decimal("1662.0")),
        ],
    )
    def test_a_card_tile_parses(
        self,
        tile: str,
        currency: str,
        amount: Decimal,
        unit: MoneyUnit,
        in_millions: Decimal,
    ) -> None:
        page = _page(value=f"{tile}\n\n2025", outlook=NO_FIGURES_OUTLOOK)
        value = extract_page_evidence(page).market_value
        assert value is not None
        assert value.currency == currency
        assert value.amount == amount
        assert value.unit is unit
        assert value.amount_in_millions == in_millions

    def test_a_hedge_before_the_figure_marks_it_approximate(self) -> None:
        evidence = extract_page_evidence(
            _page(
                outlook=(
                    "The Example Widgets Market is projected to expand from USD 1,000 Mn in 2025"
                    " to approximately USD 2,000 Mn by 2032, representing a 10.4% forecast CAGR."
                )
            )
        )
        assert evidence.forecast_value is not None
        assert evidence.forecast_value.approximate is True

    def test_a_hedge_after_a_card_figure_marks_it_approximate(self) -> None:
        page = _page(value="USD 3,500 Mn, Japan, 2025 estimate", outlook=NO_FIGURES_OUTLOOK)
        value = extract_page_evidence(page).market_value
        assert value is not None
        assert value.approximate is True
        assert value.year == 2025

    def test_a_hedge_belonging_to_an_earlier_figure_does_not_carry_over(self) -> None:
        evidence = extract_page_evidence(
            _page(
                outlook=(
                    "The Example Widgets Market is projected to expand from USD 1,000 Mn in 2025"
                    " to about USD 1,700 Mn by 2030 and USD 2,000 Mn by 2032, representing a"
                    " 10.4% forecast CAGR."
                )
            )
        )
        assert evidence.forecast_value is not None
        assert evidence.forecast_value.amount == Decimal(2000)
        assert evidence.forecast_value.approximate is False

    def test_a_tilde_marks_a_rate_approximate(self) -> None:
        evidence = extract_page_evidence(_page(forecast_cagr="~10.4%"))
        assert evidence.forecast_cagr is not None
        assert evidence.forecast_cagr.approximate is True
        assert evidence.forecast_cagr.percent == Decimal("10.4")

    # Hyphen-minus, en dash, em dash, minus sign -- by codepoint, because the glyphs are
    # indistinguishable in source and the corpus uses more than one of them.
    @pytest.mark.parametrize("dash", ["-", chr(0x2013), chr(0x2014), chr(0x2212)])
    def test_a_period_written_with_any_dash_parses(self, dash: str) -> None:
        evidence = extract_page_evidence(_page(forecast_period=f"2025{dash}2032"))
        assert evidence.forecast_cagr is not None
        assert (evidence.forecast_cagr.period_start, evidence.forecast_cagr.period_end) == (
            2025,
            2032,
        )

    def test_a_market_named_without_a_leading_article_is_still_attributed(self) -> None:
        """ "United States Hydrogen Compressors Market is projected to ..." -- no "The"."""
        page = _page(
            overview="Widgets are sold widely.",
            outlook=(
                "Example Widgets Market is projected to expand from USD 1,000 Mn in 2025 to"
                " USD 2,000 Mn by 2032, representing a 10.4% forecast CAGR."
            ),
        )
        evidence = extract_page_evidence(page)
        assert evidence.market_name == "Example Widgets Market"
        assert evidence.forecast_value is not None

    def test_a_leading_clause_is_not_swallowed_into_the_market_name(self) -> None:
        """Real page: "Through 2031, the Portugal Seed Market is estimated at USD 122.4 million".

        A name is only useful if it is the market's. A match starting at the clause would give
        "Through 2031, the Portugal Seed Market", which attributes nothing and would read
        absurdly if a caller ever put it in a sentence.
        """
        page = _page(
            overview="Seeds are sold widely.",
            outlook=(
                "Through 2031, the Example Widgets Market is projected to expand from"
                " USD 1,000 Mn in 2025 to USD 2,000 Mn by 2032."
            ),
        )
        name = extract_page_evidence(page).market_name
        assert name is None or ("," not in name and not any(c.isdigit() for c in name))

    def test_a_caller_supplied_market_name_is_used(self) -> None:
        page = _page(
            overview="This report covers widgets.",
            outlook=(
                "The Widget Pool is projected to expand from USD 1,000 Mn in 2025 to"
                " USD 2,000 Mn by 2032, representing a 10.4% forecast CAGR."
            ),
        )
        assert extract_page_evidence(page).forecast_value is None
        supplied = extract_page_evidence(page, market_name="Widget Pool")
        assert supplied.market_name == "Widget Pool"
        assert supplied.forecast_value is not None
        assert supplied.forecast_value.amount == Decimal(2000)


# --------------------------------------------------------------------------- #
# The span contract, and purity
# --------------------------------------------------------------------------- #
class TestSpanContract:
    def test_a_span_whose_offsets_do_not_cover_its_text_cannot_be_built(self) -> None:
        with pytest.raises(ValidationError):
            EvidenceSpan(text="USD 10 Mn", start=0, end=3, source=EvidenceSource.SUMMARY_CARD)

    def test_a_span_is_frozen(self) -> None:
        span = EvidenceSpan(text="abc", start=4, end=7, source=EvidenceSource.OUTLOOK_PROSE)
        with pytest.raises(ValidationError):
            setattr(span, "start", 0)  # noqa: B010 -- static typing already forbids this

    def test_spans_collects_the_evidence_of_every_returned_fact(self) -> None:
        evidence = extract_page_evidence(INDIA_COSMETICS)
        collected = evidence.spans()
        expected = sum(
            len(fact.evidence)
            for fact in (
                evidence.market_value,
                evidence.forecast_value,
                evidence.forecast_cagr,
                evidence.historical_cagr,
            )
            if fact is not None
        )
        assert len(collected) == expected

    @pytest.mark.parametrize("body", [INDIA_COSMETICS, VEGETABLE_SEED, _page()])
    def test_every_span_of_every_page_slices_back(self, body: str) -> None:
        _assert_spans_slice_back(body, extract_page_evidence(body))

    def test_offsets_hold_after_the_text_is_shifted(self) -> None:
        """Offsets are into the supplied string, not into some normalised copy of it."""
        prefix = "Unrelated preamble about the company.\n\n"
        evidence = extract_page_evidence(prefix + INDIA_COSMETICS)
        _assert_spans_slice_back(prefix + INDIA_COSMETICS, evidence)
        assert evidence.market_value is not None
        assert evidence.market_value.evidence[0].start >= len(prefix)


class TestPurity:
    def test_the_same_input_gives_the_same_output(self) -> None:
        first = extract_page_evidence(VEGETABLE_SEED)
        second = extract_page_evidence(VEGETABLE_SEED)
        assert first.model_dump() == second.model_dump()

    def test_the_result_is_frozen(self) -> None:
        evidence = extract_page_evidence(INDIA_COSMETICS)
        with pytest.raises(ValidationError):
            setattr(evidence, "market_value", None)  # noqa: B010 -- as above

    def test_no_text_is_ever_rejected_with_an_exception(self) -> None:
        for body in ("", "\n\n\n", "CAGR", "USD", "%", "Market Value\n\n", "$" * 500):
            assert extract_page_evidence(body).is_empty
