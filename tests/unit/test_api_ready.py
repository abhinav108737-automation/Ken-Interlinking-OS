"""GET /ready: aggregate readiness and its HTTP mapping (docs/CONTRACTS.md rule 8)."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from ken_contracts.common import CheckKind, HealthStatus
from ken_contracts.health import ReadinessResponse
from ken_core.health.probes import Probe, ProbeResult
from tests.conftest import make_app, make_settings


def _probe(name: str, status: HealthStatus, *, required: bool = True) -> Probe:
    async def run() -> ProbeResult:
        return ProbeResult(status=status, detail={"probe": name})

    return Probe(name, CheckKind.DATABASE, required, run)


def _raising(name: str, *, required: bool = True) -> Probe:
    async def run() -> ProbeResult:
        raise RuntimeError("password=hunter2 at postgresql://u:hunter2@db/x")

    return Probe(name, CheckKind.DATABASE, required, run)


def _slow(name: str, *, required: bool = True) -> Probe:
    async def run() -> ProbeResult:
        await asyncio.sleep(5)
        return ProbeResult(status=HealthStatus.OK)

    return Probe(name, CheckKind.DATABASE, required, run)


def _get(probes: list[Probe], **settings: object) -> tuple[int, dict[str, str], ReadinessResponse]:
    app = make_app(make_settings(**settings), probes=probes)
    response = TestClient(app, raise_server_exceptions=False).get("/ready")
    # Same body shape on every status code.
    return (
        response.status_code,
        dict(response.headers),
        ReadinessResponse.model_validate(response.json()),
    )


def test_all_ok_is_ready_200() -> None:
    code, headers, body = _get(
        [_probe("postgres", HealthStatus.OK), _probe("cms", HealthStatus.OK)]
    )
    assert code == 200
    assert body.status == "ready"
    assert headers["cache-control"] == "no-store"
    assert "retry-after" not in headers
    assert [c.name for c in body.checks] == ["postgres", "cms"]


def test_optional_failure_is_degraded_200() -> None:
    code, headers, body = _get(
        [
            _probe("postgres", HealthStatus.OK),
            _probe("pgvector", HealthStatus.UNAVAILABLE, required=False),
        ]
    )
    assert code == 200
    assert body.status == "degraded"
    assert headers["cache-control"] == "no-store"
    assert "retry-after" not in headers


def test_required_failure_is_not_ready_503_with_retry_after() -> None:
    code, headers, body = _get(
        [_probe("postgres", HealthStatus.UNAVAILABLE), _probe("cms", HealthStatus.OK)]
    )
    assert code == 503
    assert body.status == "not_ready"
    assert headers["retry-after"] == "5"
    assert headers["cache-control"] == "no-store"


def test_required_degraded_check_is_not_ready() -> None:
    code, _, body = _get([_probe("model_provider", HealthStatus.DEGRADED, required=True)])
    assert code == 503
    assert body.status == "not_ready"


def test_raising_probe_is_503_error_never_500() -> None:
    code, headers, body = _get([_raising("postgres"), _probe("cms", HealthStatus.OK)])
    assert code == 503
    assert headers["retry-after"] == "5"
    check = body.checks[0]
    assert check.name == "postgres"
    assert check.status == "error"
    # The exception text never reaches the payload.
    assert "hunter2" not in body.model_dump_json()
    assert check.error == "probe raised RuntimeError"


def test_raising_optional_probe_only_degrades() -> None:
    code, _, body = _get(
        [_probe("postgres", HealthStatus.OK), _raising("pgvector", required=False)]
    )
    assert code == 200
    assert body.status == "degraded"


def test_timeout_is_error() -> None:
    code, _, body = _get(
        [_slow("postgres"), _probe("cms", HealthStatus.OK)], readiness_timeout_ms=50
    )
    assert code == 503
    check = body.checks[0]
    assert check.status == "error"
    assert check.error is not None and "timed out" in check.error
    # The timeout bounds the whole request, not just the probe.
    assert body.duration_ms < 2000


def test_probes_run_concurrently() -> None:
    async def sleepy() -> ProbeResult:
        await asyncio.sleep(0.2)
        return ProbeResult(status=HealthStatus.OK)

    probes = [Probe(f"p{i}", CheckKind.DATABASE, True, sleepy) for i in range(5)]
    code, _, body = _get(probes)
    assert code == 200
    assert body.duration_ms < 800  # serial would be >= 1000
