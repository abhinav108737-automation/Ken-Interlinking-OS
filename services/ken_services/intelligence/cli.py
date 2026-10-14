"""Content-intelligence command line: ``python -m ken_services.intelligence.cli analyze``.

Analysis runs start here, never over HTTP (ADR-0010 section 7)::

    analyze [--force] [--no-embeddings]

* ``--force``          recompute protections and relationships for every page;
                       existing analyses / embeddings are never rewritten
                       (immutable), only missing ones are added.
* ``--no-embeddings``  skip the embedding step (no model call); semantic edges
                       are left as they are.

Prints one JSON line (run id, status, counters) when a run finished. Exit codes:

    0  run succeeded
    1  run completed_with_errors, failed, or could not be recorded
    2  usage or configuration error, e.g. the taxonomy policy (nothing was run)
    3  another analysis run is in progress (nothing was run)

Never prints the database URL or exception text: errors name a class only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any, Final

from pydantic import ValidationError

from ken_contracts.intelligence import TaxonomyPolicy
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobRunDetail
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.session import create_sessionmaker
from ken_core.providers.model.factory import build_model_provider
from ken_services.jobs.runner import JobAlreadyRunningError

EXIT_OK: Final = 0
EXIT_RUN_ERRORS: Final = 1
EXIT_USAGE: Final = 2
EXIT_ALREADY_RUNNING: Final = 3

TRIGGERED_BY: Final = "cli"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_services.intelligence.cli",
        description="Run one content-intelligence analysis job (deterministic; read-only to CMS).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze", help="Analyse, embed and relate stale pages.")
    analyze.add_argument(
        "--force",
        action="store_true",
        help="Recompute protections and relationships for every page.",
    )
    analyze.add_argument(
        "--no-embeddings",
        dest="embeddings",
        action="store_false",
        help="Skip the embedding step (no model call).",
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


def load_taxonomy_policy(settings: Settings) -> TaxonomyPolicy:
    from ken_services.intelligence.pipeline import load_policy

    return load_policy(settings.taxonomy_path)


async def _run(settings: Settings, taxonomy: TaxonomyPolicy, args: argparse.Namespace) -> int:
    from ken_services.intelligence.pipeline import run_analysis

    engine = create_engine(settings)  # no I/O until first use
    provider: Any = None
    try:
        provider = build_model_provider(settings)
        detail = await run_analysis(
            sessionmaker=create_sessionmaker(engine),
            settings=settings,
            model_provider=provider,
            triggered_by=TRIGGERED_BY,
            force=bool(args.force),
            embeddings=bool(args.embeddings),
            taxonomy=taxonomy,
        )
    except JobAlreadyRunningError:
        print("another analysis run is in progress; nothing was run", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except Exception as exc:
        print(f"analysis could not be completed ({type(exc).__name__})", file=sys.stderr)
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
        settings = get_settings()
    except (ValidationError, ConfigurationError):
        print("configuration error: invalid KEN_* settings", file=sys.stderr)
        return EXIT_USAGE
    ken_logging.configure_logging(level=settings.log_level, fmt=settings.log_format)
    try:
        taxonomy = load_taxonomy_policy(settings)
    except Exception as exc:  # missing file, invalid YAML or policy: class name only
        print(
            f"error: the taxonomy policy could not be loaded ({type(exc).__name__})",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return asyncio.run(_run(settings, taxonomy, args))


if __name__ == "__main__":
    raise SystemExit(main())
