"""Dependency probes for ``GET /ready``.

Each probe is a small async function returning a :class:`ProbeResult`. The
runner in :mod:`ken_core.health.aggregate` wraps every probe with a timeout and
a catch-all, so a probe body may raise freely: the exception becomes a
``DependencyCheck`` with status ``error``, never an HTTP 500.

Check names are FROZEN: ``postgres``, ``migrations``, ``pgvector``,
``model_provider``, ``cms``. Clients and dashboards key on them.

Secrets rule (docs/CONTRACTS.md rule 9): ``detail`` and ``error`` never carry
the database URL, credentials or raw exception text.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.cms import CMSProvider
from ken_contracts.common import CheckKind, HealthStatus
from ken_contracts.model import ModelProvider
from ken_core.config import Settings

POSTGRES: Final = "postgres"
MIGRATIONS: Final = "migrations"
PGVECTOR: Final = "pgvector"
MODEL_PROVIDER: Final = "model_provider"
CMS: Final = "cms"
CHECK_NAMES: Final = (POSTGRES, MIGRATIONS, PGVECTOR, MODEL_PROVIDER, CMS)

# Phase in which a migration first runs CREATE EXTENSION vector (ADR-0003).
PGVECTOR_ENABLED_FROM_PHASE: Final = 2


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What a probe body reports. Latency and naming are added by the runner."""

    status: HealthStatus
    detail: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None


ProbeFn = Callable[[], Awaitable[ProbeResult]]


@dataclass(frozen=True, slots=True)
class Probe:
    """One named dependency check, ready to run."""

    name: str
    kind: CheckKind
    required: bool
    run: ProbeFn


SessionFactory = async_sessionmaker[AsyncSession]


# --------------------------------------------------------------------- postgres
async def probe_postgres(sessionmaker: SessionFactory) -> ProbeResult:
    async with sessionmaker() as session:
        row = (
            await session.execute(
                text("SELECT 1, current_setting('server_version'), current_database()")
            )
        ).one()
    return ProbeResult(
        status=HealthStatus.OK,
        detail={"server_version": row[1], "database": row[2]},
    )


# ------------------------------------------------------------------- migrations
def _code_head(alembic_ini_path: str) -> str | None:
    """The single head revision in the migration scripts shipped with this build."""
    script = ScriptDirectory.from_config(Config(alembic_ini_path))
    return script.get_current_head()


async def probe_migrations(sessionmaker: SessionFactory, *, alembic_ini_path: str) -> ProbeResult:
    # Reading the scripts is file I/O; keep it off the event loop.
    code_head = await asyncio.to_thread(_code_head, alembic_ini_path)

    async with sessionmaker() as session:
        has_table: bool = (
            await session.execute(text("SELECT to_regclass('public.alembic_version') IS NOT NULL"))
        ).scalar_one()
        revisions: list[str] = []
        if has_table:
            revisions = list(
                (await session.execute(text("SELECT version_num FROM alembic_version"))).scalars()
            )

    db_revision: str | None = revisions[0] if len(revisions) == 1 else None
    detail = {"code_head": code_head, "db_revision": db_revision}

    if len(revisions) > 1:
        return ProbeResult(
            status=HealthStatus.ERROR,
            detail={**detail, "db_revisions": sorted(revisions)},
            error="database has multiple alembic heads",
        )
    if db_revision is None:
        return ProbeResult(
            status=HealthStatus.UNAVAILABLE,
            detail=detail,
            error="database is not migrated; run `alembic upgrade head`",
        )
    if db_revision != code_head:
        return ProbeResult(
            status=HealthStatus.UNAVAILABLE,
            detail=detail,
            error="database schema revision does not match the code head",
        )
    return ProbeResult(status=HealthStatus.OK, detail=detail)


# --------------------------------------------------------------------- pgvector
async def probe_pgvector(sessionmaker: SessionFactory) -> ProbeResult:
    """Phase 0 asks only whether pgvector is AVAILABLE (provisioned), not enabled.

    Reads catalogue views only; never runs CREATE EXTENSION (ADR-0003).
    """
    async with sessionmaker() as session:
        available_version = (
            await session.execute(
                text("SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")
            )
        ).scalar_one_or_none()
        installed_version = (
            await session.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
        ).scalar_one_or_none()

    detail = {
        "available": available_version is not None,
        "installed_version": installed_version,
        "enabled_from_phase": PGVECTOR_ENABLED_FROM_PHASE,
    }
    if available_version is None:
        return ProbeResult(
            status=HealthStatus.UNAVAILABLE,
            detail=detail,
            error="pgvector extension is not available on this PostgreSQL server",
        )
    return ProbeResult(status=HealthStatus.OK, detail=detail)


# --------------------------------------------------------------- model provider
async def probe_model_provider(provider: ModelProvider) -> ProbeResult:
    """Uses only the frozen ``ModelProvider`` protocol: ``health()`` then ``capabilities()``."""
    health = await provider.health()
    if not health.reachable:
        return ProbeResult(
            status=HealthStatus.UNAVAILABLE,
            detail={"provider": health.provider, "roles": {}},
            # The provider's own error string is not echoed: its content is
            # outside this module's control and could carry a URL.
            error="model server is not reachable",
        )

    capabilities = await provider.capabilities()
    roles = {
        str(role.role): {"model": role.model, "status": role.status} for role in capabilities.roles
    }
    detail = {"provider": capabilities.provider, "roles": roles}

    unsatisfied = sorted(name for name, info in roles.items() if info["status"] != "ok")
    if unsatisfied:
        # e.g. the embedding role in Phase 0: no embedding model is pulled.
        return ProbeResult(
            status=HealthStatus.DEGRADED,
            detail=detail,
            error=f"model roles not satisfied: {', '.join(unsatisfied)}",
        )
    return ProbeResult(status=HealthStatus.OK, detail=detail)


# ------------------------------------------------------------------------- cms
async def probe_cms(provider: CMSProvider) -> ProbeResult:
    health = await provider.health()
    detail = {"provider": health.provider}
    if not health.reachable:
        return ProbeResult(
            status=HealthStatus.UNAVAILABLE, detail=detail, error="CMS is not reachable"
        )
    return ProbeResult(status=HealthStatus.OK, detail=detail)


# ------------------------------------------------------------------------ build
def build_probes(
    *,
    settings: Settings,
    sessionmaker: SessionFactory,
    model_provider: ModelProvider,
    cms_provider: CMSProvider,
) -> list[Probe]:
    """The Phase 0 readiness probe set, with ``required`` computed from settings."""
    return [
        Probe(POSTGRES, CheckKind.DATABASE, True, lambda: probe_postgres(sessionmaker)),
        Probe(
            MIGRATIONS,
            CheckKind.SCHEMA,
            True,
            lambda: probe_migrations(sessionmaker, alembic_ini_path=settings.alembic_ini_path),
        ),
        Probe(
            PGVECTOR,
            CheckKind.EXTENSION,
            settings.require_pgvector,
            lambda: probe_pgvector(sessionmaker),
        ),
        Probe(
            MODEL_PROVIDER,
            CheckKind.MODEL_SERVER,
            settings.require_ollama,
            lambda: probe_model_provider(model_provider),
        ),
        Probe(CMS, CheckKind.CMS, True, lambda: probe_cms(cms_provider)),
    ]
