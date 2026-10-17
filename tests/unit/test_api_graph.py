"""The read-only /graph API over a fake reader (no database).

Every route needs `read`, is GET-only, validates its parameters, and answers the frozen contract
shapes. The generic RBAC matrix in test_api_security.py additionally covers these routes through
tests/support/route_permissions.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ken_api.deps import get_graph_reader
from ken_contracts.errors import ErrorResponse, ListResponse, PageInfo
from ken_contracts.graph import (
    GraphClusterSummary,
    GraphOverview,
    GraphPageRow,
    GraphReader,
)
from ken_contracts.intelligence import IntelligenceNotFoundError
from ken_contracts.recommendations import GraphSignals
from tests.conftest import make_app

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
HOME = uuid.UUID("11111111-1111-4111-8111-111111111111")
LONELY = uuid.UUID("22222222-2222-4222-8222-222222222222")
UNKNOWN = uuid.UUID("99999999-9999-4999-8999-999999999999")


def _signals(page_id: uuid.UUID, *, in_degree: int, crawl_depth: int | None) -> GraphSignals:
    return GraphSignals(
        page_id=page_id,
        page_version=1,
        in_degree=in_degree,
        out_degree=3,
        internal_pagerank=0.1,
        hub_score=0.2,
        authority_score=0.3,
        crawl_depth=crawl_depth,
        is_orphan=in_degree == 0,
        is_nearly_orphan=in_degree <= 1,
        is_bridge=False,
        component_id=0,
        cluster_id=None,
        metrics_version="graph-alg1-pol1",
        computed_at=NOW,
    )


def _row(page_id: uuid.UUID, url: str, *, in_degree: int, depth: int | None) -> GraphPageRow:
    return GraphPageRow(
        signals=_signals(page_id, in_degree=in_degree, crawl_depth=depth),
        canonical_url=url,
        title=None,
        is_entry_page=depth == 0,
    )


ROWS = [
    # The home page: nothing links to it (is_orphan) but it IS the crawl entry.
    _row(HOME, "https://site.test/", in_degree=0, depth=0),
    # A TRUE orphan: nothing links to it and it is unreachable from the entry.
    _row(LONELY, "https://site.test/about", in_degree=0, depth=None),
]


class FakeGraphReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_overview(self) -> GraphOverview:
        self.calls.append(("get_overview", {}))
        return GraphOverview(
            metrics_version="graph-alg1-pol1",
            computed_at=NOW,
            pages=2,
            entry_pages=1,
            orphans=2,
            true_orphans=1,
            nearly_orphans=2,
            bridges=0,
            components=2,
            clusters=0,
        )

    async def list_clusters(self) -> list[GraphClusterSummary]:
        self.calls.append(("list_clusters", {}))
        return [
            GraphClusterSummary(
                cluster_id=0,
                pages=2,
                orphans=1,
                true_orphans=1,
                nearly_orphans=2,
                bridges=0,
                mean_in_degree=0.5,
                top_page_id=HOME,
                top_page_url="https://site.test/",
            )
        ]

    async def list_pages(
        self,
        *,
        limit: int,
        offset: int,
        cluster_id: int | None = None,
        component_id: int | None = None,
        orphans_only: bool = False,
        bridges_only: bool = False,
    ) -> ListResponse[GraphPageRow]:
        self.calls.append(
            (
                "list_pages",
                {
                    "limit": limit,
                    "offset": offset,
                    "cluster_id": cluster_id,
                    "component_id": component_id,
                    "orphans_only": orphans_only,
                    "bridges_only": bridges_only,
                },
            )
        )
        return ListResponse[GraphPageRow](
            items=ROWS[offset : offset + limit],
            page=PageInfo(total=len(ROWS), limit=limit, offset=offset),
        )

    async def get_page(self, *, page_id: uuid.UUID) -> GraphPageRow:
        self.calls.append(("get_page", {"page_id": page_id}))
        for row in ROWS:
            if row.signals.page_id == page_id:
                return row
        raise IntelligenceNotFoundError("page")


@pytest.fixture
def reader() -> FakeGraphReader:
    return FakeGraphReader()


@pytest.fixture
def client(reader: FakeGraphReader) -> TestClient:
    app = make_app()
    app.dependency_overrides[get_graph_reader] = lambda: reader
    return TestClient(app, raise_server_exceptions=False)


def test_the_fake_satisfies_the_reader_protocol(reader: FakeGraphReader) -> None:
    assert isinstance(reader, GraphReader)


def test_overview_and_clusters_return_their_contract_models(client: TestClient) -> None:
    overview = client.get("/graph/overview")
    assert overview.status_code == 200
    body = GraphOverview.model_validate(overview.json())
    assert (body.orphans, body.true_orphans, body.entry_pages) == (2, 1, 1)
    clusters = client.get("/graph/clusters")
    assert clusters.status_code == 200
    assert [GraphClusterSummary.model_validate(c).cluster_id for c in clusters.json()] == [0]


def test_the_entry_page_is_distinguishable_from_a_true_orphan(client: TestClient) -> None:
    payload = client.get("/graph/pages").json()
    rows = {r["canonical_url"]: r for r in payload["items"]}
    home, about = rows["https://site.test/"], rows["https://site.test/about"]
    # Faithful: both are is_orphan ...
    assert home["signals"]["is_orphan"] and about["signals"]["is_orphan"]
    # ... but crawl_depth / is_entry_page tell them apart.
    assert (home["signals"]["crawl_depth"], home["is_entry_page"]) == (0, True)
    assert (about["signals"]["crawl_depth"], about["is_entry_page"]) == (None, False)


def test_list_pages_forwards_validated_filters_and_paginates(
    client: TestClient, reader: FakeGraphReader
) -> None:
    response = client.get(
        "/graph/pages",
        params={
            "limit": 1,
            "offset": 1,
            "cluster_id": 3,
            "component_id": 0,
            "orphans_only": "true",
            "bridges_only": "false",
        },
    )
    assert response.status_code == 200
    page = ListResponse[GraphPageRow].model_validate(response.json())
    assert page.page.total == 2 and page.page.limit == 1 and page.page.offset == 1
    assert len(page.items) == 1
    assert reader.calls[-1] == (
        "list_pages",
        {
            "limit": 1,
            "offset": 1,
            "cluster_id": 3,
            "component_id": 0,
            "orphans_only": True,
            "bridges_only": False,
        },
    )


def test_list_pages_defaults(client: TestClient, reader: FakeGraphReader) -> None:
    assert client.get("/graph/pages").status_code == 200
    assert reader.calls[-1][1] == {
        "limit": 50,
        "offset": 0,
        "cluster_id": None,
        "component_id": None,
        "orphans_only": False,
        "bridges_only": False,
    }


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 101},
        {"limit": 0},
        {"limit": -1},
        {"offset": -1},
        {"cluster_id": -1},
        {"component_id": "x"},
        {"orphans_only": "maybe"},
        {"limit": "ten"},
    ],
)
def test_invalid_parameters_are_422_error_responses_and_reach_no_reader(
    client: TestClient, reader: FakeGraphReader, params: dict[str, Any]
) -> None:
    response = client.get("/graph/pages", params=params)
    assert response.status_code == 422
    ErrorResponse.model_validate(response.json())
    assert reader.calls == []


def test_get_page_by_id_and_404_for_an_unknown_page(client: TestClient) -> None:
    found = client.get(f"/graph/pages/{HOME}")
    assert found.status_code == 200
    assert GraphPageRow.model_validate(found.json()).signals.page_id == HOME
    missing = client.get(f"/graph/pages/{UNKNOWN}")
    assert missing.status_code == 404
    assert ErrorResponse.model_validate(missing.json()).code == "page_not_found"


def test_a_malformed_page_id_is_422(client: TestClient, reader: FakeGraphReader) -> None:
    assert client.get("/graph/pages/not-a-uuid").status_code == 422
    assert reader.calls == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/graph/overview", "/graph/clusters", "/graph/pages"])
def test_there_are_no_mutation_routes(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path).status_code == 405


def test_the_graph_paths_in_the_openapi_document_are_get_only() -> None:
    paths = make_app().openapi()["paths"]
    graph_paths = {p: v for p, v in paths.items() if p.startswith("/graph")}
    assert set(graph_paths) == {
        "/graph/overview",
        "/graph/clusters",
        "/graph/pages",
        "/graph/pages/{page_id}",
    }
    assert all(set(item) == {"get"} for item in graph_paths.values())


@pytest.mark.parametrize(
    "path", ["/graph/overview", "/graph/clusters", "/graph/pages", f"/graph/pages/{HOME}"]
)
def test_every_route_requires_authentication(path: str, reader: FakeGraphReader) -> None:
    app = make_app(real_auth=True)  # the real Bearer-token dependency
    app.dependency_overrides[get_graph_reader] = lambda: reader
    response = TestClient(app, raise_server_exceptions=False).get(path)
    assert response.status_code == 401
    assert reader.calls == []  # denied before the reader was ever reached
