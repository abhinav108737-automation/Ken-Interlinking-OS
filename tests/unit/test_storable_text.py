"""QA (Phase 5) L1: text PostgreSQL cannot store, or UTF-8 cannot encode, is a 422 at the
boundary -- never a 500 from asyncpg or the password hasher."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ken_api.deps import (
    get_auth_provider_dep,
    get_control_plane_reader,
    get_review_reader,
)
from ken_contracts.auth import LoginRequest
from ken_contracts.review import ApproveRequest, RejectRequest
from tests.conftest import make_app


def _app(*, real_auth: bool = False) -> Any:
    """Dependencies stubbed so a request reaches parameter/body VALIDATION without a DB
    (FastAPI resolves dependencies first; a missing sessionmaker would 500 instead)."""
    app = make_app(real_auth=real_auth)
    for dep in (get_auth_provider_dep, get_control_plane_reader, get_review_reader):
        app.dependency_overrides[dep] = lambda: object()
    return app


BAD = ["a\x00b", "lone \ud800 surrogate", "bell\x07", "esc\x1b[31m", "del\x7f"]


@pytest.mark.parametrize("bad", BAD)
def test_login_username_and_password_reject_unstorable_text(bad: str) -> None:
    with pytest.raises(ValidationError):
        LoginRequest(username=f"user{bad}", password="long-enough-password")
    with pytest.raises(ValidationError):
        LoginRequest(username="good-user", password=f"long-enough-{bad}")


def test_ordinary_whitespace_and_unicode_are_fine() -> None:
    LoginRequest(username="good-user", password="tab\tand newline\nand ünïcødé 🙂")
    ApproveRequest(
        operation_id=uuid.uuid4(), expected_status="awaiting_approval", note="line 1\nline 2"
    )


@pytest.mark.parametrize("bad", BAD)
def test_review_notes_reject_unstorable_text(bad: str) -> None:
    with pytest.raises(ValidationError):
        RejectRequest(
            operation_id=uuid.uuid4(),
            expected_status="awaiting_approval",
            reasons=["other"],
            note=f"see {bad}",
        )


def test_login_with_a_lone_surrogate_is_422_not_500() -> None:
    client = TestClient(_app(real_auth=True), raise_server_exceptions=False)
    body = '{"username": "surr", "password": "\\ud800aaaaaaaaaaaa"}'
    response = client.post(
        "/auth/login", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert "\\ud800" not in response.text


@pytest.mark.parametrize(
    "url",
    ["/events?subject_id=a%00b", "/review/actions?actor=user:a%00b", "/events?subject_id=%1b"],
)
def test_control_characters_in_audit_queries_are_422_not_500(url: str) -> None:
    client = TestClient(_app(), raise_server_exceptions=False)
    response = client.get(url)
    assert response.status_code == 422, response.text
