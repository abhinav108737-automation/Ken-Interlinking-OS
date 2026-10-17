"""Corpus-wide link-graph read API (directive section 5; additive).

READ-ONLY. The metrics are computed by ``make graph-metrics`` (the ``graph_metrics`` job) into
``page_graph_metrics``; nothing here computes or writes. Readers resolve through ``deps.py``
over READ ONLY sessions.

The home page is normally ``is_orphan`` because nothing links to it; use ``is_entry_page`` /
``crawl_depth`` to tell the crawl entry from a TRUE orphan (see ``ken_contracts.graph``).

Route functions carry no docstrings on purpose (see routers/health.py).
"""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from ken_api.deps import GraphReaderDep
from ken_api.security import AUTH_RESPONSES, require
from ken_contracts.auth import Permission
from ken_contracts.errors import ErrorResponse, ListResponse
from ken_contracts.graph import GraphClusterSummary, GraphOverview, GraphPageRow

# Deny by default (ADR-0013 s7): every route here needs the `read` permission.
_READ: Final = [Depends(require(Permission.READ))]
router = APIRouter(prefix="/graph", tags=["graph"], dependencies=_READ, responses=AUTH_RESPONSES)

_ERRORS: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ErrorResponse, "description": "A parameter is invalid."},
}
_NOT_FOUND: Final[dict[int | str, dict[str, Any]]] = {
    404: {
        "model": ErrorResponse,
        "description": "No such page, or graph metrics have not been computed for it yet.",
    },
    **_ERRORS,
}

Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=10_000_000)]
Ordinal = Annotated[int, Query(ge=0, le=1_000_000)]
PageIdPath = Annotated[UUID, Path()]


@router.get(
    "/overview",
    operation_id="getGraphOverview",
    summary="Corpus-wide link-graph counts: orphans, bridges, components, clusters.",
    response_model=GraphOverview,
)
async def get_overview(reader: GraphReaderDep) -> GraphOverview:
    return await reader.get_overview()


@router.get(
    "/clusters",
    operation_id="listGraphClusters",
    summary="One summary per topical cluster, largest first (the topic-less group last).",
    response_model=list[GraphClusterSummary],
)
async def list_clusters(reader: GraphReaderDep) -> list[GraphClusterSummary]:
    return await reader.list_clusters()


@router.get(
    "/pages",
    operation_id="listGraphPages",
    summary="Pages with their link-graph position, most authoritative first.",
    response_model=ListResponse[GraphPageRow],
    responses=_ERRORS,
)
async def list_pages(
    reader: GraphReaderDep,
    limit: Limit = 50,
    offset: Offset = 0,
    cluster_id: Ordinal | None = None,
    component_id: Ordinal | None = None,
    orphans_only: bool = False,
    bridges_only: bool = False,
) -> ListResponse[GraphPageRow]:
    return await reader.list_pages(
        limit=limit,
        offset=offset,
        cluster_id=cluster_id,
        component_id=component_id,
        orphans_only=orphans_only,
        bridges_only=bridges_only,
    )


@router.get(
    "/pages/{page_id}",
    operation_id="getGraphPage",
    summary="The link-graph position of one page.",
    response_model=GraphPageRow,
    responses=_NOT_FOUND,
)
async def get_page(reader: GraphReaderDep, page_id: PageIdPath) -> GraphPageRow:
    return await reader.get_page(page_id=page_id)
