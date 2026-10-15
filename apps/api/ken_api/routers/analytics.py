"""Phase 7 analytics API (ADR-0015 section 6 as amended by section 12). FROZEN SIGNATURES
(orchestrator). GET-only, all ``read``: imports are CLI-only, so no UI action can ever trigger
Google egress. Every body delegates to the :class:`~ken_contracts.analytics.AnalyticsReader`
(READ ONLY sessions). Route functions carry no docstrings on purpose."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Path, Query
from fastapi.exceptions import RequestValidationError

from ken_api.deps import AnalyticsReaderDep
from ken_api.security import Reader
from ken_contracts.analytics import (
    MAX_RANGE_DAYS,
    AnalyticsImportView,
    AnalyticsSource,
    AnalyticsSummary,
    Dataset,
    DateRange,
    LinkPerformanceDetail,
    LinkPerformanceView,
    LinkSort,
    PageImpactView,
    SortOrder,
)
from ken_contracts.errors import ErrorResponse, ListResponse
from ken_contracts.recommendations import LinkPlanStatus

router = APIRouter(tags=["analytics"])

_AUTH: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorResponse, "description": "Not authenticated."},
    403: {"model": ErrorResponse, "description": "The role lacks the permission."},
    422: {"model": ErrorResponse, "description": "A parameter is invalid."},
}
Start = Annotated[
    date | None, Query(description="First day (inclusive); give both start and end, or neither.")
]
End = Annotated[date | None, Query(description="Last day (inclusive); span at most 400 days.")]


def query_range(start: date | None, end: date | None) -> DateRange | None:
    """``None`` (the reader's default window) when neither bound is given; else an ordered
    range of at most ``MAX_RANGE_DAYS`` days. Anything else is a 422 (fixed messages)."""
    if start is None and end is None:
        return None
    problem: str | None = None
    if start is None or end is None:
        problem = "Give both start and end, or neither."
    elif end < start:
        problem = "end precedes start."
    elif (end - start).days >= MAX_RANGE_DAYS:
        problem = f"The range spans more than {MAX_RANGE_DAYS} days."
    if problem is not None:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "end"), "msg": problem}]
        )
    assert start is not None and end is not None  # noqa: S101 -- narrowed above
    return DateRange(start=start, end=end)


@router.get(
    "/analytics/summary",
    operation_id="getAnalyticsSummary",
    summary="Link-level totals, daily series, top links and source freshness.",
    response_model=AnalyticsSummary,
    responses=_AUTH,
)
async def summary(
    principal: Reader, reader: AnalyticsReaderDep, start: Start = None, end: End = None
) -> AnalyticsSummary:
    return await reader.summary(date_range=query_range(start, end))


@router.get(
    "/analytics/links",
    operation_id="listLinkPerformance",
    summary="Per-link performance over a range, filtered and sorted.",
    response_model=ListResponse[LinkPerformanceView],
    responses=_AUTH,
)
async def list_links(
    principal: Reader,
    reader: AnalyticsReaderDep,
    start: Start = None,
    end: End = None,
    source: AnalyticsSource | None = None,
    source_page_id: UUID | None = None,
    target_page_id: UUID | None = None,
    deployment_id: UUID | None = None,
    plan_status: LinkPlanStatus | None = None,
    sort: LinkSort = LinkSort.CLICKS,
    order: SortOrder = SortOrder.DESC,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
) -> ListResponse[LinkPerformanceView]:
    return await reader.list_links(
        date_range=query_range(start, end),
        limit=limit,
        offset=offset,
        source=source,
        source_page_id=source_page_id,
        target_page_id=target_page_id,
        deployment_id=deployment_id,
        plan_status=plan_status,
        sort=sort,
        order=order,
    )


@router.get(
    "/analytics/links/{plan_id}",
    operation_id="getLinkPerformance",
    summary="One link: attribution chain, live window, series, before/after comparisons.",
    response_model=LinkPerformanceDetail,
    responses={**_AUTH, 404: {"model": ErrorResponse, "description": "No such plan."}},
)
async def get_link(
    principal: Reader,
    reader: AnalyticsReaderDep,
    plan_id: Annotated[UUID, Path()],
    start: Start = None,
    end: End = None,
) -> LinkPerformanceDetail:
    return await reader.get_link(plan_id=plan_id, date_range=query_range(start, end))


@router.get(
    "/analytics/pages/{page_id}",
    operation_id="getPageImpact",
    summary="One page: daily series and the observed change around each link change.",
    response_model=PageImpactView,
    responses={**_AUTH, 404: {"model": ErrorResponse, "description": "No such page."}},
)
async def page_impact(
    principal: Reader,
    reader: AnalyticsReaderDep,
    page_id: Annotated[UUID, Path()],
    window_days: Annotated[int, Query(ge=7, le=90)] = 28,
) -> PageImpactView:
    return await reader.page_impact(page_id=page_id, window_days=window_days)


@router.get(
    "/analytics/imports",
    operation_id="listAnalyticsImports",
    summary="The analytics import ledger, newest first.",
    response_model=ListResponse[AnalyticsImportView],
    responses=_AUTH,
)
async def list_imports(
    principal: Reader,
    reader: AnalyticsReaderDep,
    source: AnalyticsSource | None = None,
    dataset: Dataset | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
) -> ListResponse[AnalyticsImportView]:
    return await reader.list_imports(limit=limit, offset=offset, source=source, dataset=dataset)
