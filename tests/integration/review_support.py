"""Phase 5 integration support: users with TEST passwords (test database only), principals,
and real engine plans to review.

Passwords here exist only on the ``*_test`` database and are hashed with cheap scrypt
parameters so the suite stays fast; the production parameters are tested in
tests/unit/test_auth_passwords.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx

from ken_contracts.auth import Principal, Role
from ken_contracts.recommendations import LinkPlanStatus
from ken_core.db.repositories import auth as auth_repo
from ken_services.auth.passwords import ScryptParams, hash_password
from ken_services.auth.provider import LocalAuthProvider
from ken_services.recommendations.transitions import transition
from ken_services.review.service import DatabaseReviewService
from tests.integration.recommendation_support import (
    SETTINGS,
    SM,
    page_ids,
    recommend,
    rows,
    seed_corpus,
)

CHEAP: Final = ScryptParams(n=2**10, r=8, p=1)
PASSWORD: Final = "correct horse battery staple"
"""A TEST-ONLY password (test database)."""
OPEN_SQL: Final = (
    "SELECT * FROM link_plans WHERE status IN ('proposed', 'critiqued', 'policy_checked', "
    "'awaiting_approval', 'approved', 'validated')"
)


def api_client(app: Any, *, raise_errors: bool = True) -> httpx.AsyncClient:
    """An HTTP client that runs the app on the TEST's event loop (the sessionmaker's pool is
    bound to it; ``TestClient`` would run a second loop). No lifespan: state is pre-seeded."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_errors)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


class Clock:
    """A settable clock (UTC)."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


async def create_user(
    sessionmaker: SM,
    username: str = "alice",
    *,
    role: Role = Role.REVIEWER,
    password: str = PASSWORD,
    params: ScryptParams = CHEAP,
) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with sessionmaker() as session, session.begin():
        await auth_repo.insert_user(
            session,
            user_id=user_id,
            username=username,
            display_name=username.title(),
            role=role,
            password_hash=hash_password(password, params=params),
            now=datetime.now(UTC),
        )
    return user_id


async def principal(
    sessionmaker: SM, username: str = "alice", *, role: Role = Role.REVIEWER
) -> Principal:
    """A principal for a REAL user row (review_actions.user_id has a foreign key)."""
    user_id = await create_user(sessionmaker, username, role=role)
    return Principal(
        user_id=user_id,
        username=username,
        display_name=username.title(),
        role=role,
        session_id=uuid.uuid4(),
    )


def auth_provider(sessionmaker: SM, **kwargs: Any) -> LocalAuthProvider:
    return LocalAuthProvider(sessionmaker=sessionmaker, params=CHEAP, **kwargs)


def review_service(
    sessionmaker: SM, *, clock: Callable[[], datetime] | None = None
) -> DatabaseReviewService:
    if clock is None:
        return DatabaseReviewService(sessionmaker=sessionmaker, settings=SETTINGS)
    return DatabaseReviewService(sessionmaker=sessionmaker, settings=SETTINGS, clock=clock)


async def open_plans(sessionmaker: SM) -> dict[tuple[str, str], Any]:
    """(source path, target path) -> OPEN plan row."""
    ids = {v: k for k, v in (await page_ids(sessionmaker)).items()}
    found = await rows(sessionmaker, OPEN_SQL)
    return {(ids[r.source_page_id], ids[r.target_page_id]): r for r in found}


async def seeded(sessionmaker: SM, directory: Path) -> dict[tuple[str, str], Any]:
    """The corpus imported, analysed and recommended; its open plans."""
    await seed_corpus(sessionmaker, directory)
    detail = await recommend(sessionmaker)
    assert detail.counters["errors"] == 0, detail.counters
    return await open_plans(sessionmaker)


async def queue(sessionmaker: SM, plan_id: uuid.UUID) -> None:
    """Hand a proposed plan to the human queue as the agents would (policy gate actor)."""
    async with sessionmaker() as session, session.begin():
        await transition(
            session,
            plan_id=plan_id,
            to_status=LinkPlanStatus.AWAITING_APPROVAL,
            actor="agent:policy-gate@graph-v1",
            expected_from=LinkPlanStatus.PROPOSED,
        )


async def status_of(sessionmaker: SM, plan_id: uuid.UUID) -> LinkPlanStatus:
    found = await rows(sessionmaker, "SELECT status FROM link_plans WHERE id = :p", p=plan_id)
    return LinkPlanStatus(found[0].status)
