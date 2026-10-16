"""Analytics command line: ``python -m ken_services.analytics.cli`` (ADR-0015 §5).

Imports are CLI-ONLY, like agent runs (ADR-0013 §7): there is no HTTP route that imports, so
no UI action can ever cause Google egress -- which is an ASK-level action and off by default::

    import --source ga4|gsc|first_party --dataset links|pages --start D --end D [--file F]
           [--replace-via]
    aggregate [--start D --end D]
    demo-seed [--seed S]

* ``--source`` is the row's ORIGIN, which is what the ledger stores. ``gsc`` is accepted as
  an alias of ``search_console``.
* ``--file`` makes the run a CSV import (``via = csv``) of one of the two canonical formats;
  its ``source`` column must match ``--source``. Without a file, ``ga4`` and
  ``search_console`` go to the Google APIs (``via = api``, refused unless
  ``KEN_ANALYTICS_GOOGLE_ENABLED``), and ``first_party`` is refused -- first-party rows are
  produced by ``aggregate``, never imported.
* ``aggregate`` turns first-party ``link_events`` into daily rows for the trailing
  ``aggregation.trailing_days`` complete UTC days.
* ``demo-seed`` writes seeded demo data (local/ci only, and never mixed with real rows).

Every run is one ``run_job(ANALYTICS_IMPORT)``, so the existing reaper applies to it and the
kind lock serialises import, aggregation and measurement.

Prints ONE JSON line per run. Exit codes: 0 done; 1 the run was refused or failed (the fixed
error code is printed, nothing else); 2 usage or configuration error (nothing ran). It never
prints a database URL, a token, a file path or exception text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Final

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from ken_contracts.analytics import (
    AnalyticsBatchProvider,
    AnalyticsPolicy,
    AnalyticsSource,
    Dataset,
    DateRange,
    ImportVia,
)
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.session import create_readonly_sessionmaker, create_sessionmaker
from ken_core.providers.analytics.factory import build_analytics_provider
from ken_services.analytics.aggregate import run_aggregation
from ken_services.analytics.importer import run_import
from ken_services.analytics.policy import PolicyError, load_configured_analytics_policy
from ken_services.analytics.protocol import (
    AnalyticsImportError,
    ImportOutcome,
    ImportRequest,
)
from ken_services.analytics.synthetic import DEFAULT_SEED, run_demo_seed
from ken_services.analytics.urls import InventoryUrlResolver
from ken_services.analytics.windows import source_timezone
from ken_services.jobs.runner import JobAlreadyRunningError

__all__ = ["EXIT_OK", "EXIT_RUN_ERROR", "EXIT_USAGE", "SOURCE_ALIASES", "main", "outcome_line"]

EXIT_OK: Final = 0
EXIT_RUN_ERROR: Final = 1
EXIT_USAGE: Final = 2

SOURCE_ALIASES: Final = {
    "ga4": AnalyticsSource.GA4,
    "gsc": AnalyticsSource.SEARCH_CONSOLE,
    "search_console": AnalyticsSource.SEARCH_CONSOLE,
    "first_party": AnalyticsSource.FIRST_PARTY,
}
_API_PROVIDERS: Final = {
    AnalyticsSource.GA4: "ga4",
    AnalyticsSource.SEARCH_CONSOLE: "gsc",
}
_MAX_SEED_CHARS: Final = 64


def _iso_date(value: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError("must be a date as YYYY-MM-DD") from None


def _existing_file(value: str) -> str:
    """A readable file, checked by argparse: an obvious typo never opens a connection."""
    if not Path(value).is_file():
        raise argparse.ArgumentTypeError("must be a readable file")
    return value


def _seed(value: str) -> str:
    text = value.strip()
    if not text or len(text) > _MAX_SEED_CHARS or not text.isprintable():
        raise argparse.ArgumentTypeError(f"must be 1-{_MAX_SEED_CHARS} printable characters")
    return text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_services.analytics.cli",
        description="Analytics import, aggregation and demo seeding (ADR-0015).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    importer = commands.add_parser("import", help="Import daily rows for one source/dataset.")
    importer.add_argument("--source", required=True, choices=sorted(SOURCE_ALIASES))
    importer.add_argument("--dataset", required=True, choices=[item.value for item in Dataset])
    importer.add_argument("--start", required=True, type=_iso_date)
    importer.add_argument("--end", required=True, type=_iso_date)
    importer.add_argument(
        "--file",
        default=None,
        type=_existing_file,
        help="A canonical CSV (ken_link_daily / ken_page_daily).",
    )
    importer.add_argument(
        "--replace-via",
        dest="replace_via",
        action="store_true",
        help="Allow replacing rows that another transport wrote (recorded on the ledger).",
    )

    aggregate = commands.add_parser(
        "aggregate", help="First-party link events -> daily rows, then the measure sweep."
    )
    aggregate.add_argument("--start", default=None, type=_iso_date)
    aggregate.add_argument("--end", default=None, type=_iso_date)

    demo = commands.add_parser("demo-seed", help="Seeded demo analytics (local/ci only).")
    demo.add_argument("--seed", default=DEFAULT_SEED, type=_seed)
    return parser


def outcome_line(command: str, outcomes: Sequence[ImportOutcome]) -> str:
    """One JSON line describing what ran. No URL, no path, no credential."""
    return json.dumps(
        {
            "command": command,
            "runs": [
                {
                    "job_run_id": str(outcome.job_run_id),
                    "import_id": str(outcome.import_id),
                    "plans_measured": outcome.plans_measured,
                    "counters": dict(sorted(outcome.counters.items())),
                }
                for outcome in outcomes
            ],
        },
        sort_keys=True,
    )


async def _build_provider(
    settings: Settings, *, name: str, file: str | None
) -> tuple[AnalyticsBatchProvider, AsyncEngine]:
    """The provider plus the engine it (and the run) will use."""
    engine = create_engine(settings)  # no I/O until first use
    readonly = create_readonly_sessionmaker(engine)
    async with readonly() as session:
        resolver = await InventoryUrlResolver.build(session)
    provider = build_analytics_provider(settings, name=name, resolver=resolver, file=file)
    return provider, engine


async def _import(settings: Settings, policy: AnalyticsPolicy, args: argparse.Namespace) -> int:
    source = SOURCE_ALIASES[args.source]
    dataset = Dataset(args.dataset)
    if args.file is not None:
        provider_name, via = "csv", ImportVia.CSV
    elif source in _API_PROVIDERS:
        provider_name, via = _API_PROVIDERS[source], ImportVia.API
    else:
        print(
            "configuration error: first-party rows come from 'aggregate', not 'import' "
            "(or pass --file with a ken_link_daily CSV)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        date_range = DateRange(start=args.start, end=args.end)
    except ValidationError:
        print("configuration error: invalid date range", file=sys.stderr)
        return EXIT_USAGE
    try:
        provider, engine = await _build_provider(settings, name=provider_name, file=args.file)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:
        print(f"the provider could not be built ({type(exc).__name__})", file=sys.stderr)
        return EXIT_USAGE
    request = ImportRequest(
        source=source,
        dataset=dataset,
        via=via,
        date_range=date_range,
        source_timezone=source_timezone(source, settings),
        replace_via=bool(args.replace_via),
        triggered_by="cli",
    )
    try:
        outcome = await run_import(
            sessionmaker=create_sessionmaker(engine),
            settings=settings,
            policy=policy,
            provider=provider,
            request=request,
        )
    except AnalyticsImportError as exc:
        print(json.dumps({"command": "import", "error": exc.code.value}), file=sys.stderr)
        return EXIT_RUN_ERROR
    except JobAlreadyRunningError:
        print("another analytics run is in progress", file=sys.stderr)
        return EXIT_RUN_ERROR
    except Exception as exc:
        print(f"the import could not be completed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_RUN_ERROR
    finally:
        await _close(provider, engine)
    print(outcome_line("import", [outcome]))
    return EXIT_OK


async def _aggregate(settings: Settings, policy: AnalyticsPolicy, args: argparse.Namespace) -> int:
    if (args.start is None) != (args.end is None):
        print("configuration error: give both --start and --end, or neither", file=sys.stderr)
        return EXIT_USAGE
    window = None
    if args.start is not None and args.end is not None:
        try:
            window = DateRange(start=args.start, end=args.end)
        except ValidationError:
            print("configuration error: invalid date range", file=sys.stderr)
            return EXIT_USAGE
    engine = create_engine(settings)
    try:
        outcome = await run_aggregation(
            sessionmaker=create_sessionmaker(engine),
            settings=settings,
            policy=policy,
            date_range=window,
        )
    except AnalyticsImportError as exc:
        print(json.dumps({"command": "aggregate", "error": exc.code.value}), file=sys.stderr)
        return EXIT_RUN_ERROR
    except JobAlreadyRunningError:
        print("another analytics run is in progress", file=sys.stderr)
        return EXIT_RUN_ERROR
    except Exception as exc:
        print(f"the aggregation could not be completed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_RUN_ERROR
    finally:
        await engine.dispose()
    print(outcome_line("aggregate", [outcome]))
    return EXIT_OK


async def _demo_seed(settings: Settings, policy: AnalyticsPolicy, args: argparse.Namespace) -> int:
    engine = create_engine(settings)
    try:
        outcomes = await run_demo_seed(
            sessionmaker=create_sessionmaker(engine),
            settings=settings,
            policy=policy,
            seed=args.seed,
        )
    except AnalyticsImportError as exc:
        print(json.dumps({"command": "demo-seed", "error": exc.code.value}), file=sys.stderr)
        return EXIT_RUN_ERROR
    except JobAlreadyRunningError:
        print("another analytics run is in progress", file=sys.stderr)
        return EXIT_RUN_ERROR
    except Exception as exc:
        print(f"the demo seed could not be completed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_RUN_ERROR
    finally:
        await engine.dispose()
    print(outcome_line("demo-seed", outcomes))
    return EXIT_OK


async def _close(provider: AnalyticsBatchProvider, engine: AsyncEngine) -> None:
    try:
        await provider.aclose()
    except Exception as exc:
        print(f"provider close failed ({type(exc).__name__})", file=sys.stderr)
    await engine.dispose()


async def _run(settings: Settings, policy: AnalyticsPolicy, args: argparse.Namespace) -> int:
    match args.command:
        case "import":
            return await _import(settings, policy, args)
        case "aggregate":
            return await _aggregate(settings, policy, args)
        case "demo-seed":
            return await _demo_seed(settings, policy, args)
        case _:  # pragma: no cover - argparse requires a known command
            return EXIT_USAGE


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
        policy = load_configured_analytics_policy(settings)
    except (PolicyError, FileNotFoundError, OSError):
        print("configuration error: the analytics policy could not be loaded", file=sys.stderr)
        return EXIT_USAGE
    return asyncio.run(_run(settings, policy, args))


if __name__ == "__main__":
    raise SystemExit(main())
