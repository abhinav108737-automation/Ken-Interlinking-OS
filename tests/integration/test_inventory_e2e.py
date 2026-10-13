"""End to end: real sources -> real normaliser -> PostgreSQL -> reader (TRD.md section 22).

Every other ingestion test uses scripted sources and a fake normaliser so each
rule can be pinned in isolation. These tests wire the real pieces together over
the spec-derived fixture corpus (tests/fixtures/inventory), offline: the crawl
is served in-process by ``httpx.MockTransport`` -- no socket, no real site.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.events import EventName
from ken_contracts.inventory import (
    EMITTED_BY_INGEST,
    CrawlConfig,
    IngestionRunDetail,
    LinkResolution,
    LinkType,
    RunStatus,
    SourceType,
)
from ken_core.db.models import Event, KnownUrl, PageVersion
from ken_services.inventory.ingest import run_ingestion
from ken_services.inventory.reader import SqlInventoryReader
from ken_services.inventory.sources.file_export import FileExportContentSource
from ken_services.inventory.sources.public_web import PublicWebContentSource
from tests.support.fixture_site import EXPECTED_DIR, EXPORTS_DIR, FixtureSite

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SM = async_sessionmaker[AsyncSession]
CRAWL_GOLDEN: dict[str, Any] = json.loads((EXPECTED_DIR / "crawl.json").read_text("utf-8"))
EXPORT_GOLDEN: dict[str, Any] = json.loads((EXPECTED_DIR / "exports.json").read_text("utf-8"))


async def _no_sleep(_: float) -> None:
    return None


async def _crawl(sessionmaker: SM, site: FixtureSite) -> IngestionRunDetail:
    config = CrawlConfig(base_url=site.url("/"), **CRAWL_GOLDEN["config"])
    source = PublicWebContentSource(config, transport=site.transport(), sleep=_no_sleep)
    return await run_ingestion(source=source, sessionmaker=sessionmaker)


async def _import(sessionmaker: SM, name: str) -> IngestionRunDetail:
    source = FileExportContentSource(EXPORTS_DIR / name, max_bytes=10_000_000)
    return await run_ingestion(source=source, sessionmaker=sessionmaker)


def _invariant(run: IngestionRunDetail) -> None:
    c = run.counters
    assert c.pages_fetched == (
        c.pages_created + c.pages_changed + c.pages_unchanged + c.pages_duplicate + c.pages_failed
    )


async def _count(sessionmaker: SM, statement: Any) -> int:
    async with sessionmaker() as session:
        return int((await session.scalar(statement)) or 0)


async def _all_pages(reader: SqlInventoryReader) -> dict[str, Any]:
    result = await reader.list_pages(limit=200, offset=0)
    return {page.canonical_url: page for page in result.items}


# --------------------------------------------------------------------------- #
# Public web crawl
# --------------------------------------------------------------------------- #
async def test_crawl_of_the_fixture_site_is_persisted_and_inspectable(sessionmaker: SM) -> None:
    site = FixtureSite()
    run = await _crawl(sessionmaker, site)
    _invariant(run)
    assert run.source_type is SourceType.PUBLIC_WEB
    assert run.status in {RunStatus.SUCCEEDED, RunStatus.COMPLETED_WITH_ERRORS}
    assert run.counters.pages_created > 10
    assert run.counters.pages_failed == 0
    assert run.config_snapshot["base_url"] == site.url("/")

    reader = SqlInventoryReader(sessionmaker)
    pages = await _all_pages(reader)
    # The alias declares /reports/x as canonical: it is folded into that page, never its own.
    assert site.url("/reports/x") in pages
    assert site.url("/reports/alias-x") not in pages
    # utm_* spellings normalise to the same page.
    assert not any("utm_" in canonical for canonical in pages)
    # Off-site and robots-disallowed URLs never become pages.
    assert not any(canonical.startswith(site.url("/private/")) for canonical in pages)
    assert all(canonical.startswith(site.origin) for canonical in pages)

    # The redirect chain /old -> /older-move -> /reports/moved is recorded as aliases.
    async with sessionmaker() as session:
        old = await session.get(KnownUrl, site.url("/old"))
        moved = pages[site.url("/reports/moved")]
        assert old is not None
        assert (old.outcome, old.page_id) == ("redirect", moved.id)

    # Read-time link resolution over real data.
    broken = await reader.list_links(
        limit=200, offset=0, link_type=LinkType.INTERNAL, resolution=LinkResolution.BROKEN
    )
    broken_targets = {link.resolved_url for link in broken.items}
    assert site.url("/missing-page") in broken_targets
    assert site.url("/gone-page") in broken_targets
    external = await reader.list_links(limit=200, offset=0, link_type=LinkType.EXTERNAL)
    assert external.items
    assert all(link.resolution is LinkResolution.NOT_APPLICABLE for link in external.items)
    inbound = await reader.list_links(
        limit=200, offset=0, target_page_id=pages[site.url("/reports/x")].id
    )
    assert inbound.page.total >= 1
    assert all(link.resolution is LinkResolution.RESOLVED for link in inbound.items)

    # Events: one page.created per created page, one crawl.completed, all from ken-ingest.
    created = await _count(
        sessionmaker,
        select(func.count()).where(Event.event_name == EventName.PAGE_CREATED.value),
    )
    assert created == run.counters.pages_created
    completed = await _count(
        sessionmaker,
        select(func.count()).where(Event.event_name == EventName.CRAWL_COMPLETED.value),
    )
    assert completed == 1
    emitters = await _count(
        sessionmaker, select(func.count()).where(Event.emitted_by != EMITTED_BY_INGEST)
    )
    assert emitters == 0

    detail = await reader.get_run(run_id=run.id)
    assert detail.issue_counts.get("robots_disallowed", 0) >= 1
    assert detail.issue_counts.get("redirect_out_of_scope", 0) >= 1


async def test_recrawling_an_unchanged_site_writes_no_new_versions(sessionmaker: SM) -> None:
    first = await _crawl(sessionmaker, FixtureSite())
    versions = await _count(sessionmaker, select(func.count()).select_from(PageVersion))
    second = await _crawl(sessionmaker, FixtureSite())
    _invariant(second)
    assert (second.counters.pages_created, second.counters.pages_changed) == (0, 0)
    assert second.counters.pages_unchanged == first.counters.pages_created
    assert await _count(sessionmaker, select(func.count()).select_from(PageVersion)) == versions
    page_events = await _count(
        sessionmaker,
        select(func.count()).where(
            Event.event_name.in_([EventName.PAGE_CREATED.value, EventName.PAGE_UPDATED.value])
        ),
    )
    assert page_events == first.counters.pages_created


# --------------------------------------------------------------------------- #
# File export
# --------------------------------------------------------------------------- #
async def test_export_then_changed_export_versions_exactly_the_content_changes(
    sessionmaker: SM,
) -> None:
    first = await _import(sessionmaker, "valid.jsonl")
    _invariant(first)
    assert first.source_type is SourceType.FILE_EXPORT
    assert first.status is RunStatus.SUCCEEDED

    second = await _import(sessionmaker, "v2_changed.jsonl")
    _invariant(second)
    golden = EXPORT_GOLDEN["v2_changed"]
    pages = await _all_pages(SqlInventoryReader(sessionmaker))
    # Records that declare another canonical are aliases (ADR-0009 section 9): they
    # never get a page or a version of their own, so only authoritative records count.
    changed = [canonical for canonical in golden["hash_changes"] if canonical in pages]
    unchanged = [canonical for canonical in golden["hash_unchanged"] if canonical in pages]
    assert changed and unchanged
    assert site_alias_is_folded(pages)
    for canonical in changed:
        assert pages[canonical].current_version == 2, canonical
    for canonical in unchanged:
        assert pages[canonical].current_version == 1, canonical
    assert second.counters.pages_changed == len(changed)
    assert second.counters.pages_unchanged == len(unchanged)

    updated = await _count(
        sessionmaker,
        select(func.count()).where(Event.event_name == EventName.PAGE_UPDATED.value),
    )
    assert updated == len(changed)


def site_alias_is_folded(pages: dict[str, Any]) -> bool:
    """sea-ev and the print view both assert india-ev as canonical: no pages of their own."""
    origin = "https://export.fixture.test"
    return (
        f"{origin}/reports/india-ev" in pages
        and f"{origin}/reports/sea-ev" not in pages
        and f"{origin}/reports/india-ev-print" not in pages
    )


async def test_an_export_with_bad_lines_completes_with_errors(sessionmaker: SM) -> None:
    run = await _import(sessionmaker, "with_errors.jsonl")
    _invariant(run)
    assert run.status is RunStatus.COMPLETED_WITH_ERRORS
    assert run.counters.error_count >= 1
    assert run.counters.pages_created >= 1  # good lines still land


async def test_an_export_with_a_bad_header_fails_safely(sessionmaker: SM) -> None:
    run = await _import(sessionmaker, "bad_header.jsonl")
    assert run.status is RunStatus.FAILED
    assert run.failure_reason
    assert run.counters.pages_fetched == 0
