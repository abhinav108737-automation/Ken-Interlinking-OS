"""Link-graph command line: ``python -m ken_services.graph.cli compute`` (``make graph-metrics``).

Prints one JSON line (run id, status, metrics_version, counters, cluster sizes). Exit codes:
0 succeeded, 1 run failed, 2 usage/configuration/policy, 3 another graph run is in progress.
Never prints the database URL or exception text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Final

from pydantic import ValidationError

from ken_contracts.inventory import RunStatus
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.session import create_sessionmaker
from ken_services.graph.compute import cluster_sizes, run_graph_metrics
from ken_services.graph.policy import GraphPolicyError, load_graph_policy
from ken_services.jobs.runner import JobAlreadyRunningError

EXIT_OK: Final = 0
EXIT_ERRORS: Final = 1
EXIT_USAGE: Final = 2
EXIT_ALREADY_RUNNING: Final = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_services.graph.cli",
        description="Compute internal link-graph metrics into page_graph_metrics.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("compute", help="Compute and persist the metrics (idempotent).")
    return parser


async def _run() -> int:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        detail, result = await run_graph_metrics(
            sessionmaker=create_sessionmaker(engine), triggered_by="cli", policy=load_graph_policy()
        )
    except JobAlreadyRunningError:
        print("another graph run is in progress; nothing was run", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except Exception as exc:
        print(f"graph metrics could not be computed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_ERRORS
    finally:
        await engine.dispose()
    print(
        json.dumps(
            {
                "run_id": str(detail.id),
                "status": detail.status.value,
                "metrics_version": None if result is None else result.metrics_version,
                "counters": detail.counters,
                "cluster_sizes": {} if result is None else cluster_sizes(result),
            },
            sort_keys=True,
        )
    )
    return EXIT_OK if detail.status is RunStatus.SUCCEEDED else EXIT_ERRORS


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK
    try:
        settings = get_settings()
        load_graph_policy()
    except (ValidationError, ConfigurationError, GraphPolicyError):
        print("configuration error: invalid KEN_* settings or graph policy", file=sys.stderr)
        return EXIT_USAGE
    ken_logging.configure_logging(level=settings.log_level, fmt=settings.log_format)
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
