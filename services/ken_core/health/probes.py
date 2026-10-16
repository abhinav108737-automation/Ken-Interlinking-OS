"""Dependency probes for ``GET /ready``.

Each probe is a small async function returning a :class:`ProbeResult`. The
runner in :mod:`ken_core.health.aggregate` wraps every probe with a timeout and
a catch-all, so a probe body may raise freely: the exception becomes a
``DependencyCheck`` with status ``error``, never an HTTP 500.

Check names are FROZEN: ``postgres``, ``migrations``, ``qdrant``,
``model_provider``, ``cms``. Clients and dashboards key on them. ADR-0017 replaced the
extension check with ``qdrant``: the name stays ``qdrant`` even when the in-process
memory store is configured (``detail.store`` says which one answered), so one stable name
covers the vector layer however it is deployed.

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
from ken_contracts.vectors import CollectionStatus, VectorStore
from ken_core.config import Settings
from ken_core.providers.vectors.factory import build_vector_store

POSTGRES: Final = "postgres"
MIGRATIONS: Final = "migrations"
QDRANT: Final = "qdrant"
MODEL_PROVIDER: Final = "model_provider"
CMS: Final = "cms"
CHECK_NAMES: Final = (POSTGRES, MIGRATIONS, QDRANT, MODEL_PROVIDER, CMS)


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


# ----------------------------------------------------------------------- qdrant
async def probe_vector_store(store: VectorStore) -> ProbeResult:
    """Reachability of the vector layer, plus what the collection was built with.

    ADR-0017 section 5. Uses only the frozen :class:`VectorStore` protocol: ``health()``,
    which may not raise. The payload names the collection, its point count and the embedding
    model/version behind it, because a collection built with ANOTHER model is unusable even
    though it is perfectly reachable -- and that is exactly the failure a probe should
    surface rather than a retrieval run.
    """
    health = await store.health()
    detail: dict[str, Any] = {"store": health.provider, **dict(health.detail)}
    detail.setdefault("collection_present", True)
    if not health.reachable:
        return ProbeResult(
            status=HealthStatus.UNAVAILABLE,
            detail=detail,
            # The provider's own error string is not echoed: it could carry a URL or a key.
            error="vector store is not reachable",
        )
    if detail.get("status") == CollectionStatus.MISSING.value:
        # Reachable, but nothing is indexed yet. This is OK, not a dependency failure, and
        # the distinction matters: ``require_qdrant`` is true by default, so returning
        # DEGRADED here would make every freshly installed system answer /ready 503 before
        # anyone had run `make qdrant-index`. It mirrors Phase 0's "provisioned, not
        # enabled". An unreachable or BROKEN store still makes the service not ready, which
        # is what the required flag is for; index DRIFT is `make qdrant-verify`'s job, not
        # readiness's.
        return ProbeResult(
            status=HealthStatus.OK,
            detail={**detail, "collection_present": False, "indexed": False},
        )
    if detail.get("status") == CollectionStatus.RED.value:
        return ProbeResult(
            status=HealthStatus.UNAVAILABLE,
            detail=detail,
            error="the vector collection is broken; run `make qdrant-rebuild`",
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
    vector_store: VectorStore | None = None,
) -> list[Probe]:
    """The readiness probe set, with ``required`` computed from settings.

    ``vector_store`` is optional so existing callers keep working: when it is omitted the
    configured store is built here (ADR-0017's factory), which for the default
    ``KEN_VECTOR_STORE=memory`` is a cheap in-process object.
    """
    store = vector_store if vector_store is not None else build_vector_store(settings)
    return [
        Probe(POSTGRES, CheckKind.DATABASE, True, lambda: probe_postgres(sessionmaker)),
        Probe(
            MIGRATIONS,
            CheckKind.SCHEMA,
            True,
            lambda: probe_migrations(sessionmaker, alembic_ini_path=settings.alembic_ini_path),
        ),
        Probe(
            QDRANT,
            CheckKind.VECTOR_STORE,
            settings.require_qdrant,
            lambda: probe_vector_store(store),
        ),
        Probe(
            MODEL_PROVIDER,
            CheckKind.MODEL_SERVER,
            settings.require_ollama,
            lambda: probe_model_provider(model_provider),
        ),
        Probe(CMS, CheckKind.CMS, True, lambda: probe_cms(cms_provider)),
    ]
