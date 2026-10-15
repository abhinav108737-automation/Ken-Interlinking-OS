"""The frozen /analytics routes over a fake reader (no database): every route delegates to the
``AnalyticsReader`` with the parsed parameters, ``read`` is enough (viewer), the range rule
(both bounds or neither, ordered, at most 400 days) and the limits are 422s, and unknown plans
or pages are 404s through the existing typed errors (ADR-0015 s6 / s12.2)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ken_api.deps import get_analytics_reader
from ken_contracts.analytics import (
    AnalyticsReader,
    AnalyticsSource,
    AnalyticsSummary,
    DateRange,
    LinkSort,
    SortOrder,
)
from ken_contracts.auth import Role
from ken_contracts.errors import ListResponse, PageInfo
from ken_contracts.inventory import InventoryNotFoundError
from ken_contracts.recommendations import LinkPlanStatus, PlanNotFoundError
from tests.conftest import make_app

PLAN = uuid.UUID("11111111-1111-4111-8111-111111111111")
PAGE = uuid.UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def _summary(date_range: DateRange | None) -> AnalyticsSummary:
    return AnalyticsSummary(
        range=date_range or DateRange(start=date(2026, 9, 1), end=date(2026, 9, 28)),
        sources=[],
        totals_by_source={},
        daily=[],
        live_links=0,
        links_with_data=0,
        top_links=[],
        synthetic=False,
    )


def _empty(limit: int, offset: int) -> ListResponse[Any]:
    return ListResponse[Any](items=[], page=PageInfo(total=0, limit=limit, offset=offset))


class FakeReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.raises: BaseException | None = None

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if self.raises is not None:
            raise self.raises

    async def summary(self, *, date_range: DateRange | None) -> AnalyticsSummary:
        self._record("summary", {"date_range": date_range})
        return _summary(date_range)

    async def list_links(self, **kwargs: Any) -> ListResponse[Any]:
        self._record("list_links", kwargs)
        return _empty(kwargs["limit"], kwargs["offset"])

    async def get_link(self, **kwargs: Any) -> Any:
        self._record("get_link", kwargs)
        raise AssertionError("not reached in these tests")

    async def page_impact(self, **kwargs: Any) -> Any:
        self._record("page_impact", kwargs)
        raise AssertionError("not reached in these tests")

    async def list_imports(self, **kwargs: Any) -> ListResponse[Any]:
        self._record("list_imports", kwargs)
        return _empty(kwargs["limit"], kwargs["offset"])


@pytest.fixture
def reader() -> FakeReader:
    return FakeReader()


def _client(reader: FakeReader, role: Role = Role.VIEWER) -> TestClient:
    app = make_app(role=role)
    app.dependency_overrides[get_analytics_reader] = lambda: reader
    return TestClient(app, raise_server_exceptions=False)


def test_the_fake_satisfies_the_frozen_reader_protocol(reader: FakeReader) -> None:
    assert isinstance(reader, AnalyticsReader)


def test_summary_without_a_range_asks_for_the_default(reader: FakeReader) -> None:
    response = _client(reader).get("/analytics/summary")
    assert response.status_code == 200, response.text
    assert reader.calls == [("summary", {"date_range": None})]


def test_summary_passes_an_explicit_range(reader: FakeReader) -> None:
    response = _client(reader).get(
        "/analytics/summary", params={"start": "2026-09-01", "end": "2027-10-05"}
    )
    assert response.status_code == 200, response.text
    assert reader.calls == [
        ("summary", {"date_range": DateRange(start=date(2026, 9, 1), end=date(2027, 10, 5))})
    ]


@pytest.mark.parametrize(
    "params",
    [
        {"start": "2026-09-01"},
        {"end": "2026-09-01"},
        {"start": "2026-09-02", "end": "2026-09-01"},
        {"start": "2026-09-01", "end": "2027-10-06"},  # 401 days
        {"start": "2026-02-30", "end": "2026-03-01"},
    ],
)
@pytest.mark.parametrize(
    "path", ["/analytics/summary", "/analytics/links", f"/analytics/links/{PLAN}"]
)
def test_an_invalid_range_is_a_422(reader: FakeReader, path: str, params: dict[str, str]) -> None:
    response = _client(reader).get(path, params=params)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"
    assert reader.calls == []


def test_list_links_passes_every_filter(reader: FakeReader) -> None:
    response = _client(reader).get(
        "/analytics/links",
        params={
            "start": "2026-09-01",
            "end": "2026-09-28",
            "source": "first_party",
            "source_page_id": str(PAGE),
            "plan_status": "verified",
            "sort": "ctr",
            "order": "asc",
            "limit": "100",
            "offset": "5",
        },
    )
    assert response.status_code == 200, response.text
    assert reader.calls == [
        (
            "list_links",
            {
                "date_range": DateRange(start=date(2026, 9, 1), end=date(2026, 9, 28)),
                "limit": 100,
                "offset": 5,
                "source": AnalyticsSource.FIRST_PARTY,
                "source_page_id": PAGE,
                "target_page_id": None,
                "deployment_id": None,
                "plan_status": LinkPlanStatus.VERIFIED,
                "sort": LinkSort.CTR,
                "order": SortOrder.ASC,
            },
        )
    ]


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/analytics/links", {"limit": "101"}),
        ("/analytics/links", {"limit": "0"}),
        ("/analytics/links", {"source": "csv"}),
        ("/analytics/links", {"sort": "landings"}),
        ("/analytics/imports", {"limit": "201"}),
        ("/analytics/imports", {"dataset": "queries"}),
        (f"/analytics/pages/{PAGE}", {"window_days": "6"}),
        (f"/analytics/pages/{PAGE}", {"window_days": "91"}),
        ("/analytics/links/not-a-uuid", {}),
    ],
)
def test_parameter_bounds_are_422s(reader: FakeReader, path: str, params: dict[str, str]) -> None:
    response = _client(reader).get(path, params=params)
    assert response.status_code == 422, response.text
    assert reader.calls == []


def test_imports_default_paging(reader: FakeReader) -> None:
    response = _client(reader).get("/analytics/imports")
    assert response.status_code == 200, response.text
    assert reader.calls == [
        ("list_imports", {"limit": 50, "offset": 0, "source": None, "dataset": None})
    ]


def test_an_unknown_plan_is_a_404(reader: FakeReader) -> None:
    reader.raises = PlanNotFoundError()
    response = _client(reader).get(f"/analytics/links/{PLAN}")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert reader.calls == [("get_link", {"plan_id": PLAN, "date_range": None})]


def test_an_unknown_page_is_a_404(reader: FakeReader) -> None:
    reader.raises = InventoryNotFoundError("page")
    response = _client(reader).get(f"/analytics/pages/{PAGE}", params={"window_days": "14"})
    assert response.status_code == 404
    assert response.json()["code"] == "page_not_found"
    assert reader.calls == [("page_impact", {"page_id": PAGE, "window_days": 14})]
