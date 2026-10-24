"""Simulated CMS operator commands: ``python -m ken_services.deployment.cms_sim <command>``.

ADR-0014 s4 / s8 (``make cms-sim-seed``, ``make cms-sim-serve``)::

    seed [--export FILE.jsonl] [--reset]   create ken_cms_sim if missing and seed it
    serve                                  serve its pages on 127.0.0.1:<free port>

**Seed.** The simulated CMS lives in its OWN disposable, project-local database
(``KEN_CMS_SIM_DATABASE_URL``, default ``ken_cms_sim`` on 127.0.0.1:5433; anything else is
refused by :func:`~ken_core.providers.cms.simulated.assert_safe_sim_url`). Every CURRENT
inventory page (``pages`` at ``current_version``, not gone) becomes one CMS page:

* ``page_id`` -- ``pages.external_id`` when the current version is CMS-sourced
  (``source_type == 'cms'``) and the id is set, else
  :func:`~ken_core.providers.cms.simulated.stable_page_id` of the canonical URL. This is the
  SAME resolution as ``DatabaseDeploymentService._cms_page_id`` (external id for CMS-sourced
  pages, else the provider's ``resolve_page_id`` by url/canonical_url), because the CMS page's
  ``url`` and ``canonical_url`` are both the inventory's canonical URL;
* ``body_html`` -- with ``--export``, the ORIGINAL export record's ``body_html`` when the
  record's URL normalises to the page's fetched or canonical URL (docs/CONTRACTS.md s13, read
  through the real ``FileExportContentSource``); otherwise the stored ``page_versions.body_html``
  (the normalised content root, a fragment);
* ``title`` -- the version's title.

Existing CMS pages are KEPT (so deployed links survive a re-seed); ``--reset`` empties the
simulated CMS first (pages, operations, snapshots). A page whose url or path is already taken
by a different CMS page is skipped and counted. Prints counts only, as one JSON line.

**Serve.** :func:`ken_services.deployment.sim_site.serve_simulated_site` semantics: loopback,
port 0, never 8765, listener verified before the URL is printed. Runs until interrupted.

Exit codes: 0 done; 1 the command could not complete (class name only); 2 usage or
configuration error. Never prints a database URL, credentials or exception text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.cms import CMSPage
from ken_contracts.inventory import SourceDocument
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, Settings, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.models.inventory import Page, PageVersion
from ken_core.db.session import create_sessionmaker
from ken_core.providers.cms.factory import build_html_engine
from ken_core.providers.cms.simulated import (
    SimulatedCMSProvider,
    assert_safe_sim_url,
    ensure_database,
    stable_page_id,
)
from ken_services.deployment.sim_site import SimSiteError, SimulatedSite
from ken_services.inventory.sources.file_export import ExportFormatError, FileExportContentSource

__all__ = [
    "InventoryPage",
    "SeedSummary",
    "build_sim_pages",
    "inventory_pages",
    "load_export_bodies",
    "main",
    "seed_simulated_cms",
]

EXIT_OK: Final = 0
EXIT_RUN_ERRORS: Final = 1
EXIT_USAGE: Final = 2

CMS_SOURCE_TYPE: Final = "cms"


class UsageError(Exception):
    """A user-facing argument or configuration problem. The message is safe to print."""


@dataclass(frozen=True, slots=True)
class InventoryPage:
    """One current inventory page, as read from the application database."""

    page_id: uuid.UUID
    canonical_url: str
    external_id: str | None
    source_type: str
    url: str
    """The current version's fetched (final) URL."""
    title: str | None
    body_html: str
    published_at: datetime | None


@dataclass(slots=True)
class SeedSummary:
    database_created: bool = False
    reset: bool = False
    inventory_pages: int = 0
    seeded: int = 0
    already_present: int = 0
    from_export: int = 0
    skipped_conflict: int = 0


@dataclass(frozen=True, slots=True)
class SeedPlan:
    pages: tuple[CMSPage, ...]
    from_export: int
    skipped_conflict: int


def cms_page_id_for(page: InventoryPage) -> str:
    """The CMS page id of ``page`` (the deployment service's resolution, module docstring)."""
    if page.external_id and page.source_type == CMS_SOURCE_TYPE:
        return page.external_id
    return stable_page_id(page.canonical_url)


def _sim_path(url: str) -> str:
    """The path the simulated provider stores for ``url`` (its own ``path`` mapping)."""
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def build_sim_pages(
    inventory: Iterable[InventoryPage],
    *,
    export_bodies: Mapping[str, str] | None = None,
    existing: Iterable[CMSPage] = (),
) -> SeedPlan:
    """The CMS pages to upsert (pure). ``existing`` pages keep their url and path: a new page
    colliding with a DIFFERENT existing (or earlier) page on url or path is skipped."""
    bodies = export_bodies or {}
    url_owner: dict[str, str] = {}
    path_owner: dict[str, str] = {}
    for page in existing:
        url_owner[page.url] = page.page_id
        path_owner[_sim_path(page.url)] = page.page_id
    pages: list[CMSPage] = []
    planned: set[str] = set()
    from_export = skipped = 0
    for item in sorted(inventory, key=lambda p: p.canonical_url):
        page_id = cms_page_id_for(item)
        url = item.canonical_url
        path = _sim_path(url)
        if url_owner.get(url, page_id) != page_id or path_owner.get(path, page_id) != page_id:
            skipped += 1
            continue
        if page_id in planned:
            skipped += 1  # two inventory pages share one CMS external id
            continue
        planned.add(page_id)
        url_owner[url] = page_id
        path_owner[path] = page_id
        body = bodies.get(item.url) or bodies.get(item.canonical_url)
        if body is not None:
            from_export += 1
        pages.append(
            CMSPage(
                page_id=page_id,
                url=url,
                canonical_url=url,
                title=item.title or "",
                body_html=body if body is not None else item.body_html,
                version=f"sim-seed-{uuid.uuid4().hex}",
                published_at=item.published_at,
                metadata={"ken_page_id": str(item.page_id)},
            )
        )
    return SeedPlan(pages=tuple(pages), from_export=from_export, skipped_conflict=skipped)


async def inventory_pages(sessionmaker: async_sessionmaker[AsyncSession]) -> list[InventoryPage]:
    """Every current, not-gone inventory page with its current version."""
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(
                    Page.id,
                    Page.canonical_url,
                    Page.external_id,
                    PageVersion.source_type,
                    PageVersion.url,
                    PageVersion.title,
                    PageVersion.body_html,
                    PageVersion.published_at,
                )
                .join(
                    PageVersion,
                    (PageVersion.page_id == Page.id)
                    & (PageVersion.version == Page.current_version),
                )
                .where(Page.gone_at.is_(None))
                .order_by(Page.canonical_url)
            )
        ).all()
    return [
        InventoryPage(
            page_id=row.id,
            canonical_url=row.canonical_url,
            external_id=row.external_id,
            source_type=row.source_type,
            url=row.url,
            title=row.title,
            body_html=row.body_html,
            published_at=row.published_at,
        )
        for row in rows
    ]


async def load_export_bodies(path: Path, *, max_bytes: int) -> dict[str, str]:
    """Normalised record URL -> the record's ORIGINAL ``body_html`` (invalid, out-of-scope
    and duplicate records are ignored, exactly as an import would)."""
    source = FileExportContentSource(path, max_bytes=max_bytes)
    bodies: dict[str, str] = {}
    try:
        await source.site_scope()
        async for record in source.iter_records():
            if isinstance(record, SourceDocument):
                bodies.setdefault(record.final_url, record.html)
    except ExportFormatError as exc:
        raise UsageError(f"--export: {exc}") from None  # the message is safe by contract
    finally:
        await source.aclose()
    return bodies


async def seed_simulated_cms(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: SimulatedCMSProvider,
    export_bodies: Mapping[str, str] | None = None,
    reset: bool = False,
) -> SeedSummary:
    """Seed ``provider`` from the application inventory (module docstring). The provider's
    database must exist (:func:`ensure_database`); its schema is ensured here."""
    await provider.ensure_schema()
    if reset:
        await provider.reset()
    inventory = await inventory_pages(sessionmaker)
    existing = [] if reset else await provider.list_pages()
    plan = build_sim_pages(inventory, export_bodies=export_bodies, existing=existing)
    written = await provider.upsert_pages(plan.pages, replace=False)
    return SeedSummary(
        reset=reset,
        inventory_pages=len(inventory),
        seeded=written,
        already_present=len(plan.pages) - written,
        from_export=plan.from_export,
        skipped_conflict=plan.skipped_conflict,
    )


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_services.deployment.cms_sim",
        description="The local simulated CMS (ADR-0014): seed it, serve it.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed", help="Create ken_cms_sim if missing; seed it.")
    seed.add_argument(
        "--export",
        type=Path,
        default=None,
        metavar="FILE",
        help="JSONL export whose ORIGINAL body_html is used for matching pages.",
    )
    seed.add_argument(
        "--reset", action="store_true", help="Empty the simulated CMS before seeding."
    )
    commands.add_parser("serve", help="Serve the simulated pages on 127.0.0.1:<free port>.")
    return parser


def seed_line(summary: SeedSummary) -> str:
    return json.dumps({"command": "cms-sim-seed", **asdict(summary)}, sort_keys=True)


def serve_line(base_url: str, pages: int) -> str:
    return json.dumps(
        {"command": "cms-sim-serve", "base_url": base_url, "pages": pages, "verified": True},
        sort_keys=True,
    )


async def _seed(settings: Settings, args: argparse.Namespace) -> int:
    url = settings.cms_sim_database_url
    try:
        assert_safe_sim_url(url)
        bodies = (
            await load_export_bodies(args.export, max_bytes=settings.ingest_max_file_bytes)
            if args.export is not None
            else None
        )
    except (ConfigurationError, UsageError) as exc:
        print(f"error: {exc}", file=sys.stderr)  # fixed, URL-free messages
        return EXIT_USAGE
    engine = create_engine(settings)  # no I/O until first use
    provider: SimulatedCMSProvider | None = None
    try:
        created = await ensure_database(url)
        provider = SimulatedCMSProvider(
            database_url=url,
            html_engine=build_html_engine(),
            connect_timeout_s=settings.database_connect_timeout_s,
        )
        summary = await seed_simulated_cms(
            sessionmaker=create_sessionmaker(engine),
            provider=provider,
            export_bodies=bodies,
            reset=args.reset,
        )
        summary.database_created = created
    except Exception as exc:
        print(f"seeding could not be completed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_RUN_ERRORS
    finally:
        if provider is not None:
            await provider.aclose()
        await engine.dispose()
    print(seed_line(summary))
    return EXIT_OK


async def _serve(settings: Settings) -> int:
    try:
        assert_safe_sim_url(settings.cms_sim_database_url)
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    provider = SimulatedCMSProvider(
        database_url=settings.cms_sim_database_url,
        html_engine=None,  # read-only: the site never mutates
        connect_timeout_s=settings.database_connect_timeout_s,
    )
    site = SimulatedSite(provider)
    try:
        try:
            pages = len(await provider.list_pages())
        except Exception as exc:
            print(
                f"the simulated CMS is not readable ({type(exc).__name__}); "
                "run make cms-sim-seed first",
                file=sys.stderr,
            )
            return EXIT_RUN_ERRORS
        try:
            base_url = await site.start()
        except SimSiteError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_RUN_ERRORS
        print(serve_line(base_url, pages), flush=True)
        print(
            "serving until interrupted (Ctrl-C); e.g. "
            f"KEN_DEPLOY_VERIFY_BASE_URL={base_url} KEN_CRAWL_ALLOW_PRIVATE_NETWORKS=true "
            "(loopback needs the local/ci-only SSRF escape hatch)",
            file=sys.stderr,
            flush=True,
        )
        await site.wait()
        return EXIT_OK
    finally:
        await site.stop()
        await provider.aclose()


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
    if args.command == "seed":
        return asyncio.run(_seed(settings, args))
    try:
        return asyncio.run(_serve(settings))
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
