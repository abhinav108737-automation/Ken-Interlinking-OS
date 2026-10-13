"""The Ken worker: a heartbeat tick loop (docs/adr/ADR-0005.md).

Run with ``python -m ken_agents.worker`` (add ``--once`` for a single tick).

What it does, each tick:

1. runs the readiness probes (the same set as ``GET /ready``);
2. emits ONE structured ``worker.heartbeat`` log line;
3. appends ONE ``worker.heartbeat`` row to the ``events`` table and commits.

What it deliberately does NOT do (ADR-0005): own a queue, poll for work, make
decisions, call a model, import LangGraph or open a port. When real work
arrives (the Phase 1 crawl scheduler, the Phase 4 LangGraph runner) it is
designed deliberately, on the PostgreSQL-backed event bus, not grown here by
accident.

Failure policy: a dependency blip never kills the process. A failed tick is
logged and the loop continues; a failed heartbeat write is a warning.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID, uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.cms import CMSProvider
from ken_contracts.events import EventEnvelope, EventName
from ken_contracts.health import ReadinessStatus
from ken_contracts.model import ModelProvider
from ken_core import logging as ken_logging
from ken_core.config import Settings, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.repositories.events import append_event
from ken_core.db.session import create_sessionmaker
from ken_core.health.aggregate import aggregate_status, run_probes
from ken_core.health.probes import Probe, build_probes
from ken_core.providers.cms.factory import build_cms_provider
from ken_core.providers.model.factory import build_model_provider

EMITTED_BY = "ken-worker"
SUBJECT_TYPE = "service"
SUBJECT_ID = "ken-worker"

# Explicit name: under `python -m` __name__ is "__main__".
logger = ken_logging.get_logger("ken_agents.worker")

AppendEvent = Callable[[AsyncSession, EventEnvelope], Awaitable[None]]


class _Disposable(Protocol):
    async def dispose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TickResult:
    """Outcome of one tick, for ``--once`` and for tests."""

    tick: int
    correlation_id: UUID
    status: ReadinessStatus | None
    checks: dict[str, str]
    duration_ms: float
    heartbeat_written: bool
    event_id: UUID | None = None


@dataclass(slots=True)
class WorkerResources:
    """Everything a running worker holds. :func:`run` closes all of it on exit.

    ``probes`` defaults to :func:`build_probes` over the other resources; tests
    inject their own. ``engine`` is optional so tests can run without one.
    """

    sessionmaker: async_sessionmaker[AsyncSession]
    model_provider: ModelProvider
    cms_provider: CMSProvider
    engine: _Disposable | None = None
    probes: Sequence[Probe] | None = None
    append: AppendEvent = field(default=append_event)


def build_resources(settings: Settings) -> WorkerResources:
    """Construct engine, sessionmaker and providers. Performs no I/O."""
    engine = create_engine(settings)
    return WorkerResources(
        sessionmaker=create_sessionmaker(engine),
        model_provider=build_model_provider(settings),
        cms_provider=build_cms_provider(settings),
        engine=engine,
    )


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000.0)


async def run_tick(
    *,
    tick: int,
    probes: Sequence[Probe],
    sessionmaker: async_sessionmaker[AsyncSession],
    timeout_ms: int,
    append: AppendEvent = append_event,
) -> TickResult:
    """Probe, log one heartbeat line, append one heartbeat event.

    Never raises for a dependency failure: probes are already exception-safe
    (``run_probes``) and a failed write is logged at warning and reported via
    ``heartbeat_written=False``.
    """
    correlation_id = uuid4()
    with structlog.contextvars.bound_contextvars(correlation_id=str(correlation_id)):
        started = time.perf_counter()
        checks = await run_probes(probes, timeout_ms=timeout_ms)
        status = aggregate_status(checks)
        statuses = {check.name: check.status.value for check in checks}
        duration_ms = round(_elapsed_ms(started), 1)

        log = logger.warning if status is ReadinessStatus.NOT_READY else logger.info
        log(
            "worker.heartbeat",
            status=status.value,
            checks=statuses,
            tick=tick,
            duration_ms=duration_ms,
        )

        envelope = EventEnvelope(
            event_name=EventName.WORKER_HEARTBEAT,
            emitted_by=EMITTED_BY,
            subject_type=SUBJECT_TYPE,
            subject_id=SUBJECT_ID,
            payload={"status": status.value, "checks": statuses, "tick": tick},
            correlation_id=correlation_id,
        )
        try:
            async with sessionmaker() as session:
                await append(session, envelope)
                await session.commit()
        except Exception as exc:
            # Class name only: driver messages can carry host and user names.
            logger.warning("worker.heartbeat_write_failed", tick=tick, error=type(exc).__name__)
            return TickResult(
                tick=tick,
                correlation_id=correlation_id,
                status=status,
                checks=statuses,
                duration_ms=duration_ms,
                heartbeat_written=False,
            )

    return TickResult(
        tick=tick,
        correlation_id=correlation_id,
        status=status,
        checks=statuses,
        duration_ms=duration_ms,
        heartbeat_written=True,
        event_id=envelope.event_id,
    )


def _install_signal_handlers(stop: asyncio.Event) -> list[signal.Signals]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []

    def _on_signal(sig: signal.Signals) -> None:
        logger.info("worker.signal_received", signal=sig.name)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - non-Unix loops
            continue
        installed.append(sig)
    return installed


async def _close(resources: WorkerResources) -> None:
    """Release every resource; one failure must not leak the others."""
    closers: list[tuple[str, Callable[[], Awaitable[None]]]] = [
        ("model_provider", resources.model_provider.aclose),
        ("cms_provider", resources.cms_provider.aclose),
    ]
    if resources.engine is not None:
        closers.append(("engine", resources.engine.dispose))
    for name, close in closers:
        try:
            await close()
        except Exception as exc:
            logger.warning("worker.close_failed", resource=name, error=type(exc).__name__)


async def run(
    settings: Settings,
    *,
    once: bool = False,
    stop: asyncio.Event | None = None,
    resources: WorkerResources | None = None,
    handle_signals: bool = True,
) -> int:
    """Run the tick loop until ``stop`` is set (or one tick, with ``once``).

    Takes ownership of ``resources`` (built from ``settings`` when omitted) and
    closes them on exit. Returns the process exit code: with ``once``, 0 only if
    the heartbeat was written; otherwise 0 after a graceful stop.
    """
    stop = stop if stop is not None else asyncio.Event()
    resources = resources if resources is not None else build_resources(settings)
    probes = (
        resources.probes
        if resources.probes is not None
        else build_probes(
            settings=settings,
            sessionmaker=resources.sessionmaker,
            model_provider=resources.model_provider,
            cms_provider=resources.cms_provider,
        )
    )
    installed = _install_signal_handlers(stop) if handle_signals else []
    logger.info(
        "worker.started",
        once=once,
        tick_seconds=settings.worker_tick_seconds,
        model_provider=resources.model_provider.name,
        cms_provider=resources.cms_provider.name,
    )

    last: TickResult | None = None
    tick = 0
    try:
        while once or not stop.is_set():
            tick += 1
            try:
                last = await run_tick(
                    tick=tick,
                    probes=probes,
                    sessionmaker=resources.sessionmaker,
                    timeout_ms=settings.readiness_timeout_ms,
                    append=resources.append,
                )
            except Exception as exc:
                logger.error(
                    "worker.tick_failed", tick=tick, error=type(exc).__name__, exc_info=True
                )
                last = None
            if once:
                break
            # Wait on the event, not a bare sleep, so shutdown is prompt.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_tick_seconds)
    finally:
        loop = asyncio.get_running_loop()
        for sig in installed:
            loop.remove_signal_handler(sig)
        logger.info("worker.stopping", ticks=tick)
        await _close(resources)
        logger.info("worker.stopped", ticks=tick)

    if once:
        return 0 if last is not None and last.heartbeat_written else 1
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ken_agents.worker",
        description="Ken worker: readiness heartbeat tick loop (ADR-0005).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run exactly one tick, then exit (0 if the heartbeat was written, else 1)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    ken_logging.configure_logging(level=settings.log_level, fmt=settings.log_format)
    return asyncio.run(run(settings, once=args.once))


if __name__ == "__main__":
    raise SystemExit(main())
