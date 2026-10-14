"""The read-only /intelligence and /jobs APIs over fake readers (no database, no model)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ken_api.deps import get_intelligence_reader, get_job_reader
from ken_contracts.errors import ErrorResponse, ListResponse, PageInfo
from ken_contracts.intelligence import (
    EmbeddingUnavailableError,
    IneligibilityReason,
    IntelligenceCoverage,
    IntelligenceNotFoundError,
    IntelligenceReader,
    Intent,
    LinkEligibility,
    PageAnalysis,
    PageEmbeddingInfo,
    PageIntelligenceView,
    PageProtectionView,
    PageRelationshipView,
    PageTypeView,
    ProtectionSource,
    RelationshipType,
    SimilarPage,
    TaxonomyNodeView,
    TaxonomyView,
)
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import (
    JobKind,
    JobReader,
    JobRunDetail,
    JobRunNotFoundError,
    JobRunSummary,
)
from ken_core.db.engine import create_engine
from ken_core.db.session import create_sessionmaker
from tests.conftest import FakeModelProvider, make_app, make_settings

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
PAGE_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
RUN_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
UNKNOWN = uuid.UUID("99999999-9999-4999-8999-999999999999")
AV = "tax1-alg1"


def _list[T](items: list[T], limit: int, offset: int) -> ListResponse[T]:
    return ListResponse[T](
        items=items[offset : offset + limit],
        page=PageInfo(total=len(items), limit=limit, offset=offset),
    )


def _similar() -> SimilarPage:
    return SimilarPage(
        page_id=OTHER_ID,
        canonical_url="https://site.test/b",
        title="B",
        score=0.9,
        page_type="report",
        market_id="ev",
    )


class FakeIntelligenceReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.search_error: Exception | None = None

    def _page_known(self, page_id: uuid.UUID) -> None:
        if page_id != PAGE_ID:
            raise IntelligenceNotFoundError("page")

    async def get_page_intelligence(self, *, page_id: uuid.UUID) -> PageIntelligenceView:
        self.calls.append(("get_page_intelligence", {"page_id": page_id}))
        self._page_known(page_id)
        return PageIntelligenceView(
            page_id=PAGE_ID,
            canonical_url="https://site.test/a",
            title="A",
            current_version=2,
            analysis=PageAnalysis(
                page_id=PAGE_ID,
                page_version=2,
                analysis_version=AV,
                page_type="report",
                intent=Intent.COMMERCIAL_RESEARCH,
                commercial_importance=3,
                market_id="ev",
                industry_id="automotive",
            ),
            analysis_fresh=True,
            embedding=PageEmbeddingInfo(
                page_id=PAGE_ID,
                page_version=2,
                model="mock-embedding",
                provider="mock",
                dimensions=8,
                recipe_version=1,
                input_hash=sha256(b"x").hexdigest(),
                created_at=NOW,
            ),
            embedding_fresh=True,
            protections=[
                PageProtectionView(
                    id=1,
                    page_id=PAGE_ID,
                    source=ProtectionSource.POLICY,
                    rule_id="legal-pages",
                    reason="Legal",
                    active=True,
                    created_by="policy",
                    created_at=NOW,
                )
            ],
            eligibility=LinkEligibility(
                as_source=False,
                as_target=False,
                source_reasons=[IneligibilityReason.PROTECTED],
                target_reasons=[IneligibilityReason.PROTECTED],
            ),
            relationship_counts={"same_market": 1},
        )

    async def list_similar(self, *, page_id: uuid.UUID, limit: int) -> ListResponse[SimilarPage]:
        self.calls.append(("list_similar", {"page_id": page_id, "limit": limit}))
        self._page_known(page_id)
        return _list([_similar()], limit, 0)

    async def list_relationships(
        self,
        *,
        page_id: uuid.UUID,
        limit: int,
        offset: int,
        relationship_type: RelationshipType | None = None,
    ) -> ListResponse[PageRelationshipView]:
        self.calls.append(
            (
                "list_relationships",
                {"limit": limit, "offset": offset, "relationship_type": relationship_type},
            )
        )
        self._page_known(page_id)
        edge = PageRelationshipView(
            source_page_id=PAGE_ID,
            source_page_version=2,
            target_page_id=OTHER_ID,
            target_page_version=1,
            target_canonical_url="https://site.test/b",
            relationship_type=RelationshipType.SAME_MARKET,
            weight=1.0,
            evidence={"source_market": "ev", "target_market": "ev"},
            analysis_version=AV,
            computed_at=NOW,
        )
        return _list([edge], limit, offset)

    async def get_taxonomy(self) -> TaxonomyView:
        self.calls.append(("get_taxonomy", {}))
        return TaxonomyView(
            version=1,
            status="baseline",
            source="fixture",
            analysis_version=AV,
            markets=[TaxonomyNodeView(id="ev", label="EV", kind="market", depth=0, page_count=1)],
            geographies=[
                TaxonomyNodeView(id="india", label="India", kind="country", depth=2, page_count=0)
            ],
            page_types=[
                PageTypeView(
                    id="report",
                    label="Report",
                    intent=Intent.COMMERCIAL_RESEARCH,
                    commercial_importance=3,
                    linkable_as_source=True,
                    linkable_as_target=True,
                    page_count=1,
                )
            ],
        )

    async def list_topic_pages(
        self, *, node_id: str, limit: int, offset: int
    ) -> ListResponse[SimilarPage]:
        self.calls.append(("list_topic_pages", {"node_id": node_id, "limit": limit}))
        if node_id != "ev":
            raise IntelligenceNotFoundError("taxonomy_node")
        return _list([_similar()], limit, offset)

    async def get_coverage(self) -> IntelligenceCoverage:
        self.calls.append(("get_coverage", {}))
        return IntelligenceCoverage(
            total_pages=2,
            analysis_version=AV,
            analyzed_fresh=2,
            analyzed_stale=0,
            never_analyzed=0,
            embedding_model="mock-embedding",
            embedded_fresh=2,
            embedded_stale_or_missing=0,
            protected=1,
            eligible_sources=1,
            eligible_targets=1,
            relationships=2,
        )

    async def semantic_search(self, *, query: str, limit: int) -> ListResponse[SimilarPage]:
        self.calls.append(("semantic_search", {"query": query, "limit": limit}))
        if self.search_error is not None:
            raise self.search_error
        return _list([_similar()], limit, 0)


def _run_summary() -> dict[str, Any]:
    return {
        "id": RUN_ID,
        "kind": JobKind.ANALYSIS,
        "status": RunStatus.SUCCEEDED,
        "triggered_by": "cli",
        "started_at": NOW,
        "completed_at": NOW,
        "counters": {"pages_analyzed": 2},
    }


class FakeJobReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_runs(
        self,
        *,
        limit: int,
        offset: int,
        kind: JobKind | None = None,
        status: RunStatus | None = None,
    ) -> ListResponse[JobRunSummary]:
        self.calls.append(("list_runs", {"kind": kind, "status": status, "limit": limit}))
        return _list([JobRunSummary(**_run_summary())], limit, offset)

    async def get_run(self, *, run_id: uuid.UUID) -> JobRunDetail:
        if run_id != RUN_ID:
            raise JobRunNotFoundError()
        return JobRunDetail(**_run_summary(), config_snapshot={"force": False})


@pytest.fixture
def reader() -> FakeIntelligenceReader:
    return FakeIntelligenceReader()


@pytest.fixture
def jobs() -> FakeJobReader:
    return FakeJobReader()


@pytest.fixture
def client(reader: FakeIntelligenceReader, jobs: FakeJobReader) -> TestClient:
    app = make_app()
    app.dependency_overrides[get_intelligence_reader] = lambda: reader
    app.dependency_overrides[get_job_reader] = lambda: jobs
    return TestClient(app, raise_server_exceptions=False)


def test_fakes_satisfy_the_protocols(reader: FakeIntelligenceReader, jobs: FakeJobReader) -> None:
    assert isinstance(reader, IntelligenceReader)
    assert isinstance(jobs, JobReader)


@pytest.mark.parametrize(
    ("path", "model"),
    [
        ("/intelligence/coverage", IntelligenceCoverage),
        ("/intelligence/taxonomy", TaxonomyView),
        ("/intelligence/taxonomy/ev/pages", ListResponse[SimilarPage]),
        (f"/intelligence/pages/{PAGE_ID}", PageIntelligenceView),
        (f"/intelligence/pages/{PAGE_ID}/similar", ListResponse[SimilarPage]),
        (f"/intelligence/pages/{PAGE_ID}/relationships", ListResponse[PageRelationshipView]),
        ("/intelligence/search?q=electric", ListResponse[SimilarPage]),
        ("/jobs/runs", ListResponse[JobRunSummary]),
        (f"/jobs/runs/{RUN_ID}", JobRunDetail),
    ],
)
def test_every_route_returns_its_contract_model(client: TestClient, path: str, model: Any) -> None:
    response = client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    if "page" in body:
        body["page"].pop("has_more")  # computed, output-only
    model.model_validate(body)


def test_the_embedding_vector_is_never_served(client: TestClient) -> None:
    body = client.get(f"/intelligence/pages/{PAGE_ID}").json()
    assert set(body["embedding"]) == set(PageEmbeddingInfo.model_fields)
    assert "embedding" not in body["embedding"] and "vector" not in body["embedding"]


def test_defaults_and_parameters_reach_the_readers(
    client: TestClient, reader: FakeIntelligenceReader, jobs: FakeJobReader
) -> None:
    client.get(f"/intelligence/pages/{PAGE_ID}/similar")
    client.get(f"/intelligence/pages/{PAGE_ID}/relationships?relationship_type=submarket&offset=3")
    client.get("/intelligence/taxonomy/ev/pages")
    client.get("/intelligence/search?q=ev%20india&limit=5")
    client.get("/jobs/runs?kind=analysis&status=failed")
    calls = dict(reader.calls)
    assert calls["list_similar"]["limit"] == 10
    assert calls["list_relationships"] == {
        "limit": 50,
        "offset": 3,
        "relationship_type": RelationshipType.SUBMARKET,
    }
    assert calls["list_topic_pages"] == {"node_id": "ev", "limit": 50}
    assert calls["semantic_search"] == {"query": "ev india", "limit": 5}
    assert dict(jobs.calls)["list_runs"] == {
        "kind": JobKind.ANALYSIS,
        "status": RunStatus.FAILED,
        "limit": 50,
    }


@pytest.mark.parametrize(
    ("path", "code"),
    [
        (f"/intelligence/pages/{UNKNOWN}", "page_not_found"),
        (f"/intelligence/pages/{UNKNOWN}/similar", "page_not_found"),
        (f"/intelligence/pages/{UNKNOWN}/relationships", "page_not_found"),
        ("/intelligence/taxonomy/unknown-node/pages", "not_found"),
        (f"/jobs/runs/{UNKNOWN}", "not_found"),
    ],
)
def test_unknown_resources_are_404_without_echoing_the_id(
    client: TestClient, path: str, code: str
) -> None:
    response = client.get(path)
    assert response.status_code == 404
    body = ErrorResponse.model_validate(response.json())
    assert body.code.value == code
    assert str(UNKNOWN) not in response.text
    assert "unknown-node" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/intelligence/pages/not-a-uuid",
        f"/intelligence/pages/{PAGE_ID}/similar?limit=0",
        f"/intelligence/pages/{PAGE_ID}/similar?limit=51",
        f"/intelligence/pages/{PAGE_ID}/relationships?limit=201",
        f"/intelligence/pages/{PAGE_ID}/relationships?offset=-1",
        f"/intelligence/pages/{PAGE_ID}/relationships?relationship_type=friends",
        "/intelligence/taxonomy/Not_Valid!/pages",
        "/intelligence/taxonomy/ev/pages?limit=0",
        "/intelligence/search",
        "/intelligence/search?q=a",
        "/intelligence/search?q=" + "x" * 501,
        "/jobs/runs?kind=crawl",
        "/jobs/runs?status=paused",
        "/jobs/runs/not-a-uuid",
    ],
)
def test_invalid_parameters_are_422_error_responses(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 422, response.text
    body = ErrorResponse.model_validate(response.json())
    assert body.code.value == "validation_failed"


def test_search_rejects_nul_with_422(client: TestClient) -> None:
    response = client.get("/intelligence/search", params={"q": "ev\x00india"})
    assert response.status_code == 422


def test_search_answers_503_model_unavailable_with_retry_after(
    client: TestClient, reader: FakeIntelligenceReader
) -> None:
    reader.search_error = EmbeddingUnavailableError("connection refused to http://secret-host:1")
    response = client.get("/intelligence/search?q=electric")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    body = ErrorResponse.model_validate(response.json())
    assert body.code.value == "model_unavailable"
    assert "secret-host" not in response.text


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize(
    "path",
    [
        "/intelligence/coverage",
        "/intelligence/taxonomy",
        f"/intelligence/pages/{PAGE_ID}",
        f"/intelligence/pages/{PAGE_ID}/similar",
        f"/intelligence/pages/{PAGE_ID}/relationships",
        "/intelligence/search",
        "/jobs/runs",
        f"/jobs/runs/{RUN_ID}",
    ],
)
def test_there_are_no_mutation_routes(client: TestClient, method: str, path: str) -> None:
    response = client.request(method.upper(), path)
    assert response.status_code == 405
    ErrorResponse.model_validate(response.json())


def test_every_intelligence_and_jobs_route_is_get_only() -> None:
    paths = make_app().openapi()["paths"]
    ours = {
        path: set(operations)
        for path, operations in paths.items()
        if path.startswith(("/intelligence", "/jobs"))
    }
    assert len(ours) == 9
    assert all(methods == {"get"} for methods in ours.values()), ours


# --------------------------------------------------------------------------- #
# The real dependencies resolve the SQL readers by name (no overrides)
# --------------------------------------------------------------------------- #
async def test_default_dependencies_build_the_sql_readers() -> None:
    from ken_services.intelligence.reader import SqlIntelligenceReader
    from ken_services.jobs.reader import SqlJobReader

    settings = make_settings()
    engine = create_engine(settings)  # creating an engine performs no I/O
    try:
        sessionmaker = create_sessionmaker(engine)
        intelligence = get_intelligence_reader(settings, sessionmaker, FakeModelProvider())
        jobs = get_job_reader(sessionmaker)
        assert isinstance(intelligence, SqlIntelligenceReader)
        assert isinstance(intelligence, IntelligenceReader)
        assert isinstance(jobs, SqlJobReader)
        assert isinstance(jobs, JobReader)
    finally:
        await engine.dispose()
