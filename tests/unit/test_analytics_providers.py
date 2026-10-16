"""The analytics providers and the URL resolver: no database, no network (ADR-0015 §4, §9).

The CSV provider is the fallback transport Ken can always use, so its rules are asserted
exactly: two canonical headers, ISO dates only, unknown columns refused, malformed rows
COUNTED and never echoed, and the sha256 plus basename the ledger records.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date
from pathlib import Path

import pytest

from ken_contracts.analytics import (
    AnalyticsBatchProvider,
    AnalyticsProvider,
    Dataset,
    DateRange,
    MetricSource,
    PageUrlResolver,
)
from ken_core.config import ConfigurationError
from ken_core.providers.analytics.base import AnalyticsFetchError, FetchErrorCode
from ken_core.providers.analytics.csv import (
    LINK_HEADER,
    PAGE_HEADER,
    CsvAnalyticsProvider,
    read_csv_file,
)
from ken_core.providers.analytics.factory import ANALYTICS_PROVIDERS, build_analytics_provider
from ken_core.providers.analytics.ga4 import Ga4AnalyticsProvider
from ken_core.providers.analytics.gsc import SearchConsoleAnalyticsProvider
from ken_core.providers.analytics.mock import MockAnalyticsProvider
from ken_services.analytics.urls import InventoryUrlResolver
from tests.conftest import make_settings

RANGE = DateRange(start=date(2026, 5, 11), end=date(2026, 5, 12))
PLAN = uuid.UUID("11111111-1111-4111-8111-111111111111")
PAGE_A = uuid.UUID("22222222-2222-4222-8222-222222222222")
PAGE_B = uuid.UUID("33333333-3333-4333-8333-333333333333")
SITE = "https://site.test"


class FixedResolver:
    """A minimal :class:`PageUrlResolver` (no diagnostics), to prove the fallback path."""

    def __init__(self, mapping: dict[str, uuid.UUID]) -> None:
        self.mapping = mapping

    def resolve(self, url: str) -> str | None:
        found = self.mapping.get(url)
        return None if found is None else str(found)


def _inventory() -> InventoryUrlResolver:
    return InventoryUrlResolver(
        canonical=[(f"{SITE}/a", PAGE_A), (f"{SITE}/b", PAGE_B)],
        known=[(f"{SITE}/alias", PAGE_A)],
    )


def _csv(directory: Path, header: tuple[str, ...], *rows: str, name: str = "f.csv") -> Path:
    path = directory / name
    path.write_text("\n".join([",".join(header), *rows]) + "\n", encoding="utf-8")
    return path


def _provider(path: Path, *, resolver: PageUrlResolver | None = None) -> CsvAnalyticsProvider:
    return CsvAnalyticsProvider(
        path=path,
        resolver=resolver or _inventory(),
        max_file_bytes=1_000_000,
        max_rows=1000,
    )


# --------------------------------------------------------------------------- #
# Protocol conformance
# --------------------------------------------------------------------------- #
def test_every_provider_satisfies_the_frozen_protocols(tmp_path: Path) -> None:
    settings = make_settings(
        ga4_property_id="123456", gsc_site_url="sc-domain:site.test", environment="local"
    )
    providers = [
        MockAnalyticsProvider(),
        _provider(_csv(tmp_path, LINK_HEADER)),
        Ga4AnalyticsProvider(settings, resolver=_inventory()),
        SearchConsoleAnalyticsProvider(settings, resolver=_inventory()),
    ]
    for provider in providers:
        assert isinstance(provider, AnalyticsBatchProvider)
        assert isinstance(provider, AnalyticsProvider)


async def test_fetch_link_and_page_metrics_are_filters_over_fetch_batch(tmp_path: Path) -> None:
    path = _csv(
        tmp_path,
        LINK_HEADER,
        f"2026-05-11,first_party,{PLAN},10,1",
        f"2026-05-12,first_party,{uuid.uuid4()},20,2",
    )
    provider = _provider(path)
    everything = await provider.fetch_link_metrics(link_ids=[], date_range=RANGE)
    assert len(everything) == 2
    mine = await provider.fetch_link_metrics(link_ids=[str(PLAN)], date_range=RANGE)
    assert [row.link_id for row in mine] == [str(PLAN)]
    # A links file holds no page rows at all, and asking for the other dataset is refused.
    with pytest.raises(AnalyticsFetchError):
        await provider.fetch_page_metrics(page_ids=[], date_range=RANGE)


async def test_a_provider_health_is_safe_and_close_is_idempotent() -> None:
    provider = MockAnalyticsProvider()
    health = await provider.health()
    assert health.reachable is True
    assert health.error is None
    await provider.aclose()
    await provider.aclose()


# --------------------------------------------------------------------------- #
# ken_link_daily
# --------------------------------------------------------------------------- #
async def test_the_link_format_is_parsed_with_its_origin_and_daily_range(
    tmp_path: Path,
) -> None:
    path = _csv(
        tmp_path,
        LINK_HEADER,
        f"2026-05-11,first_party,{PLAN},100,4",
        f"2026-05-12,ga4,{PLAN},120,6",
    )
    provider = _provider(path)
    batch = await provider.fetch_batch(date_range=RANGE, datasets=frozenset({Dataset.LINKS}))
    assert [row.source for row in batch.link_rows] == [
        MetricSource.FIRST_PARTY,
        MetricSource.GA4,
    ]
    assert [row.range.start == row.range.end for row in batch.link_rows] == [True, True]
    assert batch.link_rows[0].impressions == 100
    assert batch.stats.rows_read == 2
    assert batch.stats.rejected_invalid == 0
    assert provider.file_name == path.name
    assert provider.file_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "row",
    [
        f"2026-5-11,first_party,{PLAN},1,1",  # not ISO
        f"20260511,first_party,{PLAN},1,1",  # the compact form is NOT accepted
        f"2026-05-11,csv,{PLAN},1,1",  # csv is a transport, never an origin
        f"2026-05-11,search_console,{PLAN},1,1",  # no link-level search data
        f"2026-05-11,first_party,{PLAN},-1,1",  # negative
        f"2026-05-11,first_party,{PLAN},1.5,1",  # not a whole number
        f"2026-05-11,first_party,{PLAN},1",  # too few fields
        f"2026-05-11,first_party,{PLAN},1,1,extra",  # too many fields
        "2026-05-11,first_party,,1,1",  # no link id
    ],
)
async def test_a_malformed_link_row_is_counted_and_never_echoed(tmp_path: Path, row: str) -> None:
    provider = _provider(_csv(tmp_path, LINK_HEADER, row))
    batch = await provider.fetch_batch(date_range=RANGE, datasets=frozenset({Dataset.LINKS}))
    assert batch.link_rows == ()
    assert batch.stats.rows_read == 1
    assert batch.stats.rejected_invalid + batch.stats.unresolved_link == 1
    assert row not in repr(batch.stats)


# --------------------------------------------------------------------------- #
# ken_page_daily
# --------------------------------------------------------------------------- #
async def test_the_page_format_resolves_urls_and_counts_misses(tmp_path: Path) -> None:
    path = _csv(
        tmp_path,
        PAGE_HEADER,
        f"2026-05-11,search_console,{SITE}/a?utm_source=x,100,5,3.5,0,0",
        f"2026-05-11,search_console,{SITE}/alias,10,1,,0,0",
        f"2026-05-11,search_console,{SITE}/missing,9,1,4.0,0,0",
        f"2026-05-11,ga4,{SITE}/b,0,0,,42,30",
    )
    provider = _provider(path)
    batch = await provider.fetch_batch(date_range=RANGE, datasets=frozenset({Dataset.PAGES}))
    assert [row.page_id for row in batch.page_rows] == [
        str(PAGE_A),
        str(PAGE_A),
        str(PAGE_B),
    ]
    assert batch.page_rows[0].position == 3.5
    assert batch.page_rows[1].position is None
    assert batch.page_rows[2].pageviews == 42
    assert batch.stats.unresolved_url == 1
    assert batch.stats.unresolved_sample == ("/missing",)


async def test_a_resolver_without_diagnostics_still_counts_a_miss(tmp_path: Path) -> None:
    provider = _provider(
        _csv(tmp_path, PAGE_HEADER, f"2026-05-11,ga4,{SITE}/zzz,0,0,,1,1"),
        resolver=FixedResolver({}),
    )
    batch = await provider.fetch_batch(date_range=RANGE, datasets=frozenset({Dataset.PAGES}))
    assert batch.page_rows == ()
    assert batch.stats.unresolved_url == 1
    assert batch.stats.unresolved_sample == ()


@pytest.mark.parametrize(
    "position",
    ["0.5", "abc", "nan", "inf"],
)
async def test_an_impossible_position_rejects_the_row(tmp_path: Path, position: str) -> None:
    provider = _provider(
        _csv(tmp_path, PAGE_HEADER, f"2026-05-11,search_console,{SITE}/a,5,1,{position},0,0")
    )
    batch = await provider.fetch_batch(date_range=RANGE, datasets=frozenset({Dataset.PAGES}))
    assert batch.page_rows == ()
    assert batch.stats.rejected_invalid == 1


# --------------------------------------------------------------------------- #
# File-level rules
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "header",
    [
        ("date", "source", "link_id", "impressions"),  # missing a column
        ("date", "source", "link_id", "impressions", "clicks", "landings"),  # unknown column
        ("source", "date", "link_id", "impressions", "clicks"),  # reordered
        ("date", "ken_link_id", "impressions", "event_name", "event_count"),  # a cut format
    ],
)
async def test_an_unknown_header_is_file_invalid(tmp_path: Path, header: tuple[str, ...]) -> None:
    provider = _provider(_csv(tmp_path, header))
    with pytest.raises(AnalyticsFetchError) as refused:
        await provider.fetch_batch(date_range=RANGE, datasets=frozenset({Dataset.LINKS}))
    assert refused.value.code is FetchErrorCode.FILE_INVALID


async def test_asking_for_the_other_dataset_is_refused(tmp_path: Path) -> None:
    provider = _provider(_csv(tmp_path, LINK_HEADER, f"2026-05-11,first_party,{PLAN},1,1"))
    with pytest.raises(AnalyticsFetchError) as refused:
        await provider.fetch_batch(date_range=RANGE, datasets=frozenset({Dataset.PAGES}))
    assert refused.value.code is FetchErrorCode.INVALID_REQUEST


async def test_an_empty_file_is_file_invalid(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(AnalyticsFetchError) as refused:
        await _provider(path).fetch_batch(date_range=RANGE, datasets=frozenset({Dataset.LINKS}))
    assert refused.value.code is FetchErrorCode.FILE_INVALID


async def test_a_file_over_the_size_cap_is_refused(tmp_path: Path) -> None:
    path = _csv(tmp_path, LINK_HEADER, f"2026-05-11,first_party,{PLAN},1,1")
    provider = CsvAnalyticsProvider(
        path=path, resolver=_inventory(), max_file_bytes=10, max_rows=10
    )
    with pytest.raises(AnalyticsFetchError) as refused:
        await provider.fetch_batch(date_range=RANGE, datasets=frozenset({Dataset.LINKS}))
    assert refused.value.code is FetchErrorCode.FILE_TOO_LARGE


async def test_a_file_over_the_row_cap_is_refused(tmp_path: Path) -> None:
    rows = [f"2026-05-11,first_party,{uuid.uuid4()},1,1" for _ in range(4)]
    provider = CsvAnalyticsProvider(
        path=_csv(tmp_path, LINK_HEADER, *rows),
        resolver=_inventory(),
        max_file_bytes=1_000_000,
        max_rows=3,
    )
    with pytest.raises(AnalyticsFetchError) as refused:
        await provider.fetch_batch(date_range=RANGE, datasets=frozenset({Dataset.LINKS}))
    assert refused.value.code is FetchErrorCode.TOO_MANY_ROWS


def test_reading_a_missing_file_names_no_path(tmp_path: Path) -> None:
    missing = tmp_path / "secret-directory" / "nope.csv"
    with pytest.raises(AnalyticsFetchError) as refused:
        read_csv_file(missing, max_bytes=100)
    assert str(refused.value) == FetchErrorCode.FILE_INVALID.value
    assert "secret-directory" not in str(refused.value)


def test_a_bom_and_crlf_file_still_parses(tmp_path: Path) -> None:
    path = tmp_path / "bom.csv"
    path.write_bytes(
        "﻿".encode() + f"{','.join(LINK_HEADER)}\r\n2026-05-11,first_party,{PLAN},1,1\r\n".encode()
    )
    loaded = read_csv_file(path, max_bytes=1000)
    assert loaded.text.startswith("date,source")


# --------------------------------------------------------------------------- #
# The factory
# --------------------------------------------------------------------------- #
def test_the_factory_builds_each_provider(tmp_path: Path) -> None:
    settings = make_settings(ga4_property_id="123", gsc_site_url="sc-domain:site.test")
    resolver = _inventory()
    path = _csv(tmp_path, LINK_HEADER)
    built = {
        name: build_analytics_provider(
            settings, name=name, resolver=resolver, file=path if name == "csv" else None
        )
        for name in ANALYTICS_PROVIDERS
    }
    assert set(built) == set(ANALYTICS_PROVIDERS)
    for provider in built.values():
        assert isinstance(provider, AnalyticsBatchProvider)


@pytest.mark.parametrize(
    ("name", "settings_kwargs"),
    [
        ("csv", {}),  # no FILE=
        ("ga4", {"gsc_site_url": "sc-domain:site.test"}),  # no property id
        ("gsc", {"ga4_property_id": "123"}),  # no site
        ("jsonl", {}),  # deferred to Phase 8
        ("", {}),
    ],
)
def test_the_factory_refuses_an_unusable_request(
    name: str, settings_kwargs: dict[str, object]
) -> None:
    settings = make_settings(**settings_kwargs)
    with pytest.raises(ConfigurationError):
        build_analytics_provider(settings, name=name, resolver=_inventory(), file=None)


# --------------------------------------------------------------------------- #
# The URL resolver
# --------------------------------------------------------------------------- #
def test_the_resolver_ignores_scheme_query_and_fragment() -> None:
    resolver = _inventory()
    for url in (
        f"{SITE}/a",
        "http://site.test/a",
        f"{SITE}/a?utm_source=news",
        f"{SITE}/a#section",
        f"{SITE}/./a",
        f"{SITE}/b/../a",
    ):
        assert resolver.resolve(url) == str(PAGE_A), url


def test_a_known_url_fills_a_key_the_canonical_urls_do_not_cover() -> None:
    assert _inventory().resolve(f"{SITE}/alias") == str(PAGE_A)


def test_a_canonical_url_wins_over_a_known_url_for_the_same_key() -> None:
    resolver = InventoryUrlResolver(
        canonical=[(f"{SITE}/a", PAGE_A)], known=[(f"{SITE}/a", PAGE_B)]
    )
    assert resolver.resolve(f"{SITE}/a") == str(PAGE_A)
    assert resolver.ambiguous(f"{SITE}/a") is False


def test_two_pages_on_one_key_are_ambiguous_not_unresolved() -> None:
    resolver = InventoryUrlResolver(canonical=[(f"{SITE}/a", PAGE_A), (f"{SITE}/a?x=1", PAGE_B)])
    assert resolver.resolve(f"{SITE}/a") is None
    assert resolver.ambiguous(f"{SITE}/a") is True


def test_an_unknown_url_is_unresolved_with_a_bounded_sample_path() -> None:
    resolver = _inventory()
    assert resolver.resolve(f"{SITE}/missing?q=1") is None
    assert resolver.ambiguous(f"{SITE}/missing") is False
    assert resolver.sample_path(f"{SITE}/missing?q=1") == "/missing"
    assert resolver.sample_path("not a url") is None
    assert resolver.resolve("not a url") is None


def test_a_very_long_path_is_not_sampled() -> None:
    resolver = _inventory()
    long_path = "/" + "x" * 600
    assert resolver.sample_path(SITE + long_path) is None


def test_the_resolver_satisfies_the_frozen_protocol() -> None:
    assert isinstance(_inventory(), PageUrlResolver)
    assert len(_inventory()) == 3


# --------------------------------------------------------------------------- #
# The service implementations against the FROZEN protocols
# --------------------------------------------------------------------------- #
def test_the_service_entry_points_match_the_frozen_protocol_signatures() -> None:
    """``protocol.py`` is frozen, so an implementation that drifts from it -- a renamed or
    newly positional parameter -- must fail here rather than at the first real import."""
    import inspect

    from ken_services.analytics import aggregate, attribution, importer, measure
    from ken_services.analytics.protocol import (
        LiveWindows,
        MeasureSweep,
        RunAggregation,
        RunImport,
    )

    pairs = (
        (RunImport, importer.run_import),
        (RunAggregation, aggregate.run_aggregation),
        (MeasureSweep, measure.measure_sweep),
        (LiveWindows, attribution.live_windows),
    )
    for protocol, implementation in pairs:
        expected = [
            parameter
            for name, parameter in inspect.signature(protocol.__call__).parameters.items()
            if name != "self"
        ]
        actual = list(inspect.signature(implementation).parameters.values())
        where = protocol.__name__
        assert [p.name for p in expected] == [p.name for p in actual], where
        assert [p.kind for p in expected] == [p.kind for p in actual], where
        assert inspect.iscoroutinefunction(implementation), where


def test_the_sql_reader_satisfies_the_frozen_reader_protocol() -> None:
    from ken_contracts.analytics import AnalyticsReader
    from ken_services.analytics.reader import SqlAnalyticsReader

    reader = SqlAnalyticsReader(sessionmaker=None, settings=make_settings())  # type: ignore[arg-type]
    assert isinstance(reader, AnalyticsReader)
