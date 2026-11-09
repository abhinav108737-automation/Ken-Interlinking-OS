"""The read-only /recommendations API over a fake reader (no database)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ken_api.deps import get_recommendation_reader
from ken_contracts.errors import ErrorResponse, ListResponse, PageInfo
from ken_contracts.recommendations import (
    ENGINE_VERSION,
    CandidateRejection,
    CandidateView,
    LinkPlan,
    LinkPlanDetail,
    LinkPlanStatus,
    ModuleKind,
    OpportunityKind,
    OpportunityView,
    PageModuleItemView,
    PageModuleView,
    PlacementLocator,
    PlacementPreview,
    PlanNotFoundError,
    PlanTransitionView,
    ReasonCode,
    RecommendationReader,
    RecommendationSummary,
    RetrievalChannel,
    ScoreBreakdown,
    ScoreComponent,
)
from ken_core.db.engine import create_engine
from ken_core.db.session import create_sessionmaker
from tests.conftest import make_app, make_settings

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
PLAN_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
SOURCE_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
TARGET_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
RUN_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
UNKNOWN = uuid.UUID("99999999-9999-4999-8999-999999999999")
BLOCK = "Vehicle makers depend on imported lithium-ion batteries today."


def _list[T](items: list[T], limit: int, offset: int) -> ListResponse[T]:
    return ListResponse[T](
        items=items[offset : offset + limit],
        page=PageInfo(total=len(items), limit=limit, offset=offset),
    )


def _plan() -> LinkPlan:
    start = BLOCK.index("lithium-ion batteries")
    end = start + len("lithium-ion batteries")
    return LinkPlan(
        id=PLAN_ID,
        run_id=RUN_ID,
        source_page_id=SOURCE_ID,
        source_page_version=1,
        source_url="https://site.test/reports/ev",
        target_page_id=TARGET_ID,
        target_page_version=1,
        target_url="https://site.test/reports/li",
        target_title="Lithium-ion Batteries Market",
        relationship_type="supply_chain_upstream",
        channels=(RetrievalChannel.SEMANTIC, RetrievalChannel.TAXONOMY),
        anchor_text="lithium-ion batteries",
        anchor_source="target_topic",
        locator=PlacementLocator(
            block_id="b2",
            block_kind="p",
            occurrence_index=0,
            start=start,
            end=end,
            context_hash=sha256(BLOCK.encode()).hexdigest(),
        ),
        preview=PlacementPreview(
            before=BLOCK[:start], anchor="lithium-ion batteries", after=BLOCK[end:]
        ),
        score=ScoreBreakdown(
            components=(
                ScoreComponent(name="semantic", value=0.5, weight=0.6, contribution=0.3),
                ScoreComponent(name="taxonomy", value=0.75, weight=0.4, contribution=0.3),
            ),
            relevance=0.6,
            factors={"placement_quality": 0.9},
            total=0.54,
            semantic_floor=0.3,
            policy_name="ranking",
            policy_version=1,
        ),
        confidence=0.54,
        reason_codes=("supply_chain", "anchor_matches_target_topic"),
        rationale='Link to "Lithium-ion Batteries Market" on the existing phrase.',
        engine_version=ENGINE_VERSION,
        policy_version=1,
        ranking_policy_version=1,
        embedding_model="mock-embedding",
        analysis_version="tax1-alg1",
        status=LinkPlanStatus.PROPOSED,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeRecommendationReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_plans(self, **kwargs: Any) -> ListResponse[LinkPlan]:
        self.calls.append(("list_plans", kwargs))
        return _list([_plan()], kwargs["limit"], kwargs["offset"])

    async def get_plan(self, *, plan_id: uuid.UUID) -> LinkPlanDetail:
        self.calls.append(("get_plan", {"plan_id": plan_id}))
        if plan_id != PLAN_ID:
            raise PlanNotFoundError
        return LinkPlanDetail(
            **_plan().model_dump(),
            transitions=[
                PlanTransitionView(
                    id=1,
                    plan_id=PLAN_ID,
                    from_status=None,
                    to_status=LinkPlanStatus.PROPOSED,
                    actor="system:recommendation-engine",
                    occurred_at=NOW,
                )
            ],
            source_title="EV Market",
            source_block_text=BLOCK,
        )

    async def list_candidates(self, **kwargs: Any) -> ListResponse[CandidateView]:
        self.calls.append(("list_candidates", kwargs))
        items = [
            CandidateView(
                run_id=RUN_ID,
                source_page_id=SOURCE_ID,
                target_page_id=TARGET_ID,
                target_url="https://site.test/reports/li",
                channels=(RetrievalChannel.LEXICAL,),
                channel_scores={"lexical": 0.4},
                total_score=0.2,
                rejection=CandidateRejection.BELOW_MIN_RELEVANCE,
            )
        ]
        return _list(items, kwargs["limit"], kwargs["offset"])

    async def list_opportunities(self, **kwargs: Any) -> ListResponse[OpportunityView]:
        self.calls.append(("list_opportunities", kwargs))
        items = [
            OpportunityView(
                page_id=TARGET_ID,
                canonical_url="https://site.test/reports/li",
                title="Li",
                kind=OpportunityKind.ORPHAN,
                score=1.0,
                evidence={"inbound_links": 0},
                run_id=RUN_ID,
                computed_at=NOW,
            )
        ]
        return _list(items, kwargs["limit"], kwargs["offset"])

    async def list_modules(self, **kwargs: Any) -> ListResponse[PageModuleView]:
        self.calls.append(("list_modules", kwargs))
        items = [
            PageModuleView(
                page_id=SOURCE_ID,
                canonical_url="https://site.test/reports/india-ev",
                title="India Electric Vehicles Market",
                kind=ModuleKind.REGIONAL_SIBLING,
                market_core="electric vehicles",
                geography_label="India",
                items=[
                    PageModuleItemView(
                        rank=1,
                        target_page_id=TARGET_ID,
                        target_url="https://site.test/reports/germany-ev",
                        target_title="Germany Electric Vehicles Market",
                        geography_label="Germany",
                        reason_codes=[ReasonCode.REGIONAL_SIBLING_MARKET],
                    )
                ],
                run_id=RUN_ID,
                analysis_version="tax1-alg2",
                computed_at=NOW,
            )
        ]
        return _list(items, kwargs["limit"], kwargs["offset"])

    async def get_summary(self) -> RecommendationSummary:
        self.calls.append(("get_summary", {}))
        return RecommendationSummary(
            plans_by_status={"proposed": 1},
            sources_with_plans=1,
            targets_with_plans=1,
            opportunities_by_kind={"orphan": 1},
            last_run_id=RUN_ID,
            policy_version=1,
            ranking_policy_version=1,
        )


@pytest.fixture
def reader() -> FakeRecommendationReader:
    return FakeRecommendationReader()


@pytest.fixture
def client(reader: FakeRecommendationReader) -> TestClient:
    app = make_app()
    app.dependency_overrides[get_recommendation_reader] = lambda: reader
    return TestClient(app)


def test_the_fake_satisfies_the_protocol(reader: FakeRecommendationReader) -> None:
    assert isinstance(reader, RecommendationReader)


@pytest.mark.parametrize(
    ("path", "model"),
    [
        ("/recommendations", ListResponse[LinkPlan]),
        (f"/recommendations/{PLAN_ID}", LinkPlanDetail),
        ("/recommendations/candidates", ListResponse[CandidateView]),
        ("/recommendations/opportunities", ListResponse[OpportunityView]),
        ("/recommendations/modules", ListResponse[PageModuleView]),
        ("/recommendations/summary", RecommendationSummary),
    ],
)
def test_every_route_returns_its_contract_model(client: TestClient, path: str, model: Any) -> None:
    response = client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    if "page" in body:
        body["page"].pop("has_more")  # computed, output-only
    model.model_validate(body)


def test_the_plan_detail_carries_the_block_and_history(client: TestClient) -> None:
    body = client.get(f"/recommendations/{PLAN_ID}").json()
    assert body["source_block_text"] == BLOCK
    assert [t["to_status"] for t in body["transitions"]] == ["proposed"]
    assert body["score"]["total"] == pytest.approx(0.54)
    assert body["calibration_version"] is None


def test_defaults_and_parameters_reach_the_reader(
    client: TestClient, reader: FakeRecommendationReader
) -> None:
    client.get("/recommendations")
    client.get(
        f"/recommendations?status=approved&source_page_id={SOURCE_ID}"
        f"&target_page_id={TARGET_ID}&min_score=0.4&limit=10&offset=5"
    )
    client.get(f"/recommendations/candidates?run_id={RUN_ID}&rejected=true")
    client.get(f"/recommendations/candidates?source_page_id={SOURCE_ID}&rejected=false")
    client.get("/recommendations/opportunities?kind=underlinked&limit=3")
    calls = list(reader.calls)
    assert calls[0] == (
        "list_plans",
        {
            "limit": 50,
            "offset": 0,
            "status": None,
            "source_page_id": None,
            "target_page_id": None,
            "min_score": None,
        },
    )
    assert calls[1] == (
        "list_plans",
        {
            "limit": 10,
            "offset": 5,
            "status": LinkPlanStatus.APPROVED,
            "source_page_id": SOURCE_ID,
            "target_page_id": TARGET_ID,
            "min_score": 0.4,
        },
    )
    assert calls[2] == (
        "list_candidates",
        {"limit": 50, "offset": 0, "run_id": RUN_ID, "source_page_id": None, "rejected": True},
    )
    assert calls[3][1]["rejected"] is False and calls[3][1]["source_page_id"] == SOURCE_ID
    assert calls[4] == (
        "list_opportunities",
        {"limit": 3, "offset": 0, "kind": OpportunityKind.UNDERLINKED},
    )


def test_static_routes_are_not_taken_for_plan_ids(
    client: TestClient, reader: FakeRecommendationReader
) -> None:
    for path in ("/recommendations/summary", "/recommendations/candidates"):
        assert client.get(path).status_code == 200
    assert all(name != "get_plan" for name, _ in reader.calls)


def test_an_unknown_plan_is_404_without_echoing_the_id(client: TestClient) -> None:
    response = client.get(f"/recommendations/{UNKNOWN}")
    assert response.status_code == 404
    body = ErrorResponse.model_validate(response.json())
    assert body.code.value == "not_found"
    assert str(UNKNOWN) not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/recommendations/not-a-uuid",
        "/recommendations?limit=0",
        "/recommendations?limit=201",
        "/recommendations?offset=-1",
        "/recommendations?status=published",
        "/recommendations?min_score=1.5",
        "/recommendations?min_score=-0.1",
        "/recommendations?source_page_id=x",
        "/recommendations/candidates?run_id=x",
        "/recommendations/candidates?rejected=maybe",
        "/recommendations/candidates?limit=201",
        "/recommendations/opportunities?kind=hub",
        "/recommendations/opportunities?offset=-1",
    ],
)
def test_invalid_parameters_are_422_error_responses(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 422, response.text
    body = ErrorResponse.model_validate(response.json())
    assert body.code.value == "validation_failed"


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize(
    "path",
    [
        "/recommendations",
        f"/recommendations/{PLAN_ID}",
        "/recommendations/candidates",
        "/recommendations/opportunities",
        "/recommendations/modules",
        "/recommendations/summary",
    ],
)
def test_there_are_no_mutation_routes(client: TestClient, method: str, path: str) -> None:
    response = client.request(method.upper(), path)
    assert response.status_code == 405
    ErrorResponse.model_validate(response.json())


REVIEW_POSTS = {
    "/recommendations/{plan_id}/approve",
    "/recommendations/{plan_id}/reject",
    "/recommendations/{plan_id}/edit-anchor",
    "/recommendations/{plan_id}/replace-target",
    "/recommendations/bulk-review",
}
"""The ONLY non-GET recommendation routes: the Phase 5 review actions (ADR-0013 s7)."""


def test_every_recommendation_route_is_get_only_except_the_review_actions() -> None:
    paths = make_app().openapi()["paths"]
    ours = {
        path: set(operations)
        for path, operations in paths.items()
        if path.startswith("/recommendations")
    }
    assert {p for p, m in ours.items() if m != {"get"}} == REVIEW_POSTS
    assert all(ours[p] == {"post"} for p in REVIEW_POSTS), ours
    gets = {p for p, m in ours.items() if m == {"get"}}
    assert gets == {
        "/recommendations",
        "/recommendations/{plan_id}",
        "/recommendations/candidates",
        "/recommendations/opportunities",
        "/recommendations/modules",
        "/recommendations/summary",
        "/recommendations/{plan_id}/target-options",
        "/recommendations/{plan_id}/review-actions",
        "/recommendations/{plan_id}/deployments",
    }


async def test_the_default_dependency_builds_the_sql_reader() -> None:
    from ken_services.recommendations.reader import SqlRecommendationReader

    settings = make_settings()
    engine = create_engine(settings)  # creating an engine performs no I/O
    try:
        reader = get_recommendation_reader(settings, create_sessionmaker(engine))
        assert isinstance(reader, SqlRecommendationReader)
        assert isinstance(reader, RecommendationReader)
    finally:
        await engine.dispose()
