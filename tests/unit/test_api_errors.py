"""Exception handlers: every error body is the frozen ErrorResponse."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ken_contracts.errors import ErrorResponse
from tests.conftest import make_app

LEAKY_MESSAGE = "could not connect to postgresql://ken:hunter2@db.internal/ken"


def _app_with_test_routes() -> FastAPI:
    app: FastAPI = make_app()

    @app.get("/_test/boom")
    async def boom() -> None:
        raise RuntimeError(LEAKY_MESSAGE)

    @app.get("/_test/typed")
    async def typed(n: int) -> dict[str, int]:
        return {"n": n}

    return app


def test_404_is_error_response() -> None:
    response = TestClient(make_app()).get("/does-not-exist", headers={"X-Request-ID": "r-404"})
    assert response.status_code == 404
    body = ErrorResponse.model_validate(response.json())
    assert body.code == "not_found"
    assert body.request_id == "r-404"


def test_405_is_error_response_and_keeps_allow_header() -> None:
    response = TestClient(make_app()).post("/health")
    assert response.status_code == 405
    body = ErrorResponse.model_validate(response.json())
    assert body.code == "method_not_allowed"
    assert "GET" in response.headers["allow"]


def test_unhandled_exception_is_500_without_exception_text() -> None:
    client = TestClient(_app_with_test_routes(), raise_server_exceptions=False)
    response = client.get("/_test/boom", headers={"X-Request-ID": "r-500"})

    assert response.status_code == 500
    body = ErrorResponse.model_validate(response.json())
    assert body.code == "internal_error"
    assert body.request_id == "r-500"
    assert response.headers["x-request-id"] == "r-500"
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


def test_unhandled_exception_generates_request_id_when_absent() -> None:
    client = TestClient(_app_with_test_routes(), raise_server_exceptions=False)
    response = client.get("/_test/boom")
    body = ErrorResponse.model_validate(response.json())
    assert body.request_id
    assert response.headers["x-request-id"] == body.request_id


def test_validation_error_is_error_response() -> None:
    response = TestClient(_app_with_test_routes()).get("/_test/typed", params={"n": "secret-value"})
    assert response.status_code == 422
    body = ErrorResponse.model_validate(response.json())
    assert body.code == "validation_failed"
    assert body.details and body.details[0].location == "query.n"
    # The submitted value is not echoed back.
    assert "secret-value" not in response.text


def test_phase_0_exposes_get_only() -> None:
    app = make_app()
    methods = {
        method
        for route in app.routes
        if getattr(route, "path", None) in {"/health", "/ready", "/meta"}
        for method in getattr(route, "methods", set())
    }
    assert methods <= {"GET", "HEAD"}
