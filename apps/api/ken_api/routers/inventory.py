"""Phase 1 inventory read API: pages, versions, existing links, runs.

READ-ONLY. Every route is a GET over :class:`ken_contracts.inventory.InventoryReader`;
none writes to the database, and nothing here can reach a CMS. Runs are
started from the command line (``make ingest-file`` / ``make crawl``), not over
HTTP: there is no authentication before Phase 5, and an unauthenticated
"start a crawl" endpoint would let any caller make this machine fetch URLs.

Route functions carry no docstrings on purpose: FastAPI would publish them as
OpenAPI descriptions (see routers/health.py). The frozen contract is
contracts/openapi/openapi.json.
"""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Path, Query

from ken_api.deps import InventoryReaderDep
from ken_contracts.errors import ErrorResponse, ListResponse
from ken_contracts.inventory import (
    ExistingLinkView,
    IngestionIssueView,
    IngestionRunDetail,
    IngestionRunSummary,
    IssueSeverity,
    LinkResolution,
    LinkType,
    PageDetail,
    PageSummary,
    PageVersionDetail,
    PageVersionSummary,
    PublicationState,
    RunStatus,
    SourceType,
)

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200

router = APIRouter(prefix="/inventory", tags=["inventory"])

_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ErrorResponse, "description": "A parameter is invalid."},
}
_NOT_FOUND: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ErrorResponse, "description": "No such resource."},
    **_ERRORS,
}

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Offset = Annotated[int, Query(ge=0, le=10_000_000)]
PageIdPath = Annotated[UUID, Path()]
VersionPath = Annotated[int, Path(ge=1)]
RunIdPath = Annotated[UUID, Path()]


@router.get(
    "/pages",
    operation_id="listInventoryPages",
    summary="Page inventory, newest change first.",
    response_model=ListResponse[PageSummary],
    responses=_ERRORS,
)
async def list_pages(
    reader: InventoryReaderDep,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
    # No NUL: PostgreSQL text cannot hold it, and stored text never contains it
    # (the normaliser replaces it), so it is a client error, not a 500 (QA M2).
    q: Annotated[str | None, Query(min_length=1, max_length=200, pattern=r"^[^\x00]*$")] = None,
    publication_state: PublicationState | None = None,
) -> ListResponse[PageSummary]:
    return await reader.list_pages(
        limit=limit, offset=offset, search=q, publication_state=publication_state
    )


@router.get(
    "/pages/{page_id}",
    operation_id="getInventoryPage",
    summary="One page with its current version.",
    response_model=PageDetail,
    responses=_NOT_FOUND,
)
async def get_page(reader: InventoryReaderDep, page_id: PageIdPath) -> PageDetail:
    return await reader.get_page(page_id=page_id)


@router.get(
    "/pages/{page_id}/versions",
    operation_id="listInventoryPageVersions",
    summary="A page's immutable version history, newest first.",
    response_model=ListResponse[PageVersionSummary],
    responses=_NOT_FOUND,
)
async def list_page_versions(
    reader: InventoryReaderDep,
    page_id: PageIdPath,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> ListResponse[PageVersionSummary]:
    return await reader.list_page_versions(page_id=page_id, limit=limit, offset=offset)


@router.get(
    "/pages/{page_id}/versions/{version}",
    operation_id="getInventoryPageVersion",
    summary="One page version, including its stored content.",
    response_model=PageVersionDetail,
    responses=_NOT_FOUND,
)
async def get_page_version(
    reader: InventoryReaderDep, page_id: PageIdPath, version: VersionPath
) -> PageVersionDetail:
    return await reader.get_page_version(page_id=page_id, version=version)


@router.get(
    "/pages/{page_id}/links",
    operation_id="listInventoryPageLinks",
    summary="Body links of one page version (default: current).",
    response_model=ListResponse[ExistingLinkView],
    responses=_NOT_FOUND,
)
async def list_page_links(
    reader: InventoryReaderDep,
    page_id: PageIdPath,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
    version: Annotated[int | None, Query(ge=1)] = None,
    link_type: LinkType | None = None,
) -> ListResponse[ExistingLinkView]:
    return await reader.list_page_links(
        page_id=page_id, limit=limit, offset=offset, version=version, link_type=link_type
    )


@router.get(
    "/links",
    operation_id="listInventoryLinks",
    summary="Body links across every page's current version.",
    response_model=ListResponse[ExistingLinkView],
    responses=_ERRORS,
)
async def list_links(
    reader: InventoryReaderDep,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
    link_type: LinkType | None = None,
    resolution: LinkResolution | None = None,
    target_page_id: UUID | None = None,
) -> ListResponse[ExistingLinkView]:
    return await reader.list_links(
        limit=limit,
        offset=offset,
        link_type=link_type,
        resolution=resolution,
        target_page_id=target_page_id,
    )


@router.get(
    "/runs",
    operation_id="listIngestionRuns",
    summary="Crawl and import runs, newest first.",
    response_model=ListResponse[IngestionRunSummary],
    responses=_ERRORS,
)
async def list_runs(
    reader: InventoryReaderDep,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
    source_type: SourceType | None = None,
    status: RunStatus | None = None,
) -> ListResponse[IngestionRunSummary]:
    return await reader.list_runs(
        limit=limit, offset=offset, source_type=source_type, status=status
    )


@router.get(
    "/runs/{run_id}",
    operation_id="getIngestionRun",
    summary="One run with its configuration snapshot and issue counts.",
    response_model=IngestionRunDetail,
    responses=_NOT_FOUND,
)
async def get_run(reader: InventoryReaderDep, run_id: RunIdPath) -> IngestionRunDetail:
    return await reader.get_run(run_id=run_id)


@router.get(
    "/runs/{run_id}/issues",
    operation_id="listIngestionRunIssues",
    summary="Per-URL issues recorded by one run.",
    response_model=ListResponse[IngestionIssueView],
    responses=_NOT_FOUND,
)
async def list_run_issues(
    reader: InventoryReaderDep,
    run_id: RunIdPath,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
    severity: IssueSeverity | None = None,
) -> ListResponse[IngestionIssueView]:
    return await reader.list_run_issues(
        run_id=run_id, limit=limit, offset=offset, severity=severity
    )
