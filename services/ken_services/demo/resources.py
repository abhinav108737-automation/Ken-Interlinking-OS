"""The demo's disposable resources: two databases, one Qdrant collection, two identities.

Creation and destruction are limited to names that :class:`~ken_services.demo.config.DemoConfig`
has already validated against a closed pattern, and every statement that interpolates a name
does so only after that check (a database name cannot be a bind parameter).
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from datetime import UTC, datetime
from typing import Final

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from ken_contracts.auth import Principal, Role
from ken_core.config import REPO_ROOT
from ken_core.db.repositories import auth as auth_repo
from ken_services.auth.passwords import hash_password
from ken_services.demo.config import (
    COLLECTION_PREFIX,
    HUMAN_USERNAME,
    SERVICE_USERNAME,
    DemoConfig,
    DemoRefusalError,
)

__all__ = [
    "DEMO_ADMIN_ENV",
    "create_identities",
    "delete_collections",
    "drop_databases",
    "migrate",
    "recreate_databases",
]

DEMO_ADMIN_ENV: Final = "KEN_DEMO_ADMIN_PASSWORD"
ALEMBIC_INI: Final = REPO_ROOT / "alembic.ini"


async def _admin(config: DemoConfig, statements: list[str]) -> None:
    engine = create_async_engine(config.admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


def _names(config: DemoConfig) -> list[str]:
    # DemoConfig validated both against a closed pattern in __post_init__; re-asserted here
    # because these two names are interpolated into DDL.
    names = [config.app_db, config.sim_db]
    for name in names:
        if not name.startswith(("ken_demo", "ken_cms_sim_demo")):
            raise DemoRefusalError("refusing to drop a database the demo does not own")
    return names


async def drop_databases(config: DemoConfig) -> None:
    await _admin(
        config, [f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)' for name in _names(config)]
    )


async def recreate_databases(config: DemoConfig) -> None:
    """Drop and create ``ken_demo`` and ``ken_cms_sim_demo`` (nothing else is touched)."""
    names = _names(config)
    await _admin(
        config,
        [f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)' for name in names]
        + [f'CREATE DATABASE "{name}"' for name in names],
    )


def _upgrade(url: str) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


async def migrate(config: DemoConfig) -> None:
    """``alembic upgrade head`` on the demo database. Alembic's async env runs its own event
    loop, so it runs in a worker thread."""
    await asyncio.to_thread(_upgrade, config.app_url)


async def delete_collections(
    config: DemoConfig, *, every_demo_collection: bool = False
) -> list[str]:
    """Delete this run's Qdrant collection; with ``every_demo_collection`` (``demo-clean``),
    every collection prefixed ``ken_demo_``. Nothing else is ever deleted."""
    deleted: list[str] = []
    async with httpx.AsyncClient(
        base_url=config.qdrant_url, timeout=10.0, trust_env=False
    ) as client:
        listing = await client.get("/collections")
        listing.raise_for_status()
        for entry in listing.json().get("result", {}).get("collections", []):
            name = str(entry.get("name", ""))
            owned = name.startswith(COLLECTION_PREFIX) and (
                every_demo_collection or name == config.collection
            )
            if owned:
                response = await client.delete(f"/collections/{name}")
                response.raise_for_status()
                deleted.append(name)
    return sorted(deleted)


async def create_identities(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[Principal, bool]:
    """The autonomous publisher's operator identity, and optionally a human demo login.

    The service identity gets a random password that is generated here, hashed, and dropped
    from memory with this function: it is never printed, logged or stored anywhere else, so
    nobody can log in as it. ``deployments.actor`` needs a real enabled account
    (``^user:`` CHECK), which is all this identity is for.

    ``demo_admin`` is created only when ``KEN_DEMO_ADMIN_PASSWORD`` is set, with THAT value
    (never one chosen here). Returns the service principal and whether a human login exists.
    """
    now = datetime.now(UTC)
    service_id = uuid.uuid4()
    secret = secrets.token_urlsafe(48)
    async with sessionmaker() as session, session.begin():
        await auth_repo.insert_user(
            session,
            user_id=service_id,
            username=SERVICE_USERNAME,
            display_name="Demo autonomy service (no login)",
            role=Role.OPERATOR_ADMIN,
            password_hash=hash_password(secret),
            now=now,
        )
    del secret
    human = os.environ.get(DEMO_ADMIN_ENV)
    if human:
        async with sessionmaker() as session, session.begin():
            await auth_repo.insert_user(
                session,
                user_id=uuid.uuid4(),
                username=HUMAN_USERNAME,
                display_name="Demo admin",
                role=Role.OPERATOR_ADMIN,
                password_hash=hash_password(human),
                now=now,
            )
    principal = Principal(
        user_id=service_id,
        username=SERVICE_USERNAME,
        display_name="Demo autonomy service (no login)",
        role=Role.OPERATOR_ADMIN,
        session_id=uuid.uuid4(),
    )
    return principal, bool(human)
