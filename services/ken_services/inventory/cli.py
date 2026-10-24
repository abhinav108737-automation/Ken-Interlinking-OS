"""Inventory command line: ``python -m ken_services.inventory.cli <command>``.

Runs start here, never over HTTP (ADR-0009 section 7)::

    import-file PATH                 import a JSONL export (docs/CONTRACTS.md)
    crawl                            crawl the ONE site in KEN_CRAWL_BASE_URL
    import-cms-mock --site-url URL   read the mock CMS through CMSContentSource

Prints one JSON line (run id, status, counters) on success. Exit codes:

    0  run succeeded
    1  run completed_with_errors, failed, or could not be recorded
    2  usage or configuration error (nothing was run)
    3  another ingestion run is in progress (nothing was run)

Never prints the database URL or exception text: errors name a class only.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from ken_contracts.inventory import ContentSourceProvider, IngestionRunDetail, RunStatus
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.session import create_sessionmaker
from ken_services.inventory.ingest import IngestionAlreadyRunningError, run_ingestion

EXIT_OK: Final = 0
EXIT_RUN_ERRORS: Final = 1
EXIT_USAGE: Final = 2
EXIT_ALREADY_RUNNING: Final = 3

_SOURCES: Final = "ken_services.inventory.sources"


class UsageError(Exception):
    """A user-facing configuration or argument problem. The message is safe to print."""


# --------------------------------------------------------------------------- #
# Source construction (modules imported lazily: each is its own workstream)
# --------------------------------------------------------------------------- #
def _module(name: str) -> Any:
    return importlib.import_module(f"{_SOURCES}.{name}")


def build_file_source(path: Path, settings: Settings) -> ContentSourceProvider:
    if not path.is_file():
        raise UsageError("import-file: the export file does not exist or is not a file")
    source: ContentSourceProvider = _module("file_export").FileExportContentSource(
        path, max_bytes=settings.ingest_max_file_bytes
    )
    return source


def build_crawl_source(settings: Settings) -> ContentSourceProvider:
    try:
        config = settings.crawl_config()
    except ConfigurationError as exc:
        raise UsageError(str(exc)) from exc  # fixed, secret-free text from Settings
    source: ContentSourceProvider = _module("public_web").PublicWebContentSource(
        config, allow_private_networks=settings.crawl_allow_private_networks
    )
    return source


def build_cms_mock_source(site_url: str) -> ContentSourceProvider:
    cms = _module("cms")
    source: ContentSourceProvider = cms.CMSContentSource(
        cms.MockCMSInventoryReader(), site_url=site_url
    )
    return source


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_services.inventory.cli",
        description="Run one content-inventory ingestion (read-only towards every source).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    file_cmd = commands.add_parser("import-file", help="Import a JSONL export file.")
    file_cmd.add_argument("path", type=Path, help="Path to the .jsonl export.")
    commands.add_parser("crawl", help="Crawl the site configured in KEN_CRAWL_BASE_URL.")
    cms_cmd = commands.add_parser("import-cms-mock", help="Import from the mock CMS reader.")
    cms_cmd.add_argument("--site-url", required=True, help="Base URL of the mock CMS site.")
    return parser


def _build_source(args: argparse.Namespace, settings: Settings) -> ContentSourceProvider:
    if args.command == "import-file":
        return build_file_source(args.path, settings)
    if args.command == "crawl":
        return build_crawl_source(settings)
    return build_cms_mock_source(args.site_url)


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
def summary_line(detail: IngestionRunDetail) -> str:
    return json.dumps(
        {
            "run_id": str(detail.id),
            "status": detail.status.value,
            "source_type": detail.source_type.value,
            "source_name": detail.source_name,
            "counters": detail.counters.model_dump(),
            "failure_reason": detail.failure_reason,
        },
        sort_keys=True,
    )


def exit_code_for(detail: IngestionRunDetail) -> int:
    return EXIT_OK if detail.status is RunStatus.SUCCEEDED else EXIT_RUN_ERRORS


async def _run(settings: Settings, source: ContentSourceProvider) -> int:
    engine = create_engine(settings)  # no I/O until first use
    try:
        detail = await run_ingestion(source=source, sessionmaker=create_sessionmaker(engine))
    except IngestionAlreadyRunningError:
        print("another ingestion run is in progress; nothing was run", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except Exception as exc:
        print(f"ingestion could not be completed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_RUN_ERRORS
    finally:
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
        # hide_input_in_errors keeps values out, but print nothing of it anyway.
        print("configuration error: invalid KEN_* settings", file=sys.stderr)
        return EXIT_USAGE
    ken_logging.configure_logging(level=settings.log_level, fmt=settings.log_format)
    try:
        source = _build_source(args, settings)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return asyncio.run(_run(settings, source))


if __name__ == "__main__":
    raise SystemExit(main())
