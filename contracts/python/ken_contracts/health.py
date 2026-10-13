"""Health, readiness and meta response contracts.

FROZEN CONTRACT. The frontend's generated types derive from these models, so a
change here ripples into apps/web.

Two endpoints with deliberately different costs:

``GET /health``  -- liveness. Performs ZERO I/O. Always 200 while the process
                    is alive. If a database query is ever added here, a
                    transient Postgres blip becomes a container restart loop.
``GET /ready``   -- readiness. Probes every dependency concurrently and returns
                    the SAME body shape whether it succeeds or fails, so a
                    client never has to branch on the status code to parse it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from ken_contracts.common import CheckKind, ContractModel, HealthStatus, ServiceIdentity


class ReadinessStatus(StrEnum):
    """Aggregate verdict over all dependency checks.

    The distinction between DEGRADED and NOT_READY is the whole point of the
    ``required`` flag on each check: an optional dependency that is missing
    degrades the service without making it unready. In Phase 0 pgvector and
    Ollama are both optional, so their absence must not take the API down.
    """

    READY = "ready"
    DEGRADED = "degraded"
    NOT_READY = "not_ready"


class DependencyCheck(ContractModel):
    """Result of probing exactly one dependency."""

    name: str = Field(description="Stable identifier, e.g. 'postgres'.")
    kind: CheckKind
    required: bool = Field(
        description=(
            "Whether failure makes the service NOT_READY. Computed from settings "
            "(KEN_REQUIRE_PGVECTOR, KEN_REQUIRE_OLLAMA) so one binary can be "
            "lenient in native development and strict elsewhere."
        )
    )
    status: HealthStatus
    latency_ms: float = Field(ge=0.0, description="Wall-clock duration of this probe.")
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description="Probe-specific facts. Must never contain secrets or credentials.",
    )
    error: str | None = Field(
        default=None, description="Human-readable failure reason when status is not ok."
    )


class HealthResponse(ContractModel):
    """``GET /health`` -- liveness only. No dependency is consulted."""

    status: HealthStatus = Field(default=HealthStatus.OK)
    identity: ServiceIdentity
    uptime_s: float = Field(ge=0.0)
    timestamp: datetime


class ReadinessResponse(ContractModel):
    """``GET /ready`` -- aggregate readiness.

    HTTP status mapping, implemented in the router and asserted by tests:

    ==================  =====  ===============================
    aggregate status    code   headers
    ==================  =====  ===============================
    ready               200    Cache-Control: no-store
    degraded            200    Cache-Control: no-store
    not_ready           503    no-store, Retry-After: 5
    ==================  =====  ===============================

    A probe that itself raises yields 503 with that check marked ERROR -- never
    a 500, because an unhandled exception in a readiness probe is still a
    readiness answer.
    """

    status: ReadinessStatus
    identity: ServiceIdentity
    timestamp: datetime
    duration_ms: float = Field(ge=0.0)
    checks: list[DependencyCheck]


class MetaResponse(ContractModel):
    """``GET /meta`` -- build and configuration facts.

    ``settings`` is an explicit allowlist built in the router. Secrets are
    never placed in this payload; see docs/CONTRACTS.md.
    """

    identity: ServiceIdentity
    python_version: str
    phase: int = Field(description="Implementation phase this build targets.")
    auto_approve_enabled: bool = Field(
        description="Must be false in v1 (TRD.md section 20). Enforced by a Settings validator."
    )
    settings: dict[str, Any] = Field(
        default_factory=dict, description="Non-secret settings allowlist."
    )
