"""The live OpenAPI document must match the frozen contracts/openapi/openapi.json.

The comparison is exact for ``paths`` and ``components.schemas`` -- the parts
a generated client depends on -- and for the OpenAPI version and title.

Two decisions behind the exact match:

* Every parameterised route declares its 422 as ``ErrorResponse``, so FastAPI
  emits no ``HTTPValidationError`` / ``ValidationError`` schemas: the API never
  returns that shape (ken_api.errors). A test below keeps it that way.
* ``ken_api.main`` also registers ``ErrorResponse`` explicitly, so the schema
  is published even if no route referenced it.

Regenerate with ``make openapi`` -- a deliberate contract change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ken_api.main import create_app
from tests.conftest import make_settings

FROZEN = Path(__file__).resolve().parents[2] / "contracts/openapi/openapi.json"


@pytest.fixture(scope="module")
def frozen() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(FROZEN.read_text())
    return document


@pytest.fixture(scope="module")
def live() -> dict[str, Any]:
    return create_app(make_settings()).openapi()


def test_paths_match(live: dict[str, Any], frozen: dict[str, Any]) -> None:
    assert live["paths"] == frozen["paths"]


def test_component_schemas_match(live: dict[str, Any], frozen: dict[str, Any]) -> None:
    live_schemas = live["components"]["schemas"]
    frozen_schemas = frozen["components"]["schemas"]
    assert sorted(live_schemas) == sorted(frozen_schemas)
    for name, schema in frozen_schemas.items():
        assert live_schemas[name] == schema, f"component schema {name!r} drifted"


def test_document_header_matches(live: dict[str, Any], frozen: dict[str, Any]) -> None:
    assert live["openapi"] == frozen["openapi"]
    assert live["info"]["title"] == frozen["info"]["title"]


def test_operation_ids_and_tags(live: dict[str, Any]) -> None:
    operations = {
        path: (op["operationId"], op["tags"])
        for path, item in live["paths"].items()
        for op in item.values()
    }
    assert operations == {
        "/health": ("getHealth", ["system"]),
        "/ready": ("getReady", ["system"]),
        "/meta": ("getMeta", ["system"]),
        "/inventory/pages": ("listInventoryPages", ["inventory"]),
        "/inventory/pages/{page_id}": ("getInventoryPage", ["inventory"]),
        "/inventory/pages/{page_id}/versions": ("listInventoryPageVersions", ["inventory"]),
        "/inventory/pages/{page_id}/versions/{version}": (
            "getInventoryPageVersion",
            ["inventory"],
        ),
        "/inventory/pages/{page_id}/links": ("listInventoryPageLinks", ["inventory"]),
        "/inventory/links": ("listInventoryLinks", ["inventory"]),
        "/inventory/runs": ("listIngestionRuns", ["inventory"]),
        "/inventory/runs/{run_id}": ("getIngestionRun", ["inventory"]),
        "/inventory/runs/{run_id}/issues": ("listIngestionRunIssues", ["inventory"]),
        "/intelligence/coverage": ("getIntelligenceCoverage", ["intelligence"]),
        "/intelligence/taxonomy": ("getTaxonomy", ["intelligence"]),
        "/intelligence/taxonomy/{node_id}/pages": ("listTaxonomyNodePages", ["intelligence"]),
        "/intelligence/pages/{page_id}": ("getPageIntelligence", ["intelligence"]),
        "/intelligence/pages/{page_id}/similar": ("listSimilarPages", ["intelligence"]),
        "/intelligence/pages/{page_id}/relationships": ("listPageRelationships", ["intelligence"]),
        "/intelligence/search": ("semanticSearch", ["intelligence"]),
        "/jobs/runs": ("listJobRuns", ["jobs"]),
        "/jobs/runs/{run_id}": ("getJobRun", ["jobs"]),
        "/recommendations": ("listLinkPlans", ["recommendations"]),
        "/recommendations/summary": ("getRecommendationSummary", ["recommendations"]),
        "/recommendations/candidates": ("listLinkCandidates", ["recommendations"]),
        "/recommendations/opportunities": ("listOpportunities", ["recommendations"]),
        "/recommendations/{plan_id}": ("getLinkPlan", ["recommendations"]),
        "/agents/runs": ("listAgentRuns", ["agents"]),
        "/agents/runs/{run_id}": ("getAgentRun", ["agents"]),
        "/agents/plans/{plan_id}/reviews": ("listAgentPlanReviews", ["agents"]),
        "/auth/login": ("login", ["auth"]),
        "/auth/logout": ("logout", ["auth"]),
        "/auth/me": ("getCurrentUser", ["auth"]),
        "/recommendations/{plan_id}/approve": ("approvePlan", ["review"]),
        "/recommendations/{plan_id}/reject": ("rejectPlan", ["review"]),
        "/recommendations/{plan_id}/edit-anchor": ("editPlanAnchor", ["review"]),
        "/recommendations/{plan_id}/replace-target": ("replacePlanTarget", ["review"]),
        "/recommendations/bulk-review": ("bulkReviewPlans", ["review"]),
        "/recommendations/{plan_id}/target-options": ("listPlanTargetOptions", ["review"]),
        "/recommendations/{plan_id}/review-actions": ("listPlanReviewActions", ["review"]),
        "/review/actions": ("listReviewActions", ["review"]),
        "/dashboard": ("getDashboard", ["control-plane"]),
        "/events": ("listEvents", ["control-plane"]),
        "/settings/policies": ("getPolicies", ["control-plane"]),
    }


MUTATIONS = {
    ("post", "/auth/login"),
    ("post", "/auth/logout"),
    ("post", "/recommendations/{plan_id}/approve"),
    ("post", "/recommendations/{plan_id}/reject"),
    ("post", "/recommendations/{plan_id}/edit-anchor"),
    ("post", "/recommendations/{plan_id}/replace-target"),
    ("post", "/recommendations/bulk-review"),
}
"""The ONLY non-GET operations of the whole API (Phase 5, ADR-0013 s7)."""


def test_inventory_api_is_read_only(live: dict[str, Any]) -> None:
    """Inventory, intelligence, job and agent routes are read-only (runs start from the CLI;
    agents: ADR-0012 section 11); recommendations are GET except the frozen review POSTs."""
    methods = {
        method
        for path, item in live["paths"].items()
        for method in item
        if path.startswith(("/inventory", "/intelligence", "/jobs", "/agents"))
    }
    assert methods == {"get"}
    recommendation_writes = {
        (method, path)
        for path, item in live["paths"].items()
        for method in item
        if path.startswith("/recommendations") and method != "get"
    }
    assert recommendation_writes == {m for m in MUTATIONS if m[1].startswith("/recommendations")}


def test_the_only_mutations_are_review_actions_and_login_logout(live: dict[str, Any]) -> None:
    writes = {
        (method, path) for path, item in live["paths"].items() for method in item if method != "get"
    }
    assert writes == MUTATIONS


def test_errors_are_always_error_response(live: dict[str, Any]) -> None:
    """FastAPI's own validation-error shape never appears: every 4xx is ErrorResponse."""
    schemas = live["components"]["schemas"]
    assert "HTTPValidationError" not in schemas
    for path, item in live["paths"].items():
        for operation in item.values():
            for status, response in operation["responses"].items():
                if status.startswith("4"):
                    ref = response["content"]["application/json"]["schema"]["$ref"]
                    assert ref.endswith("/ErrorResponse"), (path, status)
