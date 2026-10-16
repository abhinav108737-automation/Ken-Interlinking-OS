"""Vector index command line (ADR-0017 section 5)::

    python -m ken_services.vectors.cli index   [--force]
    python -m ken_services.vectors.cli rebuild [--batch-size N]
    python -m ken_services.vectors.cli verify

* ``index``   idempotent incremental indexing from PostgreSQL (``make qdrant-index``);
* ``rebuild`` drop the collection and refill it from PostgreSQL (``make qdrant-rebuild``);
* ``verify``  drift report in both directions, changes nothing (``make qdrant-verify``).

Each prints one JSON line. Exit codes: 0 ok / in sync, 1 run errors or DRIFT, 2 usage or
configuration, 3 another index run is in progress. Never prints a URL, a key or exception text.
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
from ken_contracts.vectors import VectorStoreError, VectorStoreNotConfiguredError
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.session import create_sessionmaker
from ken_core.providers.vectors.factory import build_vector_store
from ken_services.jobs.runner import JobAlreadyRunningError
from ken_services.vectors.index import run_index_job, run_rebuild_job
from ken_services.vectors.protocol import DriftReport
from ken_services.vectors.verify import PostgresVectorVerifier

EXIT_OK: Final = 0
EXIT_ERRORS: Final = 1
EXIT_USAGE: Final = 2
EXIT_ALREADY_RUNNING: Final = 3
TRIGGERED_BY: Final = "cli"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_services.vectors.cli",
        description="Keep the derived vector index in step with PostgreSQL.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="Idempotent incremental indexing.")
    index.add_argument("--force", action="store_true", help="Re-send unchanged points too.")
    rebuild = commands.add_parser("rebuild", help="Drop the collection and refill it.")
    rebuild.add_argument("--batch-size", type=int, default=256)
    commands.add_parser("verify", help="Drift report; non-zero exit on drift.")
    return parser


def drift_summary(report: DriftReport) -> dict[str, object]:
    return {
        "collection": report.collection,
        "status": report.collection_info.status.value,
        "points": report.collection_info.points,
        "vector_size": report.collection_info.vector_size,
        "missing_vectors": len(report.missing_vectors),
        "orphaned_points": len(report.orphaned_points),
        "stale_points": len(report.stale_points),
        "identity_mismatch": report.identity_mismatch,
        "dimension_mismatch": report.dimension_mismatch,
        "in_sync": report.in_sync,
    }


async def _run(settings: Settings, args: argparse.Namespace) -> int:
    if settings.vector_store == "memory" and args.command != "verify":
        # A process-local store is filled and discarded with this process: say so.
        print(
            "warning: KEN_VECTOR_STORE=memory is process-local; nothing will persist "
            "(use KEN_VECTOR_STORE=qdrant, as the make targets do)",
            file=sys.stderr,
        )
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    try:
        store = build_vector_store(settings)
    except VectorStoreNotConfiguredError:
        print("the configured vector store is not available in this build", file=sys.stderr)
        await engine.dispose()
        return EXIT_USAGE
    try:
        if args.command == "verify":
            drift = await PostgresVectorVerifier(
                sessionmaker=sessionmaker, store=store, settings=settings
            ).verify()
            print(json.dumps(drift_summary(drift), sort_keys=True))
            return EXIT_OK if drift.in_sync else EXIT_ERRORS
        if args.command == "index":
            detail, report = await run_index_job(
                sessionmaker=sessionmaker,
                store=store,
                settings=settings,
                triggered_by=TRIGGERED_BY,
                force=bool(args.force),
            )
            print(
                json.dumps(
                    {
                        "run_id": str(detail.id),
                        "status": detail.status.value,
                        "counters": detail.counters,
                        "vector_size": None if report is None else report.vector_size,
                        "embedding_model": None if report is None else report.embedding_model,
                        "issues": [] if report is None else list(report.issues),
                    },
                    sort_keys=True,
                )
            )
            return EXIT_OK if detail.status is RunStatus.SUCCEEDED else EXIT_ERRORS
        detail, rebuilt = await run_rebuild_job(
            sessionmaker=sessionmaker,
            store=store,
            settings=settings,
            triggered_by=TRIGGERED_BY,
            batch_size=int(args.batch_size),
        )
        print(
            json.dumps(
                {
                    "run_id": str(detail.id),
                    "status": detail.status.value,
                    "dropped": None if rebuilt is None else rebuilt.dropped,
                    "points_written": None if rebuilt is None else rebuilt.points_written,
                    "vector_size": None if rebuilt is None else rebuilt.vector_size,
                },
                sort_keys=True,
            )
        )
        return EXIT_OK if detail.status is RunStatus.SUCCEEDED else EXIT_ERRORS
    except JobAlreadyRunningError:
        print("another vector index run is in progress; nothing was run", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except VectorStoreError as exc:
        print(f"vector store error ({type(exc).__name__})", file=sys.stderr)
        return EXIT_ERRORS
    except Exception as exc:
        print(f"vector command failed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_ERRORS
    finally:
        await store.aclose()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK
    try:
        settings = get_settings()
    except (ValidationError, ConfigurationError):
        print("configuration error: invalid KEN_* settings", file=sys.stderr)
        return EXIT_USAGE
    ken_logging.configure_logging(level=settings.log_level, fmt=settings.log_format)
    return asyncio.run(_run(settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
