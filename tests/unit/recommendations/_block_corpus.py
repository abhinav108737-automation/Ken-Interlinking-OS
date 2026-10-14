"""Every stored ``body_html`` the repository's fixture corpora produce, for block-walk parity.

Three corpora, all normalised by the Phase 1 normaliser (no DB, no network):

* the Phase 1 fixture SITE (``tests/fixtures/inventory/site``), every 200 HTML route;
* the Phase 1 EXPORTS (``tests/fixtures/inventory/exports/*.jsonl``), every valid record;
* the Phase 2 DEMO corpus (``tests/fixtures/demo/research_site.jsonl``).

:func:`block_digest` hashes ``(block_id, kind, text)`` of every block of every page. The
digest pinned in ``test_text_runs.py`` was computed with the normaliser BEFORE text runs
were added, so equality proves the runs extension changed no block id, kind or text.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Final

from ken_contracts.inventory import NormalizedPage, SourceDocument, SourceType
from ken_services.inventory.normalize import extract_blocks, normalize_document
from ken_services.inventory.sources.file_export import FileExportContentSource
from ken_services.inventory.urls import build_scope
from tests.support.fixture_site import EXPORTS_DIR, FixtureSite
from tests.unit.intelligence_evals.demo_corpus import load_pages as load_demo_pages

FIXED_TIME: Final = datetime(2026, 10, 14, tzinfo=UTC)


def _site_pages() -> list[tuple[str, NormalizedPage]]:
    site = FixtureSite()
    scope = build_scope(site.origin)
    out: list[tuple[str, NormalizedPage]] = []
    for route in site.html_pages():
        url = site.url(route.path)
        doc = SourceDocument(
            source_type=SourceType.PUBLIC_WEB,
            source_id=url,
            requested_url=url,
            final_url=url,
            http_status=200,
            content_type=route.content_type,
            content_language=route.header("content-language"),
            fetched_at=FIXED_TIME,
            depth=0,
            html=route.text(),
        )
        out.append((f"site:{route.path}", normalize_document(doc, scope=scope)))
    return out


async def _export_pages(path: Path) -> list[tuple[str, NormalizedPage]]:
    source = FileExportContentSource(path, max_bytes=20_000_000, clock=lambda: FIXED_TIME)
    out: list[tuple[str, NormalizedPage]] = []
    try:
        scope = await source.site_scope()
        async for record in source.iter_records():
            if isinstance(record, SourceDocument):
                page = normalize_document(record, scope=scope)
                out.append((f"export:{path.name}:{page.url}", page))
    except ValueError:  # a deliberately broken header file
        return out
    finally:
        await source.aclose()
    return out


@cache
def corpus() -> tuple[tuple[str, NormalizedPage], ...]:
    """``(label, page)`` for every page of the three corpora, in a fixed order."""
    pages = _site_pages()
    for path in sorted(EXPORTS_DIR.glob("*.jsonl")):
        pages.extend(asyncio.run(_export_pages(path)))
    pages.extend((f"demo:{page.url}", page) for page in load_demo_pages())
    return tuple(pages)


def block_digest(pages: Iterable[tuple[str, NormalizedPage]]) -> tuple[int, int, str]:
    """``(pages, blocks, sha256)`` over every page's ``(label, block_id, kind, text)``."""
    digest = hashlib.sha256()
    n_pages = n_blocks = 0
    for label, page in pages:
        n_pages += 1
        for block in extract_blocks(page.body_html):
            n_blocks += 1
            for part in (label, block.block_id, block.kind, block.text):
                digest.update(part.encode("utf-8"))
                digest.update(b"\x00")
        digest.update(b"\x01")
    return n_pages, n_blocks, digest.hexdigest()
