"""The live OpenAPI document must match the frozen contracts/openapi/openapi.json.

The comparison is exact for ``paths`` and ``components.schemas`` -- the parts
a generated client depends on -- and for the OpenAPI version and title.

Two decisions behind the exact match:

* No Phase 0 route takes parameters, so FastAPI emits no
  ``HTTPValidationError`` / ``ValidationError`` schemas. If a later route adds
  parameters, FastAPI will add them; that is a contract change and must update
  the frozen file through the orchestrator, not be filtered out here.
* ``ErrorResponse``, ``ErrorDetail`` and ``ErrorCode`` are referenced by no
  Phase 0 path, so ``ken_api.main`` registers them explicitly: every error the
  API returns has that shape.
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
    }
