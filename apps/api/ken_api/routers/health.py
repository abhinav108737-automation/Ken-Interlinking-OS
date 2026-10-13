"""``GET /health``, ``GET /ready``, ``GET /meta``.

Paths, operation ids, tags, summaries and response models are FROZEN by
contracts/openapi/openapi.json; tests/contract/test_openapi_snapshot.py fails
if they drift. Route functions carry no docstrings on purpose: FastAPI would
publish them as OpenAPI ``description`` fields the frozen contract lacks.
"""

from __future__ import annotations

import platform
import time
from datetime import UTC, datetime
from typing import Final

from fastapi import APIRouter, Request, Response

from ken_api.deps import IdentityDep, ProbesDep, SettingsDep
from ken_contracts.health import (
    HealthResponse,
    MetaResponse,
    ReadinessResponse,
    ReadinessStatus,
)
from ken_core.health.aggregate import aggregate_status, run_probes

RETRY_AFTER_SECONDS: Final = "5"

router = APIRouter(tags=["system"])


@router.get(
    "/health",
    operation_id="getHealth",
    summary="Liveness. Performs zero I/O.",
    response_model=HealthResponse,
)
async def get_health(request: Request, identity: IdentityDep) -> HealthResponse:
    # ZERO I/O (docs/CONTRACTS.md rule 7). identity was computed once at
    # startup; uptime is arithmetic on a monotonic clock. Adding a database
    # call here turns a transient Postgres blip into a restart loop.
    started: float = request.app.state.started_monotonic
    return HealthResponse(
        identity=identity,
        uptime_s=max(0.0, time.monotonic() - started),
        timestamp=datetime.now(UTC),
    )


@router.get(
    "/ready",
    operation_id="getReady",
    summary="Readiness. Probes every dependency.",
    response_model=ReadinessResponse,
    responses={
        503: {"model": ReadinessResponse, "description": "A required dependency is not ready."}
    },
)
async def get_ready(
    response: Response,
    settings: SettingsDep,
    identity: IdentityDep,
    probes: ProbesDep,
) -> ReadinessResponse:
    started = time.perf_counter()
    checks = await run_probes(probes, timeout_ms=settings.readiness_timeout_ms)
    status = aggregate_status(checks)

    # Readiness is a point-in-time answer; a cached "ready" is a lie.
    response.headers["Cache-Control"] = "no-store"
    if status is ReadinessStatus.NOT_READY:
        response.status_code = 503
        response.headers["Retry-After"] = RETRY_AFTER_SECONDS

    # Same body shape on 200 and 503 (docs/CONTRACTS.md rule 8).
    return ReadinessResponse(
        status=status,
        identity=identity,
        timestamp=datetime.now(UTC),
        duration_ms=max(0.0, (time.perf_counter() - started) * 1000.0),
        checks=checks,
    )


@router.get(
    "/meta",
    operation_id="getMeta",
    summary="Build and non-secret configuration facts.",
    response_model=MetaResponse,
)
async def get_meta(settings: SettingsDep, identity: IdentityDep) -> MetaResponse:
    return MetaResponse(
        identity=identity,
        python_version=platform.python_version(),
        phase=settings.phase,
        auto_approve_enabled=settings.auto_approve_enabled,
        # An explicit allowlist, never a settings dump (docs/CONTRACTS.md rule 9).
        settings=settings.public_settings(),
    )
