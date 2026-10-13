"""Worker tick loop (ADR-0005): no DB, no network -- fakes for everything."""

from __future__ import annotations

import ast
import asyncio
import inspect
import time
from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from ken_agents import worker
from ken_agents.worker import TickResult, WorkerResources, run, run_tick
from ken_contracts.common import CheckKind, HealthStatus
from ken_contracts.events import EventEnvelope, EventName
from ken_contracts.health import ReadinessStatus
from ken_core import logging as ken_logging
from ken_core.config import Settings
from ken_core.health.probes import Probe, ProbeResult
from tests.conftest import FakeCMSProvider, FakeModelProvider


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"worker_tick_seconds": 0.01, "readiness_timeout_ms": 500}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


class FakeSession:
    def __init__(self, store: FakeSessionmaker) -> None:
        self.store = store

    async def __aenter__(self) -> FakeSession:
        if self.store.fail:
            raise ConnectionRefusedError("db down at postgres://secret@host")
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        self.store.commits += 1


class FakeSessionmaker:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.commits = 0

    def __call__(self) -> FakeSession:
        return FakeSession(self)


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = 0

    async def dispose(self) -> None:
        self.disposed += 1


class Appender:
    def __init__(self) -> None:
        self.envelopes: list[EventEnvelope] = []

    async def __call__(self, session: AsyncSession, envelope: EventEnvelope) -> None:
        self.envelopes.append(envelope)


def _probe(name: str, status: HealthStatus = HealthStatus.OK, *, required: bool = True) -> Probe:
    async def body() -> ProbeResult:
        return ProbeResult(status=status)

    return Probe(name, CheckKind.DATABASE, required, body)


def _raising_probe(name: str) -> Probe:
    async def body() -> ProbeResult:
        raise ConnectionRefusedError("db down")

    return Probe(name, CheckKind.DATABASE, True, body)


def _resources(
    *,
    sessionmaker: FakeSessionmaker | None = None,
    probes: list[Probe] | None = None,
    appender: Appender | None = None,
) -> tuple[WorkerResources, FakeModelProvider, FakeCMSProvider, FakeEngine]:
    model, cms, engine = FakeModelProvider(), FakeCMSProvider(), FakeEngine()
    resources = WorkerResources(
        sessionmaker=sessionmaker or FakeSessionmaker(),  # type: ignore[arg-type]
        model_provider=model,
        cms_provider=cms,
        engine=engine,
        probes=probes if probes is not None else [_probe("postgres"), _probe("cms")],
        append=appender or Appender(),
    )
    return resources, model, cms, engine


# ------------------------------------------------------------------ run_tick
async def test_tick_logs_and_appends_one_heartbeat() -> None:
    appender, sessions = Appender(), FakeSessionmaker()
    probes = [_probe("postgres"), _probe("model_provider", HealthStatus.DEGRADED, required=False)]
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        result = await run_tick(
            tick=3,
            probes=probes,
            sessionmaker=sessions,  # type: ignore[arg-type]
            timeout_ms=500,
            append=appender,
        )

    assert result.heartbeat_written is True
    assert result.status is ReadinessStatus.DEGRADED
    assert sessions.commits == 1
    [envelope] = appender.envelopes
    assert envelope.event_name is EventName.WORKER_HEARTBEAT
    assert envelope.emitted_by == "ken-worker"
    assert (envelope.subject_type, envelope.subject_id) == ("service", "ken-worker")
    assert envelope.payload == {
        "status": "degraded",
        "checks": {"postgres": "ok", "model_provider": "degraded"},
        "tick": 3,
    }
    assert envelope.correlation_id == result.correlation_id
    assert result.event_id == envelope.event_id

    heartbeats = [entry for entry in logs if entry["event"] == "worker.heartbeat"]
    assert len(heartbeats) == 1
    line = heartbeats[0]
    assert line["status"] == "degraded"
    assert line["tick"] == 3
    assert line["checks"] == {"postgres": "ok", "model_provider": "degraded"}
    assert line["duration_ms"] >= 0
    assert line["correlation_id"] == str(result.correlation_id)


async def test_each_tick_gets_a_fresh_correlation_id() -> None:
    kwargs: dict[str, Any] = {
        "probes": [_probe("postgres")],
        "sessionmaker": FakeSessionmaker(),
        "timeout_ms": 500,
        "append": Appender(),
    }
    first = await run_tick(tick=1, **kwargs)
    second = await run_tick(tick=2, **kwargs)
    assert first.correlation_id != second.correlation_id
    assert structlog.contextvars.get_contextvars().get("correlation_id") is None


async def test_db_down_tick_does_not_raise_and_reports_not_written() -> None:
    appender = Appender()
    with structlog.testing.capture_logs() as logs:
        result = await run_tick(
            tick=1,
            probes=[_raising_probe("postgres")],
            sessionmaker=FakeSessionmaker(fail=True),  # type: ignore[arg-type]
            timeout_ms=500,
            append=appender,
        )
    assert result.heartbeat_written is False
    assert result.status is ReadinessStatus.NOT_READY
    assert result.checks == {"postgres": "error"}
    [failure] = [e for e in logs if e["event"] == "worker.heartbeat_write_failed"]
    assert failure["log_level"] == "warning"
    assert failure["error"] == "ConnectionRefusedError"
    assert "secret" not in str(logs)


# ----------------------------------------------------------------------- run
async def test_once_exit_code_zero_when_heartbeat_written() -> None:
    resources, *_ = _resources()
    assert await run(_settings(), once=True, resources=resources, handle_signals=False) == 0
    assert len(resources.append.envelopes) == 1  # type: ignore[attr-defined]


async def test_once_exit_code_nonzero_when_heartbeat_not_written() -> None:
    resources, *_ = _resources(sessionmaker=FakeSessionmaker(fail=True))
    assert await run(_settings(), once=True, resources=resources, handle_signals=False) == 1


async def test_once_exit_code_nonzero_when_tick_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(**_: object) -> TickResult:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(worker, "run_tick", boom)
    resources, *_ = _resources()
    assert await run(_settings(), once=True, resources=resources, handle_signals=False) == 1


async def test_failed_ticks_do_not_stop_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    stop = asyncio.Event()
    calls: list[int] = []
    real_run_tick = worker.run_tick

    async def flaky(**kwargs: Any) -> TickResult:
        calls.append(kwargs["tick"])
        if kwargs["tick"] == 1:
            raise RuntimeError("tick blew up")
        if kwargs["tick"] >= 3:
            stop.set()
        return await real_run_tick(**kwargs)

    monkeypatch.setattr(worker, "run_tick", flaky)
    # Tick 2 also hits a dead database: still not fatal.
    resources, *_ = _resources(
        sessionmaker=FakeSessionmaker(fail=True), probes=[_raising_probe("postgres")]
    )
    code = await asyncio.wait_for(
        run(_settings(), stop=stop, resources=resources, handle_signals=False), timeout=5
    )
    assert code == 0
    assert calls == [1, 2, 3]


async def test_stop_event_interrupts_the_wait_promptly() -> None:
    stop = asyncio.Event()
    resources, *_ = _resources()
    task = asyncio.create_task(
        run(_settings(worker_tick_seconds=60), stop=stop, resources=resources, handle_signals=False)
    )
    await asyncio.sleep(0.05)  # first tick done, now waiting on the 60s interval
    started = time.perf_counter()
    stop.set()
    code = await asyncio.wait_for(task, timeout=2)
    assert code == 0
    assert time.perf_counter() - started < 1.0
    assert len(resources.append.envelopes) == 1  # type: ignore[attr-defined]


async def test_shutdown_closes_providers_and_disposes_engine() -> None:
    stop = asyncio.Event()
    stop.set()
    resources, model, cms, engine = _resources()
    assert await run(_settings(), stop=stop, resources=resources, handle_signals=False) == 0
    assert (model.closed, cms.closed, engine.disposed) == (1, 1, 1)

    resources, model, cms, engine = _resources()
    await run(_settings(), once=True, resources=resources, handle_signals=False)
    assert (model.closed, cms.closed, engine.disposed) == (1, 1, 1)


async def test_signal_handlers_are_installed_and_removed() -> None:
    import signal

    loop = asyncio.get_running_loop()
    resources, *_ = _resources()
    assert await run(_settings(), once=True, resources=resources, handle_signals=True) == 0
    # Removed again on exit: remove_signal_handler returns False when none is set.
    assert loop.remove_signal_handler(signal.SIGTERM) is False
    assert loop.remove_signal_handler(signal.SIGINT) is False


def test_main_parses_once(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    async def fake_run(settings: Settings, *, once: bool = False, **_: object) -> int:
        seen["once"] = once
        return 7

    monkeypatch.setattr(worker, "run", fake_run)
    monkeypatch.setattr(ken_logging, "configure_logging", lambda **_: None)
    assert worker.main(["--once"]) == 7
    assert seen == {"once": True}
    assert worker.main([]) == 7
    assert seen == {"once": False}


# --------------------------------------------------------------- ADR-0005 guard
def test_worker_owns_no_queue_no_langgraph_no_server_no_inference() -> None:
    tree = ast.parse(inspect.getsource(worker))
    imported: set[str] = set()
    attributes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Attribute):
            attributes.add(node.attr)
        elif isinstance(node, ast.Name):
            attributes.add(node.id)

    forbidden_modules = ("langgraph", "langchain", "celery", "redis", "uvicorn", "fastapi")
    assert not [m for m in imported if m.split(".")[0] in forbidden_modules]
    assert not {"Queue", "PriorityQueue", "LifoQueue"} & attributes
    # The model provider is only built and closed, never asked to infer.
    assert not {"complete", "complete_structured", "embed"} & attributes


# --------------------------------------------------------------------------- #
# Heartbeat persistence: first tick + status transitions only (Phase 0 QA L4)
# --------------------------------------------------------------------------- #
def _scripted_probe(statuses: list[HealthStatus], stop: asyncio.Event) -> Probe:
    """Returns the scripted statuses in order; sets ``stop`` after the last one."""
    remaining = list(statuses)

    async def body() -> ProbeResult:
        status = remaining.pop(0)
        if not remaining:
            stop.set()
        return ProbeResult(status=status)

    return Probe("postgres", CheckKind.DATABASE, True, body)


async def _run_scripted(statuses: list[HealthStatus], appender: Appender) -> None:
    stop = asyncio.Event()
    resources, *_ = _resources(probes=[_scripted_probe(statuses, stop)], appender=appender)
    code = await asyncio.wait_for(
        run(_settings(), stop=stop, resources=resources, handle_signals=False), timeout=5
    )
    assert code == 0


async def test_unchanged_status_is_not_persisted_every_tick() -> None:
    appender = Appender()
    await _run_scripted([HealthStatus.OK] * 5, appender)
    assert [e.payload["tick"] for e in appender.envelopes] == [1]


async def test_status_transitions_are_persisted() -> None:
    appender = Appender()
    ok, down = HealthStatus.OK, HealthStatus.UNAVAILABLE
    await _run_scripted([ok, ok, down, down, ok], appender)
    assert [(e.payload["tick"], e.payload["status"]) for e in appender.envelopes] == [
        (1, "ready"),
        (3, "not_ready"),
        (5, "ready"),
    ]


async def test_skipped_write_is_reported_as_skipped_not_failed() -> None:
    appender = Appender()
    result = await run_tick(
        tick=2,
        probes=[_probe("postgres")],
        sessionmaker=FakeSessionmaker(),  # type: ignore[arg-type]
        timeout_ms=500,
        append=appender,
        skip_if_status=ReadinessStatus.READY,
    )
    assert (result.heartbeat_written, result.write_skipped) == (False, True)
    assert appender.envelopes == []


async def test_a_failed_write_is_retried_on_the_next_tick() -> None:
    """A heartbeat that could not be written must not count as persisted."""

    class FlakyAppender(Appender):
        async def __call__(self, session: AsyncSession, envelope: EventEnvelope) -> None:
            if not self.envelopes and not getattr(self, "failed_once", False):
                self.failed_once = True
                raise ConnectionRefusedError("transient")
            await super().__call__(session, envelope)

    appender = FlakyAppender()
    await _run_scripted([HealthStatus.OK] * 3, appender)
    assert [e.payload["tick"] for e in appender.envelopes] == [2]
