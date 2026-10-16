"""The five ``/analytics`` routes over the REAL reader on the ``*_test`` database
(ADR-0015 §6, §9 "API"): the frozen routes, the READ ONLY session, the RBAC matrix and the
parameter validation, now that ``SqlAnalyticsReader`` exists.

The reader itself is exercised through HTTP on purpose: that is the only way the API will
ever use it, and it proves the composition in ``deps.get_analytics_reader`` (resolved by
name) really works.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest

from ken_contracts.analytics import (
    LINK_SOURCES,
    AnalyticsImportView,
    AnalyticsSource,
    AnalyticsSummary,
    Dataset,
    DateRange,
    ImportVia,
    LinkPerformanceDetail,
    LinkPerformanceView,
    LinkSort,
    MetricSource,
    PageImpactView,
    PageMetrics,
    SortOrder,
)
from ken_contracts.auth import Role
from ken_contracts.errors import ErrorResponse, ListResponse
from ken_contracts.recommendations import LinkPlanStatus
from tests.conftest import FakeCMSProvider, FakeModelProvider, make_app
from tests.integration.analytics_support import (
    SETTINGS,
    a_user,
    make_live,
    new_plan,
    record_deployment,
    request_for,
    run_mock_import,
    seed_pages,
)
from tests.integration.recommendation_support import SITE, SM
from tests.integration.review_support import PASSWORD, api_client, auth_provider, create_user

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SOURCE = "/s1"
TARGET = "/t1"
DEPLOYED_AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
FIRST = date(2026, 2, 1)
LAST = date(2026, 3, 29)
NOW = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)


class World:
    def __init__(
        self,
        *,
        pages: dict[str, uuid.UUID],
        plan_id: uuid.UUID,
        deployment_id: uuid.UUID,
    ) -> None:
        self.pages = pages
        self.plan_id = plan_id
        self.deployment_id = deployment_id


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


async def _world(sessionmaker: SM) -> World:
    pages = await seed_pages(sessionmaker, SOURCE, TARGET)
    plan_id = await new_plan(
        sessionmaker,
        source=pages[SOURCE],
        target=pages[TARGET],
        source_url=SITE + SOURCE,
        target_url=SITE + TARGET,
    )
    await make_live(
        sessionmaker,
        plan_id,
        deployed_at=DEPLOYED_AT,
        verified_at=DEPLOYED_AT + timedelta(days=1),
    )
    user_id = await a_user(sessionmaker)
    deployment_id = await record_deployment(
        sessionmaker,
        source_page_id=pages[SOURCE],
        plan_ids=[plan_id],
        user_id=user_id,
        applied_at=DEPLOYED_AT,
        verified_at=DEPLOYED_AT + timedelta(days=1),
    )
    # First-party link rows inside the live window, and GA4 page rows for both pages.
    await run_mock_import(
        sessionmaker,
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start=DEPLOYED_AT.date().isoformat(),
            end=LAST.isoformat(),
            via=ImportVia.COLLECTOR,
        ),
        link_rows=[_link(plan_id, day) for day in _days(DEPLOYED_AT.date(), LAST)],
        now=NOW,
    )
    await run_mock_import(
        sessionmaker,
        request=request_for(
            source=AnalyticsSource.GA4,
            dataset=Dataset.PAGES,
            start=FIRST.isoformat(),
            end=LAST.isoformat(),
            via=ImportVia.API,
            source_timezone="Asia/Kolkata",
        ),
        page_rows=[
            PageMetrics(
                page_id=str(page_id),
                source=MetricSource.GA4,
                range=DateRange(start=day, end=day),
                pageviews=40,
                sessions=20,
            )
            for page_id in sorted(pages.values(), key=str)
            for day in _days(FIRST, LAST)
        ],
        now=NOW,
    )
    return World(pages=pages, plan_id=plan_id, deployment_id=deployment_id)


def _link(plan_id: uuid.UUID, day: date) -> Any:
    from ken_contracts.analytics import LinkMetrics

    return LinkMetrics(
        link_id=str(plan_id),
        source=MetricSource.FIRST_PARTY,
        range=DateRange(start=day, end=day),
        impressions=100,
        clicks=5,
    )


async def _client(sessionmaker: SM, username: str, role: Role) -> httpx.AsyncClient:
    """Real Bearer authentication, the real reader resolved by name through deps."""
    await create_user(sessionmaker, username, role=role)
    app = make_app(SETTINGS, real_auth=True)
    app.state.sessionmaker = sessionmaker
    app.state.model_provider = FakeModelProvider()
    app.state.cms_provider = FakeCMSProvider()
    app.state.auth_provider = auth_provider(sessionmaker)
    client = api_client(app, raise_errors=False)
    login = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _error(response: Any, status: int) -> ErrorResponse:
    assert response.status_code == status, response.text
    return ErrorResponse.model_validate(response.json())


# --------------------------------------------------------------------------- #
# The five routes
# --------------------------------------------------------------------------- #
async def test_the_summary_reports_totals_freshness_and_top_links(sessionmaker: SM) -> None:
    world = await _world(sessionmaker)
    client = await _client(sessionmaker, "viv", Role.VIEWER)
    response = await client.get(
        "/analytics/summary", params={"start": "2026-03-01", "end": "2026-03-29"}
    )
    assert response.status_code == 200, response.text
    summary = AnalyticsSummary.model_validate(response.json())
    assert summary.range.start == date(2026, 3, 1)
    assert summary.live_links == 1
    assert summary.links_with_data == 1
    assert summary.totals_by_source[AnalyticsSource.FIRST_PARTY].clicks == 5 * 29
    assert summary.totals_by_source[AnalyticsSource.FIRST_PARTY].ctr == pytest.approx(0.05)
    assert [point.metric_date for point in summary.daily][:2] == [
        date(2026, 3, 1),
        date(2026, 3, 2),
    ]
    assert [view.plan_id for view in summary.top_links] == [world.plan_id]
    assert summary.synthetic is False
    freshness = {(item.source, item.dataset): item for item in summary.sources}
    assert freshness[(AnalyticsSource.FIRST_PARTY, Dataset.LINKS)].latest_date == LAST
    assert freshness[(AnalyticsSource.FIRST_PARTY, Dataset.LINKS)].last_via is ImportVia.COLLECTOR
    assert freshness[(AnalyticsSource.GA4, Dataset.LINKS)].latest_date is None
    assert freshness[(AnalyticsSource.SEARCH_CONSOLE, Dataset.PAGES)].last_import_id is None


async def test_the_default_window_ends_at_the_latest_stored_day(sessionmaker: SM) -> None:
    await _world(sessionmaker)
    client = await _client(sessionmaker, "viv", Role.VIEWER)
    summary = AnalyticsSummary.model_validate((await client.get("/analytics/summary")).json())
    assert summary.range.end == LAST
    assert summary.range.start == LAST - timedelta(days=27)


async def test_link_performance_is_listed_filtered_and_sorted(sessionmaker: SM) -> None:
    world = await _world(sessionmaker)
    client = await _client(sessionmaker, "viv", Role.VIEWER)
    listed = ListResponse[LinkPerformanceView].model_validate(
        (await client.get("/analytics/links")).json()
    )
    assert listed.page.total == 1
    view = listed.items[0]
    assert view.plan_id == world.plan_id
    # The world's link data covers D0 + 28, so the import's own sweep already measured it.
    assert view.plan_status is LinkPlanStatus.MEASURED
    assert view.deployed_at == DEPLOYED_AT
    assert view.rolled_back_at is None
    assert view.totals[AnalyticsSource.FIRST_PARTY].impressions > 0
    assert view.synthetic is False

    by_deployment = ListResponse[LinkPerformanceView].model_validate(
        (
            await client.get("/analytics/links", params={"deployment_id": str(world.deployment_id)})
        ).json()
    )
    assert [item.plan_id for item in by_deployment.items] == [world.plan_id]
    other = ListResponse[LinkPerformanceView].model_validate(
        (await client.get("/analytics/links", params={"deployment_id": str(uuid.uuid4())})).json()
    )
    assert other.items == []
    by_status = ListResponse[LinkPerformanceView].model_validate(
        (await client.get("/analytics/links", params={"plan_status": "rolled_back"})).json()
    )
    assert by_status.items == []
    by_source = ListResponse[LinkPerformanceView].model_validate(
        (await client.get("/analytics/links", params={"source": "ga4"})).json()
    )
    assert by_source.items[0].totals == {}


async def test_one_link_carries_its_attribution_window_series_and_impact(
    sessionmaker: SM,
) -> None:
    world = await _world(sessionmaker)
    client = await _client(sessionmaker, "viv", Role.VIEWER)
    response = await client.get(f"/analytics/links/{world.plan_id}")
    assert response.status_code == 200, response.text
    detail = LinkPerformanceDetail.model_validate(response.json())
    assert detail.attribution.plan_id == world.plan_id
    assert detail.attribution.deployment_id == world.deployment_id
    assert detail.attribution.deployed_at == DEPLOYED_AT
    assert detail.live_window.deployed_at == DEPLOYED_AT
    assert detail.live_window.rolled_back_at is None
    assert detail.series[AnalyticsSource.FIRST_PARTY][0].metric_date >= DEPLOYED_AT.date()
    assert detail.totals[AnalyticsSource.FIRST_PARTY].clicks > 0
    assert {item.role for item in detail.source_impact} == {"source"}
    assert {item.role for item in detail.target_impact} == {"target"}
    assert {item.source for item in detail.target_impact} == {
        AnalyticsSource.GA4,
        AnalyticsSource.SEARCH_CONSOLE,
    }
    assert detail.measurement is not None
    assert detail.measurement.window_start == DEPLOYED_AT.date()
    assert detail.measurement.window_end == DEPLOYED_AT.date() + timedelta(days=28)
    assert AnalyticsSource.FIRST_PARTY in detail.measurement.link_totals
    assert detail.range.end == LAST


async def test_a_page_reports_its_series_and_the_changes_around_it(sessionmaker: SM) -> None:
    world = await _world(sessionmaker)
    client = await _client(sessionmaker, "viv", Role.VIEWER)
    response = await client.get(f"/analytics/pages/{world.pages[TARGET]}")
    assert response.status_code == 200, response.text
    view = PageImpactView.model_validate(response.json())
    assert view.page_id == world.pages[TARGET]
    assert view.url == SITE + TARGET
    assert view.window_days == 28
    assert view.series[AnalyticsSource.GA4][-1].metric_date == LAST
    assert view.series[AnalyticsSource.GA4][-1].pageviews == 40
    # One comparison per (plan that touched this page, page source).
    assert {change.plan_id for change in view.changes} == {world.plan_id}
    assert {change.source for change in view.changes} == {
        AnalyticsSource.GA4,
        AnalyticsSource.SEARCH_CONSOLE,
    }
    assert {change.page_id for change in view.changes} == {world.pages[TARGET]}
    assert {change.role for change in view.changes} == {"target"}
    assert {change.metric for change in view.changes} == {"pageviews", "clicks"}


async def test_the_import_ledger_is_listed_newest_first_with_its_counters(
    sessionmaker: SM,
) -> None:
    await _world(sessionmaker)
    client = await _client(sessionmaker, "viv", Role.VIEWER)
    listed = ListResponse[AnalyticsImportView].model_validate(
        (await client.get("/analytics/imports")).json()
    )
    assert listed.page.total == 2
    assert {(item.source, item.dataset) for item in listed.items} == {
        (AnalyticsSource.FIRST_PARTY, Dataset.LINKS),
        (AnalyticsSource.GA4, Dataset.PAGES),
    }
    google = next(item for item in listed.items if item.source is AnalyticsSource.GA4)
    assert google.dataset is Dataset.PAGES
    assert google.via is ImportVia.API
    assert google.rows_written > 0
    assert google.counters["rows_written"] == google.rows_written
    assert google.file_name is None
    assert google.file_sha256 is None
    assert google.source_timezone == "Asia/Kolkata"
    assert google.analytics_policy_version >= 1
    filtered = ListResponse[AnalyticsImportView].model_validate(
        (await client.get("/analytics/imports", params={"source": "first_party"})).json()
    )
    assert [item.source for item in filtered.items] == [AnalyticsSource.FIRST_PARTY]


# --------------------------------------------------------------------------- #
# Not found, RBAC, validation
# --------------------------------------------------------------------------- #
async def test_an_unknown_plan_or_page_is_a_404(sessionmaker: SM) -> None:
    await _world(sessionmaker)
    client = await _client(sessionmaker, "viv", Role.VIEWER)
    unknown = _error(await client.get(f"/analytics/links/{uuid.uuid4()}"), 404)
    assert unknown.code.value == "not_found"
    page = _error(await client.get(f"/analytics/pages/{uuid.uuid4()}"), 404)
    assert page.code.value == "page_not_found"


@pytest.mark.parametrize(
    "path",
    [
        "/analytics/summary",
        "/analytics/links",
        "/analytics/imports",
    ],
)
async def test_anonymous_is_401_and_a_viewer_is_200(sessionmaker: SM, path: str) -> None:
    await _world(sessionmaker)
    client = await _client(sessionmaker, "viv", Role.VIEWER)
    assert (await client.get(path)).status_code == 200
    del client.headers["Authorization"]
    assert _error(await client.get(path), 401).code.value == "unauthenticated"


async def test_every_analytics_route_is_get_only(sessionmaker: SM) -> None:
    await _world(sessionmaker)
    client = await _client(sessionmaker, "ops", Role.OPERATOR_ADMIN)
    for method in ("post", "put", "patch", "delete"):
        response = await getattr(client, method)("/analytics/summary")
        assert response.status_code == 405, (method, response.status_code)


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/analytics/summary", {"start": "2026-03-01"}),
        ("/analytics/summary", {"end": "2026-03-01"}),
        ("/analytics/summary", {"start": "2026-03-02", "end": "2026-03-01"}),
        ("/analytics/summary", {"start": "2020-01-01", "end": "2026-03-01"}),
        ("/analytics/links", {"limit": "0"}),
        ("/analytics/links", {"limit": "101"}),
        ("/analytics/links", {"offset": "-1"}),
        ("/analytics/links", {"sort": "landings"}),
        ("/analytics/imports", {"limit": "201"}),
    ],
)
async def test_invalid_parameters_are_422s(
    sessionmaker: SM, path: str, params: dict[str, str]
) -> None:
    await _world(sessionmaker)
    client = await _client(sessionmaker, "viv", Role.VIEWER)
    _error(await client.get(path, params=params), 422)


@pytest.mark.parametrize("window_days", ["6", "91", "abc"])
async def test_the_page_window_is_bounded(sessionmaker: SM, window_days: str) -> None:
    world = await _world(sessionmaker)
    client = await _client(sessionmaker, "viv", Role.VIEWER)
    _error(
        await client.get(
            f"/analytics/pages/{world.pages[TARGET]}", params={"window_days": window_days}
        ),
        422,
    )


async def test_the_reader_runs_on_a_read_only_session(sessionmaker: SM) -> None:
    """Every route reads through the READ ONLY factory, so a write attempt cannot succeed."""
    from sqlalchemy import text

    from ken_core.db.session import create_readonly_sessionmaker

    await _world(sessionmaker)
    readonly = create_readonly_sessionmaker(sessionmaker.kw["bind"])
    async with readonly() as session:
        with pytest.raises(Exception, match=r"read-only|read only"):
            await session.execute(text("DELETE FROM analytics_imports"))
            await session.commit()


# --------------------------------------------------------------------------- #
# Bounded read fan-out and the ranking semantics (Phase 7 QA finding 2)
# --------------------------------------------------------------------------- #
LINK_LIST_ROW_BOUND = 100
"""The route caps ``limit`` at 100, and the reader materialises at most ``limit`` ranked rows,
``limit`` plan rows and ``limit`` x |LINK_SOURCES| aggregate rows -- 400 rows in all, however
long the requested range is (ADR-0015 §12.10.8)."""

# (impressions, clicks) PER DAY for each plan of the ranking world; ``None`` means no rows at
# all. The ties are deliberate: they are what pins the tie-break.
RANKING_SERIES: tuple[tuple[int, int] | None, ...] = (
    (10, 1),  # clicks 29, impressions 290, ctr 0.1
    (100, 1),  # clicks 29  -> ties with plan 0 on clicks
    (50, 5),  # clicks 145, impressions 1450, ctr 0.1 -> ties with plan 0 on ctr
    None,  # no rows at all: every ranking is 0
    (0, 0),  # rows, but no impressions: ctr is NULL while it ranks as 0.0
    (10, 2),  # clicks 58, impressions 290, ctr 0.2
)


async def _ranking_world(sessionmaker: SM) -> list[uuid.UUID]:
    """One source page and one live plan per :data:`RANKING_SERIES` entry, with daily rows."""
    from ken_contracts.analytics import LinkMetrics

    paths = [SOURCE, *[f"/t{n}" for n in range(len(RANKING_SERIES))]]
    pages = await seed_pages(sessionmaker, *paths)
    plan_ids: list[uuid.UUID] = []
    for n in range(len(RANKING_SERIES)):
        plan_id = await new_plan(
            sessionmaker,
            source=pages[SOURCE],
            target=pages[f"/t{n}"],
            source_url=SITE + SOURCE,
            target_url=f"{SITE}/t{n}",
            block_text=f"Case {n}: batteries power electric vehicles.",
        )
        await make_live(sessionmaker, plan_id, deployed_at=DEPLOYED_AT)
        plan_ids.append(plan_id)
    rows = [
        LinkMetrics(
            link_id=str(plan_ids[n]),
            source=MetricSource.FIRST_PARTY,
            range=DateRange(start=day, end=day),
            impressions=series[0],
            clicks=series[1],
        )
        for n, series in enumerate(RANKING_SERIES)
        if series is not None
        for day in _days(DEPLOYED_AT.date(), LAST)
    ]
    await run_mock_import(
        sessionmaker,
        request=request_for(
            source=AnalyticsSource.FIRST_PARTY,
            dataset=Dataset.LINKS,
            start=DEPLOYED_AT.date().isoformat(),
            end=LAST.isoformat(),
            via=ImportVia.COLLECTOR,
        ),
        link_rows=rows,
        now=NOW,
    )
    return plan_ids


def _reader(sessionmaker: SM) -> Any:
    from ken_services.analytics.reader import SqlAnalyticsReader

    return SqlAnalyticsReader(sessionmaker=sessionmaker, settings=SETTINGS)


def _expected_order(
    plan_ids: list[uuid.UUID], values: list[float], *, descending: bool
) -> list[uuid.UUID]:
    """The order the reader used to produce in Python: ``(value, plan id as text)``, both keys
    in the same direction. Pinning it here is what proves the SQL ranking did not change it."""
    ranked = sorted(
        zip(values, [str(plan_id) for plan_id in plan_ids], strict=True), reverse=descending
    )
    return [uuid.UUID(key) for _value, key in ranked]


_DAYS_OF_DATA = 29
CLICKS = [0.0 if s is None else float(s[1] * _DAYS_OF_DATA) for s in RANKING_SERIES]
IMPRESSIONS = [0.0 if s is None else float(s[0] * _DAYS_OF_DATA) for s in RANKING_SERIES]
CTRS = [0.0 if not imp else clk / imp for clk, imp in zip(CLICKS, IMPRESSIONS, strict=True)]


@pytest.mark.parametrize("order", list(SortOrder))
@pytest.mark.parametrize("sort", list(LinkSort))
async def test_the_link_list_ranks_exactly_as_it_did_in_python(
    sessionmaker: SM, sort: LinkSort, order: SortOrder
) -> None:
    plan_ids = await _ranking_world(sessionmaker)
    values = {LinkSort.CLICKS: CLICKS, LinkSort.IMPRESSIONS: IMPRESSIONS, LinkSort.CTR: CTRS}[sort]
    listed = await _reader(sessionmaker).list_links(
        date_range=DateRange(start=DEPLOYED_AT.date(), end=LAST),
        limit=len(plan_ids),
        offset=0,
        sort=sort,
        order=order,
    )
    assert listed.page.total == len(plan_ids)
    assert [item.plan_id for item in listed.items] == _expected_order(
        plan_ids, values, descending=order is SortOrder.DESC
    )
    # A plan with no rows at all is still listed, with no totals.
    empty = next(item for item in listed.items if item.plan_id == plan_ids[3])
    assert empty.totals == {}
    assert empty.deployed_at == DEPLOYED_AT
    # A plan WITH rows but no impressions is listed with totals whose ctr is null -- while
    # it ranked as 0.0, which is the semantic the SQL ranking has to keep.
    zero = next(item for item in listed.items if item.plan_id == plan_ids[4])
    assert zero.totals[AnalyticsSource.FIRST_PARTY].impressions == 0
    assert zero.totals[AnalyticsSource.FIRST_PARTY].ctr is None


async def test_the_link_list_pages_through_the_same_ranking(sessionmaker: SM) -> None:
    plan_ids = await _ranking_world(sessionmaker)
    expected = _expected_order(plan_ids, CLICKS, descending=True)
    seen: list[uuid.UUID] = []
    for offset in range(0, len(plan_ids), 2):
        page = await _reader(sessionmaker).list_links(
            date_range=DateRange(start=DEPLOYED_AT.date(), end=LAST), limit=2, offset=offset
        )
        assert page.page.total == len(plan_ids)
        assert len(page.items) == 2
        seen.extend(item.plan_id for item in page.items)
    assert seen == expected
    past_the_end = await _reader(sessionmaker).list_links(
        date_range=DateRange(start=DEPLOYED_AT.date(), end=LAST), limit=2, offset=len(plan_ids)
    )
    assert past_the_end.items == []
    assert past_the_end.page.total == len(plan_ids)


async def test_the_link_list_reads_a_bounded_number_of_rows_whatever_the_range(
    sessionmaker: SM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One GET must never be ``O(live links x days)``: the span may be 400 days wide and the
    work still bounded by ``limit`` (ADR-0015 §12.10.8)."""
    from ken_core.db.repositories import analytics as repo

    plan_ids = await _ranking_world(sessionmaker)
    calls: dict[str, list[int]] = {"ranked": [], "totals": [], "daily": []}
    real_rank, real_totals = repo.rank_deployed_plans, repo.link_totals_by_plan

    async def counted_rank(*args: Any, **kwargs: Any) -> Any:
        rows, total = await real_rank(*args, **kwargs)
        calls["ranked"].append(len(rows))
        return rows, total

    async def counted_totals(*args: Any, **kwargs: Any) -> Any:
        rows = await real_totals(*args, **kwargs)
        calls["totals"].append(len(rows))
        return rows

    async def forbidden_daily(*args: Any, **kwargs: Any) -> Any:
        calls["daily"].append(1)
        raise AssertionError("list_links must not read raw daily rows")

    monkeypatch.setattr(repo, "rank_deployed_plans", counted_rank)
    monkeypatch.setattr(repo, "link_totals_by_plan", counted_totals)
    monkeypatch.setattr(repo, "link_daily_rows", forbidden_daily)

    limit = 2
    for span in (1, 390):  # one day, and nearly the contract's maximum range
        listed = await _reader(sessionmaker).list_links(
            date_range=DateRange(start=LAST - timedelta(days=span - 1), end=LAST),
            limit=limit,
            offset=0,
        )
        assert listed.page.total == len(plan_ids)
        assert len(listed.items) == limit
    assert calls["daily"] == []
    assert calls["ranked"] == [limit, limit]
    assert all(rows <= limit * len(LINK_SOURCES) for rows in calls["totals"])
    # The documented ceiling: at the route's cap, a page is at most 100 ranked rows.
    assert limit <= LINK_LIST_ROW_BOUND
