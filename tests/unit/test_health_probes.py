"""Readiness probes and aggregation, against protocol fakes (no I/O)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ken_contracts.common import CheckKind, HealthStatus
from ken_contracts.health import DependencyCheck, ReadinessStatus
from ken_contracts.model import ModelRole
from ken_core.health.aggregate import aggregate_status, run_probes
from ken_core.health.probes import (
    CHECK_NAMES,
    Probe,
    ProbeResult,
    build_probes,
    probe_cms,
    probe_migrations,
    probe_model_provider,
)
from tests.conftest import FakeCMSProvider, FakeModelProvider, make_settings


class BrokenSessionmaker:
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("connection to postgresql://ken:hunter2@db failed")


def _check(status: HealthStatus, *, required: bool) -> DependencyCheck:
    return DependencyCheck(
        name="x", kind=CheckKind.DATABASE, required=required, status=status, latency_ms=0.0
    )


# ------------------------------------------------------------------ build_probes
def test_check_names_are_frozen() -> None:
    probes = build_probes(
        settings=make_settings(),
        sessionmaker=BrokenSessionmaker(),  # type: ignore[arg-type]
        model_provider=FakeModelProvider(),
        cms_provider=FakeCMSProvider(),
    )
    assert [p.name for p in probes] == list(CHECK_NAMES)
    assert CHECK_NAMES == ("postgres", "migrations", "pgvector", "model_provider", "cms")
    kinds = {p.name: p.kind for p in probes}
    assert kinds == {
        "postgres": CheckKind.DATABASE,
        "migrations": CheckKind.SCHEMA,
        "pgvector": CheckKind.EXTENSION,
        "model_provider": CheckKind.MODEL_SERVER,
        "cms": CheckKind.CMS,
    }


@pytest.mark.parametrize(("pgvector", "ollama"), [(False, False), (True, True), (True, False)])
def test_required_flags_follow_settings(pgvector: bool, ollama: bool) -> None:
    probes = build_probes(
        settings=make_settings(require_pgvector=pgvector, require_ollama=ollama),
        sessionmaker=BrokenSessionmaker(),  # type: ignore[arg-type]
        model_provider=FakeModelProvider(),
        cms_provider=FakeCMSProvider(),
    )
    required = {p.name: p.required for p in probes}
    assert required == {
        "postgres": True,
        "migrations": True,
        "pgvector": pgvector,
        "model_provider": ollama,
        "cms": True,
    }


async def test_database_probes_with_broken_sessionmaker_become_error_checks() -> None:
    probes = build_probes(
        settings=make_settings(),
        sessionmaker=BrokenSessionmaker(),  # type: ignore[arg-type]
        model_provider=FakeModelProvider(),
        cms_provider=FakeCMSProvider(),
    )
    checks = {c.name: c for c in await run_probes(probes, timeout_ms=1000)}
    for name in ("postgres", "migrations", "pgvector"):
        assert checks[name].status == HealthStatus.ERROR
        assert "hunter2" not in (checks[name].error or "")
    assert checks["cms"].status == HealthStatus.OK
    assert aggregate_status(list(checks.values())) is ReadinessStatus.NOT_READY


# -------------------------------------------------------------- model_provider
async def test_model_provider_all_roles_ok() -> None:
    result = await probe_model_provider(FakeModelProvider())
    assert result.status is HealthStatus.OK
    assert result.detail["provider"] == "fake-model"
    assert result.detail["roles"]["fast"] == {"model": "model-fast", "status": "ok"}


async def test_model_provider_missing_embedding_is_degraded() -> None:
    provider = FakeModelProvider(
        role_status={
            ModelRole.FAST: "ok",
            ModelRole.REASONING: "ok",
            ModelRole.EMBEDDING: "missing",
        }
    )
    result = await probe_model_provider(provider)
    assert result.status is HealthStatus.DEGRADED
    assert result.detail["roles"]["embedding"]["status"] == "missing"
    assert result.error is not None and "embedding" in result.error


async def test_model_provider_unreachable_is_unavailable_without_echoing_error() -> None:
    result = await probe_model_provider(FakeModelProvider(reachable=False))
    assert result.status is HealthStatus.UNAVAILABLE
    assert "secret-host" not in (result.error or "")


# ------------------------------------------------------------------------- cms
async def test_cms_probe() -> None:
    assert (await probe_cms(FakeCMSProvider())).status is HealthStatus.OK
    down = await probe_cms(FakeCMSProvider(reachable=False))
    assert down.status is HealthStatus.UNAVAILABLE
    assert down.detail == {"provider": "fake-cms"}


# ------------------------------------------------------------------ migrations
async def test_migrations_probe_with_missing_ini_raises_and_runner_reports_error(
    tmp_path: Any,
) -> None:
    probe = Probe(
        "migrations",
        CheckKind.SCHEMA,
        True,
        lambda: probe_migrations(
            BrokenSessionmaker(),  # type: ignore[arg-type]
            alembic_ini_path=str(tmp_path / "missing.ini"),
        ),
    )
    (check,) = await run_probes([probe], timeout_ms=1000)
    assert check.status == HealthStatus.ERROR


# --------------------------------------------------------------------- runner
async def test_runner_preserves_order_and_measures_latency() -> None:
    async def slow_ok() -> ProbeResult:
        await asyncio.sleep(0.05)
        return ProbeResult(status=HealthStatus.OK, detail={"a": 1})

    async def fast_ok() -> ProbeResult:
        return ProbeResult(status=HealthStatus.OK)

    checks = await run_probes(
        [
            Probe("slow", CheckKind.DATABASE, True, slow_ok),
            Probe("fast", CheckKind.CMS, False, fast_ok),
        ],
        timeout_ms=1000,
    )
    assert [c.name for c in checks] == ["slow", "fast"]
    assert checks[0].latency_ms >= 40
    assert checks[0].detail == {"a": 1}
    assert checks[1].required is False


async def test_runner_timeout_is_error() -> None:
    async def hang() -> ProbeResult:
        await asyncio.sleep(10)
        return ProbeResult(status=HealthStatus.OK)

    (check,) = await run_probes([Probe("hang", CheckKind.CMS, True, hang)], timeout_ms=30)
    assert check.status == HealthStatus.ERROR
    assert check.error is not None and "timed out" in check.error
    assert check.latency_ms < 1000


# ------------------------------------------------------------------ aggregate
@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([], ReadinessStatus.READY),
        ([(HealthStatus.OK, True), (HealthStatus.OK, False)], ReadinessStatus.READY),
        ([(HealthStatus.OK, True), (HealthStatus.UNAVAILABLE, False)], ReadinessStatus.DEGRADED),
        ([(HealthStatus.OK, True), (HealthStatus.DEGRADED, False)], ReadinessStatus.DEGRADED),
        ([(HealthStatus.OK, True), (HealthStatus.SKIPPED, False)], ReadinessStatus.DEGRADED),
        ([(HealthStatus.ERROR, True), (HealthStatus.OK, False)], ReadinessStatus.NOT_READY),
        ([(HealthStatus.DEGRADED, True)], ReadinessStatus.NOT_READY),
        (
            [(HealthStatus.UNAVAILABLE, True), (HealthStatus.ERROR, False)],
            ReadinessStatus.NOT_READY,
        ),
    ],
)
def test_aggregate_status(
    checks: list[tuple[HealthStatus, bool]], expected: ReadinessStatus
) -> None:
    assert aggregate_status([_check(s, required=r) for s, r in checks]) is expected
