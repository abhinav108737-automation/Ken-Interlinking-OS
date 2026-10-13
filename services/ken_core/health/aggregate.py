"""Run probes concurrently and reduce them to one readiness verdict."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

from ken_contracts.common import HealthStatus
from ken_contracts.health import DependencyCheck, ReadinessStatus
from ken_core.health.probes import Probe
from ken_core.logging import get_logger

logger = get_logger(__name__)


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000.0)


def _failed(probe: Probe, latency_ms: float, error: str) -> DependencyCheck:
    return DependencyCheck(
        name=probe.name,
        kind=probe.kind,
        required=probe.required,
        status=HealthStatus.ERROR,
        latency_ms=latency_ms,
        error=error,
    )


async def _run_one(probe: Probe, timeout_s: float) -> DependencyCheck:
    """Run one probe. Never raises (except cancellation of the caller itself)."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_s):
            result = await probe.run()
    except TimeoutError:
        logger.warning("readiness_probe_timeout", check=probe.name, timeout_s=timeout_s)
        return _failed(probe, _elapsed_ms(started), f"probe timed out after {timeout_s:g}s")
    except Exception as exc:
        # The real exception goes to the server log only. Its text can carry a
        # host, a user name or SQL, so the payload names its class instead.
        logger.warning("readiness_probe_failed", check=probe.name, exc_info=True)
        return _failed(probe, _elapsed_ms(started), f"probe raised {type(exc).__name__}")

    return DependencyCheck(
        name=probe.name,
        kind=probe.kind,
        required=probe.required,
        status=result.status,
        latency_ms=_elapsed_ms(started),
        detail=dict(result.detail),
        error=result.error,
    )


async def run_probes(probes: Sequence[Probe], *, timeout_ms: int) -> list[DependencyCheck]:
    """Run every probe concurrently, each under its own timeout.

    The result list preserves the order of ``probes``. ``return_exceptions``
    is a second safety net behind :func:`_run_one`'s catch-all: whatever
    escapes still becomes an ``error`` check rather than a 500.
    """
    timeout_s = timeout_ms / 1000.0
    outcomes = await asyncio.gather(
        *(_run_one(probe, timeout_s) for probe in probes), return_exceptions=True
    )
    checks: list[DependencyCheck] = []
    for probe, outcome in zip(probes, outcomes, strict=True):
        if isinstance(outcome, DependencyCheck):
            checks.append(outcome)
        else:
            logger.error("readiness_probe_escaped", check=probe.name, exc_info=outcome)
            checks.append(_failed(probe, 0.0, f"probe raised {type(outcome).__name__}"))
    return checks


def aggregate_status(checks: Sequence[DependencyCheck]) -> ReadinessStatus:
    """``ready`` / ``degraded`` / ``not_ready`` (docs/CONTRACTS.md rule 8).

    * any REQUIRED check not ok          -> not_ready
    * all required ok, some optional not -> degraded
    * everything ok                      -> ready
    """
    if any(check.required and check.status != HealthStatus.OK for check in checks):
        return ReadinessStatus.NOT_READY
    if any(check.status != HealthStatus.OK for check in checks):
        return ReadinessStatus.DEGRADED
    return ReadinessStatus.READY
