"""Recommendation command line: ``python -m ken_services.recommendations.cli recommend``.

Recommendation runs start here, never over HTTP (ADR-0011 section 8)::

    recommend [--source PAGE_ID ...]

* ``--source PAGE_ID``  only these source pages (repeatable; UUIDs). Named sources are
                        processed even when ineligible, so their candidates record why.

Deterministic: no LLM. The configured model provider is asked only for the embedding
IDENTITY (which stored vectors are current); nothing is embedded. When it cannot answer,
the semantic channel is off for the run. Nothing is published: plans are proposals.

Prints one JSON line (run id, status, counters) when a run finished. Exit codes:

    0  run succeeded
    1  run completed_with_errors, failed, or could not be recorded
    2  usage or configuration error, e.g. a policy file (nothing was run)
    3  another recommendation run is in progress (nothing was run)

Never prints the database URL or exception text: errors name a class only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from typing import Any, Final

from pydantic import ValidationError

from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobRunDetail
from ken_contracts.recommendations import RankingPolicy, RecommendationPolicy
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


def _page_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a page id (UUID)") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_services.recommendations.cli",
        description="Run one recommendation job (deterministic; proposes, never publishes).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    recommend = commands.add_parser("recommend", help="Propose contextual internal links.")
    recommend.add_argument(
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


def load_policies(settings: Settings) -> tuple[RecommendationPolicy, RankingPolicy]:
    from ken_services.recommendations.pipeline import load_policies as load

    return load(settings)


async def _run(
    settings: Settings,
    policies: tuple[RecommendationPolicy, RankingPolicy],
    args: argparse.Namespace,
) -> int:
    from ken_services.recommendations.pipeline import run_recommendations

    engine = create_engine(settings)  # no I/O until first use
    provider: Any = None
    try:
        provider = build_model_provider(settings)
        detail = await run_recommendations(
            sessionmaker=create_sessionmaker(engine),
            settings=settings,
            triggered_by=TRIGGERED_BY,
            source_page_ids=args.sources,
            policy=policies[0],
            ranking=policies[1],
            model_provider=provider,
        )
    except JobAlreadyRunningError:
        print("another recommendation run is in progress; nothing was run", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except Exception as exc:
        print(f"recommendation could not be completed ({type(exc).__name__})", file=sys.stderr)
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
        policies = load_policies(settings)
    except Exception as exc:  # missing file, invalid YAML or policy: class name only
        print(
            f"error: the recommendation policy could not be loaded ({type(exc).__name__})",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return asyncio.run(_run(settings, policies, args))


if __name__ == "__main__":
    raise SystemExit(main())
