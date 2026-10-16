"""GA4 and Search Console through ``httpx.MockTransport``: **Google is never called**.

What the review made binding (ADR-0015 §12.3, §12.9.4) is asserted here:

* every call is refused BEFORE any I/O unless ``KEN_ANALYTICS_GOOGLE_ENABLED`` is on;
* the request body carries the property/site and the dates and NOTHING about Ken's
  inventory -- no link id, no URL;
* the token is read from the token file at the start of each fetch, the file must be mode
  0600, and the token never appears in a log, a counter, an error or a snapshot;
* redirects are not followed, the environment's proxies are ignored;
* 429/5xx get a bounded 3-try backoff, 401/403 is the fixed code ``google_auth_failed``;
* GA4 ``(other)`` rows are counted, ``samplingMetadatas`` sets ``sampled``, and paging uses
  ``offset`` / ``startRow``.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog

from ken_contracts.analytics import Dataset, DateRange, MetricSource
from ken_core.config import GA4_BASE_URL, GSC_BASE_URL
from ken_core.providers.analytics.base import (
    GOOGLE_TRIES,
    AnalyticsFetchError,
    FetchErrorCode,
)
from ken_core.providers.analytics.ga4 import (
    EVENT_NAMES,
    GA4_LIMIT,
    LINK_ID_DIMENSION,
    Ga4AnalyticsProvider,
)
from ken_core.providers.analytics.gsc import (
    GSC_ROW_LIMIT,
    GSC_TIMEZONE,
    SearchConsoleAnalyticsProvider,
)
from ken_services.analytics.urls import InventoryUrlResolver
from tests.conftest import make_settings

TOKEN = "ya29.a0-SECRET-TOKEN-VALUE"
PROPERTY = "456789"
SITE_PROPERTY = "sc-domain:site.test"
SITE = "https://site.test"
PAGE_A = uuid.UUID("22222222-2222-4222-8222-222222222222")
PLAN = uuid.UUID("11111111-1111-4111-8111-111111111111")
RANGE = DateRange(start=date(2026, 5, 11), end=date(2026, 5, 12))
LINKS = frozenset({Dataset.LINKS})
PAGES = frozenset({Dataset.PAGES})


def _resolver() -> InventoryUrlResolver:
    return InventoryUrlResolver(canonical=[(f"{SITE}/a", PAGE_A)])


def _token_file(tmp_path: Path, *, mode: int = 0o600, value: str = TOKEN) -> Path:
    path = tmp_path / "token"
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _settings(tmp_path: Path, **overrides: Any) -> Any:
    return make_settings(
        environment="local",
        analytics_google_enabled=True,
        analytics_google_token_file=str(_token_file(tmp_path)),
        ga4_property_id=PROPERTY,
        gsc_site_url=SITE_PROPERTY,
        ga4_timezone="Asia/Kolkata",
        **overrides,
    )


class Recorder:
    """A transport that records every request and answers from a scripted queue."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if not self.responses:
                raise AssertionError("more requests than scripted responses")
            return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

        return httpx.MockTransport(handler)

    def body(self, index: int = 0) -> dict[str, Any]:
        parsed: dict[str, Any] = json.loads(self.requests[index].content)
        return parsed


def _ga4_response(rows: list[dict[str, Any]], **extra: Any) -> httpx.Response:
    return httpx.Response(200, json={"rows": rows, "rowCount": len(rows), **extra})


def _ga4_link_row(day: str, event: str, link_id: str, count: int) -> dict[str, Any]:
    return {
        "dimensionValues": [{"value": day}, {"value": event}, {"value": link_id}],
        "metricValues": [{"value": str(count)}],
    }


def _ga4_page_row(day: str, host: str, path: str, views: int, sessions: int) -> dict[str, Any]:
    return {
        "dimensionValues": [{"value": day}, {"value": host}, {"value": path}],
        "metricValues": [{"value": str(views)}, {"value": str(sessions)}],
    }


# --------------------------------------------------------------------------- #
# Gating
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("provider_class", [Ga4AnalyticsProvider, SearchConsoleAnalyticsProvider])
async def test_a_call_is_refused_before_any_io_when_egress_is_disabled(
    provider_class: Any,
) -> None:
    recorder = Recorder(httpx.Response(200, json={}))
    settings = make_settings(
        analytics_google_enabled=False,
        ga4_property_id=PROPERTY,
        gsc_site_url=SITE_PROPERTY,
    )
    provider = provider_class(settings, resolver=_resolver(), transport=recorder.transport())
    with pytest.raises(AnalyticsFetchError) as refused:
        await provider.fetch_batch(date_range=RANGE, datasets=PAGES)
    assert refused.value.code is FetchErrorCode.GOOGLE_DISABLED
    assert recorder.requests == []


async def test_the_default_base_urls_are_the_exact_google_hosts(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.analytics_ga4_base_url == GA4_BASE_URL
    assert settings.analytics_gsc_base_url == GSC_BASE_URL
    assert GA4_BASE_URL.startswith("https://analyticsdata.googleapis.com")
    assert GSC_BASE_URL.startswith("https://www.googleapis.com")


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o604])
async def test_a_token_file_readable_by_others_is_refused(tmp_path: Path, mode: int) -> None:
    path = _token_file(tmp_path, mode=mode)
    settings = make_settings(
        environment="local",
        analytics_google_enabled=True,
        analytics_google_token_file=str(path),
        ga4_property_id=PROPERTY,
    )
    recorder = Recorder(httpx.Response(200, json={}))
    provider = Ga4AnalyticsProvider(settings, resolver=_resolver(), transport=recorder.transport())
    with pytest.raises(AnalyticsFetchError) as refused:
        await provider.fetch_batch(date_range=RANGE, datasets=LINKS)
    assert refused.value.code is FetchErrorCode.TOKEN_FILE_INVALID
    assert recorder.requests == []


async def test_a_missing_token_file_is_refused(tmp_path: Path) -> None:
    existing = _token_file(tmp_path)
    settings = make_settings(
        environment="local",
        analytics_google_enabled=True,
        analytics_google_token_file=str(existing),
        ga4_property_id=PROPERTY,
    )
    existing.unlink()
    provider = Ga4AnalyticsProvider(
        settings, resolver=_resolver(), transport=Recorder().transport()
    )
    with pytest.raises(AnalyticsFetchError) as refused:
        await provider.fetch_batch(date_range=RANGE, datasets=LINKS)
    assert refused.value.code is FetchErrorCode.TOKEN_FILE_INVALID


async def test_a_loopback_fixture_never_reads_the_token_file(tmp_path: Path) -> None:
    """Egress off plus a loopback base URL is the fixture mode: no token is read at all."""
    recorder = Recorder(_ga4_response([]))
    settings = make_settings(
        environment="local",
        analytics_google_enabled=False,
        analytics_ga4_base_url="http://127.0.0.1:9",
        analytics_google_token_file=str(tmp_path / "does-not-exist"),
        ga4_property_id=PROPERTY,
    )
    provider = Ga4AnalyticsProvider(settings, resolver=_resolver(), transport=recorder.transport())
    batch = await provider.fetch_batch(date_range=RANGE, datasets=LINKS)
    assert batch.link_rows == ()
    assert "Authorization" not in recorder.requests[0].headers


# --------------------------------------------------------------------------- #
# GA4 request shape
# --------------------------------------------------------------------------- #
async def test_the_ga4_link_request_is_exactly_the_documented_body(tmp_path: Path) -> None:
    recorder = Recorder(
        _ga4_response(
            [
                _ga4_link_row("20260511", EVENT_NAMES[0], str(PLAN), 100),
                _ga4_link_row("20260511", EVENT_NAMES[1], str(PLAN), 4),
            ]
        )
    )
    provider = Ga4AnalyticsProvider(
        _settings(tmp_path), resolver=_resolver(), transport=recorder.transport()
    )
    batch = await provider.fetch_batch(date_range=RANGE, datasets=LINKS)

    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == (f"{GA4_BASE_URL}/v1beta/properties/{PROPERTY}:runReport")
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    body = recorder.body()
    assert body["dateRanges"] == [{"startDate": "2026-05-11", "endDate": "2026-05-12"}]
    assert [item["name"] for item in body["dimensions"]] == [
        "date",
        "eventName",
        LINK_ID_DIMENSION,
    ]
    assert [item["name"] for item in body["metrics"]] == ["eventCount"]
    assert body["dimensionFilter"] == {
        "filter": {
            "fieldName": "eventName",
            "inListFilter": {"values": list(EVENT_NAMES)},
        }
    }
    assert len(EVENT_NAMES) == 2
    assert body["limit"] == GA4_LIMIT
    assert body["offset"] == 0
    # Nothing about Ken's inventory is sent: no plan id, no URL, no host.
    rendered = request.content.decode()
    assert str(PLAN) not in rendered
    assert "site.test" not in rendered

    assert len(batch.link_rows) == 1
    assert batch.link_rows[0].link_id == str(PLAN)
    assert batch.link_rows[0].impressions == 100
    assert batch.link_rows[0].clicks == 4
    assert batch.link_rows[0].source is MetricSource.GA4
    assert batch.link_rows[0].range.start == date(2026, 5, 11)


async def test_the_ga4_page_request_asks_for_host_and_path_only(tmp_path: Path) -> None:
    recorder = Recorder(_ga4_response([_ga4_page_row("20260512", "site.test", "/a", 42, 30)]))
    provider = Ga4AnalyticsProvider(
        _settings(tmp_path), resolver=_resolver(), transport=recorder.transport()
    )
    batch = await provider.fetch_batch(date_range=RANGE, datasets=PAGES)
    body = recorder.body()
    assert [item["name"] for item in body["dimensions"]] == ["date", "hostName", "pagePath"]
    assert [item["name"] for item in body["metrics"]] == ["screenPageViews", "sessions"]
    assert "dimensionFilter" not in body
    assert "site.test" not in recorder.requests[0].content.decode()
    assert batch.page_rows[0].page_id == str(PAGE_A)
    assert batch.page_rows[0].pageviews == 42
    assert batch.page_rows[0].sessions == 30


async def test_ga4_pages_with_offset_until_the_rows_run_out(tmp_path: Path) -> None:
    full = [
        _ga4_link_row("20260511", EVENT_NAMES[0], str(uuid.uuid4()), 1) for _ in range(GA4_LIMIT)
    ]
    recorder = Recorder(
        httpx.Response(200, json={"rows": full, "rowCount": GA4_LIMIT + 1}),
        _ga4_response([_ga4_link_row("20260512", EVENT_NAMES[0], str(PLAN), 7)]),
    )
    provider = Ga4AnalyticsProvider(
        _settings(tmp_path), resolver=_resolver(), transport=recorder.transport()
    )
    batch = await provider.fetch_batch(date_range=RANGE, datasets=LINKS)
    assert [recorder.body(index)["offset"] for index in range(2)] == [0, GA4_LIMIT]
    assert len(batch.link_rows) == GA4_LIMIT + 1
    assert batch.stats.rows_read == GA4_LIMIT + 1


async def test_ga4_other_rows_are_counted_and_sampling_is_recorded(tmp_path: Path) -> None:
    recorder = Recorder(
        _ga4_response(
            [
                _ga4_link_row("20260511", EVENT_NAMES[0], "(other)", 500),
                _ga4_link_row("20260511", EVENT_NAMES[0], str(PLAN), 10),
                _ga4_link_row("20260511", "some_other_event", str(PLAN), 10),
                {"dimensionValues": [{"value": "20260511"}], "metricValues": []},
            ],
            samplingMetadatas=[{"samplesReadCount": "1000", "samplingSpaceSize": "5000"}],
        )
    )
    provider = Ga4AnalyticsProvider(
        _settings(tmp_path), resolver=_resolver(), transport=recorder.transport()
    )
    batch = await provider.fetch_batch(date_range=RANGE, datasets=LINKS)
    assert batch.stats.other_row == 1
    assert batch.stats.rejected_invalid == 2
    assert batch.stats.sampled is True
    assert len(batch.link_rows) == 1


async def test_a_ga4_row_without_a_link_id_is_unresolved(tmp_path: Path) -> None:
    recorder = Recorder(_ga4_response([_ga4_link_row("20260511", EVENT_NAMES[0], "  ", 3)]))
    provider = Ga4AnalyticsProvider(
        _settings(tmp_path), resolver=_resolver(), transport=recorder.transport()
    )
    batch = await provider.fetch_batch(date_range=RANGE, datasets=LINKS)
    assert batch.link_rows == ()
    assert batch.stats.unresolved_link == 1


# --------------------------------------------------------------------------- #
# Search Console request shape
# --------------------------------------------------------------------------- #
async def test_the_search_console_request_is_exactly_the_documented_body(
    tmp_path: Path,
) -> None:
    recorder = Recorder(
        httpx.Response(
            200,
            json={
                "rows": [
                    {
                        "keys": ["2026-05-11", f"{SITE}/a"],
                        "clicks": 5.0,
                        "impressions": 100.0,
                        "ctr": 0.05,
                        "position": 3.5,
                    },
                    {
                        "keys": ["2026-05-11", f"{SITE}/unknown"],
                        "clicks": 1.0,
                        "impressions": 9.0,
                        "ctr": 0.1,
                        "position": 8.0,
                    },
                ]
            },
        )
    )
    provider = SearchConsoleAnalyticsProvider(
        _settings(tmp_path), resolver=_resolver(), transport=recorder.transport()
    )
    batch = await provider.fetch_batch(date_range=RANGE, datasets=PAGES)
    request = recorder.requests[0]
    assert str(request.url) == (
        f"{GSC_BASE_URL}/webmasters/v3/sites/sc-domain%3Asite.test/searchAnalytics/query"
    )
    assert recorder.body() == {
        "startDate": "2026-05-11",
        "endDate": "2026-05-12",
        "dimensions": ["date", "page"],
        "type": "web",
        "dataState": "final",
        "rowLimit": GSC_ROW_LIMIT,
        "startRow": 0,
    }
    assert "query" not in recorder.body()["dimensions"]  # query metrics are Phase 8
    assert len(batch.page_rows) == 1
    row = batch.page_rows[0]
    assert row.page_id == str(PAGE_A)
    assert (row.impressions, row.clicks, row.position) == (100, 5, 3.5)
    assert row.source is MetricSource.SEARCH_CONSOLE
    assert batch.stats.unresolved_url == 1
    assert GSC_TIMEZONE == "America/Los_Angeles"


async def test_search_console_pages_with_start_row(tmp_path: Path) -> None:
    full = [
        {
            "keys": ["2026-05-11", f"{SITE}/a"],
            "clicks": 1.0,
            "impressions": 2.0,
            "position": 4.0,
        }
        for _ in range(GSC_ROW_LIMIT)
    ]
    recorder = Recorder(
        httpx.Response(200, json={"rows": full}),
        httpx.Response(200, json={"rows": []}),
    )
    provider = SearchConsoleAnalyticsProvider(
        _settings(tmp_path), resolver=_resolver(), transport=recorder.transport()
    )
    await provider.fetch_batch(date_range=RANGE, datasets=PAGES)
    assert [recorder.body(index)["startRow"] for index in range(2)] == [0, GSC_ROW_LIMIT]


async def test_search_console_has_no_link_dataset_and_sends_nothing_for_it(
    tmp_path: Path,
) -> None:
    recorder = Recorder(httpx.Response(200, json={"rows": []}))
    provider = SearchConsoleAnalyticsProvider(
        _settings(tmp_path), resolver=_resolver(), transport=recorder.transport()
    )
    batch = await provider.fetch_batch(date_range=RANGE, datasets=LINKS)
    assert batch.link_rows == ()
    assert batch.page_rows == ()
    assert recorder.requests == []


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [401, 403])
async def test_an_auth_failure_is_one_fixed_code(tmp_path: Path, status: int) -> None:
    recorder = Recorder(httpx.Response(status, json={"error": {"message": "nope"}}))
    provider = Ga4AnalyticsProvider(
        _settings(tmp_path), resolver=_resolver(), transport=recorder.transport()
    )
    with pytest.raises(AnalyticsFetchError) as refused:
        await provider.fetch_batch(date_range=RANGE, datasets=LINKS)
    assert refused.value.code is FetchErrorCode.GOOGLE_AUTH_FAILED
    assert str(refused.value) == "google_auth_failed"
    assert len(recorder.requests) == 1  # never retried


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_a_retryable_status_is_tried_three_times_then_unavailable(
    tmp_path: Path, status: int
) -> None:
    recorder = Recorder(httpx.Response(status, json={}))
    provider = Ga4AnalyticsProvider(
        _settings(tmp_path), resolver=_resolver(), transport=recorder.transport()
    )
    provider._egress._sleep = _no_sleep
    with pytest.raises(AnalyticsFetchError) as refused:
        await provider.fetch_batch(date_range=RANGE, datasets=LINKS)
    assert refused.value.code is FetchErrorCode.GOOGLE_UNAVAILABLE
    assert len(recorder.requests) == GOOGLE_TRIES == 3


async def test_a_retry_that_succeeds_returns_the_rows(tmp_path: Path) -> None:
    recorder = Recorder(
        httpx.Response(429, json={}),
        _ga4_response([_ga4_link_row("20260511", EVENT_NAMES[0], str(PLAN), 2)]),
    )
    provider = Ga4AnalyticsProvider(
        _settings(tmp_path), resolver=_resolver(), transport=recorder.transport()
    )
    provider._egress._sleep = _no_sleep
    batch = await provider.fetch_batch(date_range=RANGE, datasets=LINKS)
    assert len(batch.link_rows) == 1
    assert len(recorder.requests) == 2


async def test_a_non_json_body_is_unavailable(tmp_path: Path) -> None:
    recorder = Recorder(httpx.Response(200, content=b"<html>not json</html>"))
    provider = Ga4AnalyticsProvider(
        _settings(tmp_path), resolver=_resolver(), transport=recorder.transport()
    )
    with pytest.raises(AnalyticsFetchError) as refused:
        await provider.fetch_batch(date_range=RANGE, datasets=LINKS)
    assert refused.value.code is FetchErrorCode.GOOGLE_UNAVAILABLE


async def test_a_redirect_is_never_followed(tmp_path: Path) -> None:
    recorder = Recorder(
        httpx.Response(302, headers={"location": "https://evil.test/steal"}, json={})
    )
    provider = Ga4AnalyticsProvider(
        _settings(tmp_path), resolver=_resolver(), transport=recorder.transport()
    )
    with pytest.raises(AnalyticsFetchError) as refused:
        await provider.fetch_batch(date_range=RANGE, datasets=LINKS)
    assert refused.value.code is FetchErrorCode.GOOGLE_UNAVAILABLE
    assert len(recorder.requests) == 1
    assert [str(request.url) for request in recorder.requests] == [
        f"{GA4_BASE_URL}/v1beta/properties/{PROPERTY}:runReport"
    ]


async def test_the_token_never_reaches_a_log_a_counter_or_an_error(tmp_path: Path) -> None:
    recorder = Recorder(httpx.Response(429, json={}))
    provider = Ga4AnalyticsProvider(
        _settings(tmp_path), resolver=_resolver(), transport=recorder.transport()
    )
    provider._egress._sleep = _no_sleep
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(AnalyticsFetchError) as refused,
    ):
        await provider.fetch_batch(date_range=RANGE, datasets=LINKS)
    assert TOKEN not in str(refused.value)
    assert TOKEN not in json.dumps(logs, default=str)
    assert logs, "the retries must be logged (without the token)"
    assert all("token" not in json.dumps(entry, default=str).lower() for entry in logs)


async def test_health_is_safe_when_egress_is_disabled() -> None:
    settings = make_settings(ga4_property_id=PROPERTY, gsc_site_url=SITE_PROPERTY)
    for provider in (
        Ga4AnalyticsProvider(settings, resolver=_resolver()),
        SearchConsoleAnalyticsProvider(settings, resolver=_resolver()),
    ):
        health = await provider.health()
        assert health.reachable is False
        assert health.error == "google egress is disabled"
        await provider.aclose()


async def _no_sleep(_seconds: float) -> None:
    return None
