"""Phase 2 content-intelligence and job-run read API.

READ-ONLY, like the inventory API: analysis runs start from the command line
(``make analyze``). ``GET /intelligence/search`` embeds the query text with
the EMBEDDING role and answers 503 ``model_unavailable`` when the model cannot
be reached -- the only route here that calls a model, and it never stores
anything.

Route functions carry no docstrings on purpose (see routers/health.py).
"""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from ken_api.deps import IntelligenceReaderDep, JobReaderDep
from ken_api.security import AUTH_RESPONSES, require
from ken_contracts.auth import Permission
from ken_contracts.errors import ErrorResponse, ListResponse
from ken_contracts.intelligence import (
    IntelligenceCoverage,
    PageIntelligenceView,
    PageRelationshipView,
    RelationshipType,
    SimilarPage,
    TaxonomyView,
)
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail, JobRunSummary

# Deny by default (ADR-0013 s7): every route here needs the `read` permission.
_READ: Final = [Depends(require(Permission.READ))]
router = APIRouter(
    prefix="/intelligence", tags=["intelligence"], dependencies=_READ, responses=AUTH_RESPONSES
)
jobs_router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=_READ, responses=AUTH_RESPONSES)

_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ErrorResponse, "description": "A parameter is invalid."},
}
_NOT_FOUND: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ErrorResponse, "description": "No such resource."},
    **_ERRORS,
}
_SEARCH: Final[dict[int | str, dict[str, Any]]] = {
    503: {"model": ErrorResponse, "description": "The embedding model is unavailable."},
    **_ERRORS,
}

Limit = Annotated[int, Query(ge=1, le=200)]
SmallLimit = Annotated[int, Query(ge=1, le=50)]
Offset = Annotated[int, Query(ge=0, le=10_000_000)]
PageIdPath = Annotated[UUID, Path()]
NodeIdPath = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9_.-]{0,99}$")]


@router.get(
    "/coverage",
    operation_id="getIntelligenceCoverage",
    summary="How much of the inventory is analysed, embedded, protected and eligible.",
    response_model=IntelligenceCoverage,
)
async def get_coverage(reader: IntelligenceReaderDep) -> IntelligenceCoverage:
    return await reader.get_coverage()


@router.get(
    "/taxonomy",
    operation_id="getTaxonomy",
    summary="The active taxonomy policy with page counts.",
    response_model=TaxonomyView,
)
async def get_taxonomy(reader: IntelligenceReaderDep) -> TaxonomyView:
    return await reader.get_taxonomy()


@router.get(
    "/taxonomy/{node_id}/pages",
    operation_id="listTaxonomyNodePages",
    summary="Pages whose current analysis lists a market or geography.",
    response_model=ListResponse[SimilarPage],
    responses=_NOT_FOUND,
)
async def list_topic_pages(
    reader: IntelligenceReaderDep,
    node_id: NodeIdPath,
    limit: Limit = 50,
    offset: Offset = 0,
) -> ListResponse[SimilarPage]:
    return await reader.list_topic_pages(node_id=node_id, limit=limit, offset=offset)


@router.get(
    "/pages/{page_id}",
    operation_id="getPageIntelligence",
    summary="Analysis, embedding, protections and eligibility of one page.",
    response_model=PageIntelligenceView,
    responses=_NOT_FOUND,
)
async def get_page_intelligence(
    reader: IntelligenceReaderDep, page_id: PageIdPath
) -> PageIntelligenceView:
    return await reader.get_page_intelligence(page_id=page_id)


@router.get(
    "/pages/{page_id}/similar",
    operation_id="listSimilarPages",
    summary="Nearest pages by stored embedding.",
    response_model=ListResponse[SimilarPage],
    responses=_NOT_FOUND,
)
async def list_similar(
    reader: IntelligenceReaderDep, page_id: PageIdPath, limit: SmallLimit = 10
) -> ListResponse[SimilarPage]:
    return await reader.list_similar(page_id=page_id, limit=limit)


@router.get(
    "/pages/{page_id}/relationships",
    operation_id="listPageRelationships",
    summary="Typed relationships from one page, strongest first.",
    response_model=ListResponse[PageRelationshipView],
    responses=_NOT_FOUND,
)
async def list_relationships(
    reader: IntelligenceReaderDep,
    page_id: PageIdPath,
    limit: Limit = 50,
    offset: Offset = 0,
    relationship_type: RelationshipType | None = None,
) -> ListResponse[PageRelationshipView]:
    return await reader.list_relationships(
        page_id=page_id, limit=limit, offset=offset, relationship_type=relationship_type
    )


@router.get(
    "/search",
    operation_id="semanticSearch",
    summary="Semantic search over current page embeddings.",
    response_model=ListResponse[SimilarPage],
    responses=_SEARCH,
)
async def semantic_search(
    reader: IntelligenceReaderDep,
    q: Annotated[str, Query(min_length=2, max_length=500, pattern=r"^[^\x00]*$")],
    limit: SmallLimit = 10,
) -> ListResponse[SimilarPage]:
    return await reader.semantic_search(query=q, limit=limit)


@jobs_router.get(
    "/runs",
    operation_id="listJobRuns",
    summary="Batch job runs (analysis and later phases), newest first.",
    response_model=ListResponse[JobRunSummary],
    responses=_ERRORS,
)
async def list_job_runs(
    reader: JobReaderDep,
    limit: Limit = 50,
    offset: Offset = 0,
    kind: JobKind | None = None,
    status: RunStatus | None = None,
) -> ListResponse[JobRunSummary]:
    return await reader.list_runs(limit=limit, offset=offset, kind=kind, status=status)


@jobs_router.get(
    "/runs/{run_id}",
    operation_id="getJobRun",
    summary="One job run with its configuration snapshot.",
    response_model=JobRunDetail,
    responses=_NOT_FOUND,
)
async def get_job_run(reader: JobReaderDep, run_id: Annotated[UUID, Path()]) -> JobRunDetail:
    return await reader.get_run(run_id=run_id)
