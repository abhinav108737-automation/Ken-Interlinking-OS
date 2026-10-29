"""Agent command line: ``python -m ken_agents.cli agents`` (``make agents``).

Agent runs start here, never over HTTP (ADR-0012 section 11)::

    agents [--source PAGE_ID ...]

* without ``--source``: trigger ``batch``, over the sources that have unreviewed proposed
  plans (at most ``max_sources_per_batch``);
* ``--source PAGE_ID``: trigger ``manual``, only these sources (repeatable; UUIDs).

Agents REVIEW the deterministic engine's ``proposed`` plans and hand the accepted ones on to
the autonomy engine (``policy_checked``, ADR-0019). Nothing is approved or published. Remote tracing
must be off (ADR-0012 section 9), or nothing runs.

Prints one JSON line (job run id, status, counters) when the batch finished. Exit codes:

    0  batch succeeded
    1  batch completed_with_errors, failed, or could not be recorded
    2  usage or configuration error (incl. remote tracing configured, a policy file)
    3  another agent batch is in progress (nothing was run)

Never prints the database URL or exception text: errors name a class only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, Final

from pydantic import ValidationError

from ken_agents.runner import BatchJob, build_config_snapshot, run_agent_batch
from ken_agents.tracing_guard import RemoteTracingConfiguredError, enforce_no_remote_tracing
from ken_contracts.agents import AgentPolicy
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings

EXIT_OK: Final = 0
EXIT_RUN_ERRORS: Final = 1
EXIT_USAGE: Final = 2
EXIT_ALREADY_RUNNING: Final = 3

TRIGGERED_BY: Final = "cli"


def _page_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a page id (UUID)") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_agents.cli",
        description="Run one agent review batch (reviews proposed plans; never publishes).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    agents = commands.add_parser("agents", help="Review proposed plans and hand them off.")
    agents.add_argument(
        "--source",
        dest="sources",
        action="append",
        type=_page_id,
        default=None,
        metavar="PAGE_ID",
        help="Only this source page (repeatable).",
    )
    return parser


def summary_line(detail: JobRunDetail) -> str:
    return json.dumps(
        {
            "run_id": str(detail.id),
            "kind": detail.kind.value,
            "status": detail.status.value,
            "counters": detail.counters,
            "failure_reason": detail.failure_reason,
        },
        sort_keys=True,
    )


def exit_code_for(detail: JobRunDetail) -> int:
    return EXIT_OK if detail.status is RunStatus.SUCCEEDED else EXIT_RUN_ERRORS


def load_policies(settings: Settings) -> tuple[AgentPolicy, int, int]:
    """The agent policy plus the recommendation and ranking policy versions."""
    from ken_services.agents.policy import load_configured_agent_policy
    from ken_services.recommendations.pipeline import load_policies as load_recommendation

    recommendation, ranking = load_recommendation(settings)
    return load_configured_agent_policy(settings), recommendation.version, ranking.version


async def _run(
    settings: Settings, policies: tuple[AgentPolicy, int, int], args: argparse.Namespace
) -> int:
    # Lazy: the database-backed implementations live in ken_services (ADR-0012 section 7).
    from ken_core.db.engine import create_engine
    from ken_core.db.session import create_sessionmaker
    from ken_core.providers.model.factory import build_model_provider
    from ken_services.agents.backend import DatabaseAgentBackend
    from ken_services.agents.store import DatabaseAgentRunStore
    from ken_services.jobs.runner import JobAlreadyRunningError, run_job

    policy, recommendation_version, ranking_version = policies
    engine = create_engine(settings)  # no I/O until first use
    sessionmaker = create_sessionmaker(engine)
    provider: Any = None

    async def start_job(
        *,
        kind: JobKind,
        triggered_by: str,
        body: Callable[[BatchJob], Awaitable[RunStatus]],
        config_snapshot: Mapping[str, Any],
        counter_names: Iterable[str],
    ) -> JobRunDetail:
        return await run_job(
            kind=kind,
            sessionmaker=sessionmaker,
            triggered_by=triggered_by,
            body=body,
            config_snapshot=config_snapshot,
            counter_names=counter_names,
        )

    try:
        provider = build_model_provider(settings)
        detail = await run_agent_batch(
            start_job=start_job,
            backend=DatabaseAgentBackend(sessionmaker=sessionmaker, settings=settings),
            store=DatabaseAgentRunStore(sessionmaker=sessionmaker),
            provider=provider,
            policy=policy,
            config_snapshot=build_config_snapshot(
                policy=policy,
                recommendation_policy_version=recommendation_version,
                ranking_policy_version=ranking_version,
                provider_configured=True,
            ),
            triggered_by=TRIGGERED_BY,
            source_page_ids=args.sources,
        )
    except JobAlreadyRunningError:
        print("another agent batch is in progress; nothing was run", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except Exception as exc:
        print(f"agent batch could not be completed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_RUN_ERRORS
    finally:
        if provider is not None:
            try:
                await provider.aclose()
            except Exception as exc:
                print(f"model provider close failed ({type(exc).__name__})", file=sys.stderr)
        await engine.dispose()
    print(summary_line(detail))
    return exit_code_for(detail)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse has already printed its usage message
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK
    try:
        enforce_no_remote_tracing()
    except RemoteTracingConfiguredError as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)  # names variables, never values
        return EXIT_USAGE
    try:
        settings = get_settings()
    except (ValidationError, ConfigurationError):
        print("configuration error: invalid KEN_* settings", file=sys.stderr)
        return EXIT_USAGE
    ken_logging.configure_logging(level=settings.log_level, fmt=settings.log_format)
    try:
        policies = load_policies(settings)
    except Exception as exc:  # missing file, invalid YAML or policy: class name only
        print(f"error: a policy could not be loaded ({type(exc).__name__})", file=sys.stderr)
        return EXIT_USAGE
    return asyncio.run(_run(settings, policies, args))


if __name__ == "__main__":
    raise SystemExit(main())
