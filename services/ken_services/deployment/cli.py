"""Deployment command line: ``python -m ken_services.deployment.cli reconcile``.

Reconciliation is an operator command, never an HTTP route and never a scheduled agent
(ADR-0014 s8)::

    reconcile [--older-than-s N]

* ``--older-than-s N``  only rows not touched for N seconds (default
                        ``KEN_DEPLOY_RECONCILE_AFTER_S``).

Resends the STORED request of every ``applying`` / ``rolling_back`` deployment byte for byte
through the configured CMS provider (``KEN_CMS_PROVIDER``). A row whose source page is locked
by an in-flight deployment is skipped. Nothing new is deployed.

Refused for ``KEN_CMS_PROVIDER=mock`` (exit 2, nothing run): the mock provider is in-process
and freshly seeded per process, so it holds none of the operations the stored requests were
sent to, and a resend would record fictitious outcomes against durable rows.

Prints one JSON line (the provider name and the :class:`ReconcileSummary` counters). Exit
codes:

    0  the sweep completed and no examined row is left with an unknown outcome
    1  the sweep could not complete, or a row is still ``applying`` / ``rolling_back`` with
       an unknown outcome (the CMS could not answer): run it again later
    2  usage or configuration error (nothing was run)

Never prints a database or CMS URL, a token or exception text: errors name a class only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Sequence
from dataclasses import asdict
from typing import Final

from pydantic import ValidationError

from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.session import create_sessionmaker
from ken_core.providers.cms.factory import build_cms_provider
from ken_services.deployment.compose import build_deployment_service
from ken_services.deployment.service import ReconcileSummary

__all__ = [
    "EXIT_OK",
    "EXIT_RUN_ERRORS",
    "EXIT_USAGE",
    "MOCK_PROVIDER_REFUSAL",
    "exit_code_for",
    "main",
    "summary_line",
]

EXIT_OK: Final = 0
EXIT_RUN_ERRORS: Final = 1
EXIT_USAGE: Final = 2

MOCK_PROVIDER_REFUSAL: Final = (
    "configuration error: reconcile is refused for KEN_CMS_PROVIDER=mock (an in-process, "
    "freshly seeded CMS knows none of the stored operations); use the simulated or http "
    "provider the deployments were made with"
)

MAX_OLDER_THAN_S: Final = 30 * 24 * 3600.0


def _seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number of seconds") from None
    if not math.isfinite(seconds) or seconds < 0 or seconds > MAX_OLDER_THAN_S:
        raise argparse.ArgumentTypeError(f"must be between 0 and {int(MAX_OLDER_THAN_S)} seconds")
    return seconds


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_services.deployment.cli",
        description="Controlled-deployment operator commands (ADR-0014).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    reconcile = commands.add_parser(
        "reconcile", help="Resend the stored request of stuck applying/rolling_back deployments."
    )
    reconcile.add_argument(
        "--older-than-s",
        dest="older_than_s",
        type=_seconds,
        default=None,
        metavar="N",
        help="Only rows not touched for N seconds (default KEN_DEPLOY_RECONCILE_AFTER_S).",
    )
    return parser


def summary_line(summary: ReconcileSummary, *, cms_provider: str) -> str:
    return json.dumps(
        {"command": "reconcile", "cms_provider": cms_provider, **asdict(summary)},
        sort_keys=True,
    )


def exit_code_for(summary: ReconcileSummary) -> int:
    return EXIT_RUN_ERRORS if summary.still_unknown else EXIT_OK


async def _reconcile(settings: Settings, args: argparse.Namespace) -> int:
    if settings.cms_provider == "mock":
        print(MOCK_PROVIDER_REFUSAL, file=sys.stderr)
        return EXIT_USAGE
    try:
        cms = build_cms_provider(settings)
    except (ConfigurationError, ValueError) as exc:
        print(f"configuration error: the CMS provider ({type(exc).__name__})", file=sys.stderr)
        return EXIT_USAGE
    engine = create_engine(settings)  # no I/O until first use
    try:
        service = build_deployment_service(
            sessionmaker=create_sessionmaker(engine), settings=settings, cms=cms
        )
        summary = await service.reconcile(older_than_s=args.older_than_s)
    except Exception as exc:
        print(f"reconciliation could not be completed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_RUN_ERRORS
    finally:
        try:
            await cms.aclose()
        except Exception as exc:
            print(f"CMS provider close failed ({type(exc).__name__})", file=sys.stderr)
        await engine.dispose()
    print(summary_line(summary, cms_provider=settings.cms_provider))
    return exit_code_for(summary)


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
    return asyncio.run(_reconcile(settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
