"""The shared state of one demo run, and the few helpers every stage uses."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ken_contracts.auth import Principal
from ken_contracts.model import ModelProvider
from ken_contracts.vectors import VectorStore
from ken_core.config import Settings
from ken_core.providers.cms.simulated import SimulatedCMSProvider
from ken_services.analytics.collector.app import CollectorApp
from ken_services.demo.config import DemoConfig
from ken_services.demo.console import Console

__all__ = [
    "Ctx",
    "DemoInvariantError",
    "RecordingCMS",
    "fetchall",
    "fetchone",
    "need",
    "scalar",
    "short",
]

type Row = Any


class DemoInvariantError(RuntimeError):
    """A fact the demo just wrote or read back is not there. The run stops."""


def need[T](value: T | None, what: str = "a row the demo just wrote") -> T:
    if value is None:
        raise DemoInvariantError(f"missing: {what}")
    return value


class RecordingCMS:
    """The simulated CMS provider plus a ledger of every WRITE call that reached it.

    It wraps the real provider and changes nothing about its behaviour: it is how the demo
    proves "zero CMS calls" for a blocked plan (the ledger did not move) rather than asserting
    it from the absence of a database row.
    """

    def __init__(self, inner: SimulatedCMSProvider) -> None:
        self.inner = inner
        self.name = inner.name
        self.applied: list[Any] = []
        self.rolled_back: list[str] = []

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)

    async def apply_link_changes(self, *, request: Any) -> Any:
        self.applied.append(request)
        return await self.inner.apply_link_changes(request=request)

    async def rollback(self, *, deployment_id: str, operation_id: Any, actor: str) -> Any:
        self.rolled_back.append(deployment_id)
        return await self.inner.rollback(
            deployment_id=deployment_id, operation_id=operation_id, actor=actor
        )

    @property
    def writes(self) -> int:
        return len(self.applied) + len(self.rolled_back)


@dataclass
class Ctx:
    config: DemoConfig
    out: Console
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    settings: Settings
    model: ModelProvider
    store: VectorStore
    sim: SimulatedCMSProvider
    cms: RecordingCMS
    site_url: str
    collector_url: str
    collector_app: CollectorApp
    principal: Principal
    human_login: bool
    state: dict[str, Any] = field(default_factory=dict)


async def fetchall(ctx: Ctx, sql: str, **params: Any) -> list[Row]:
    async with ctx.sessionmaker() as session:
        found = list((await session.execute(text(sql), params)).all())
        await session.rollback()
    return found


async def fetchone(ctx: Ctx, sql: str, **params: Any) -> Row | None:
    found = await fetchall(ctx, sql, **params)
    return found[0] if found else None


async def scalar(ctx: Ctx, sql: str, **params: Any) -> Any:
    row = await fetchone(ctx, sql, **params)
    return None if row is None else row[0]


def short(value: uuid.UUID | str) -> str:
    return str(value)[:8]


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)
