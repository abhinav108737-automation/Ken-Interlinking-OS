"""A compression bomb must be bounded while it is READ, not after it is inflated (QA finding).

The crawler sends ``Accept-Encoding: identity``, so it never asks for compression. A hostile
server may answer ``Content-Encoding: gzip`` anyway; before the fix httpx inflated each raw
chunk whole and a 1 MiB body peaked at hundreds of MB per request (x concurrency 8).
"""

from __future__ import annotations

import gc
import tracemalloc
import zlib
from datetime import UTC, datetime

from ken_contracts.inventory import CrawlConfig, IssueCode, SourceDocument, SourceIssue
from ken_services.inventory.sources.public_web import PublicWebContentSource
from tests.support.local_site import HTML, LocalSite, RouteSpec, html_page

UA = "KenInterlinkBot/0.1 (+https://example.invalid/bot)"
MAX_BYTES = 1_048_576
INFLATED = 1 << 30  # 1 GiB
PEAK_BUDGET = 24 * 1024 * 1024  # generous: the unfixed code peaks near 260 MB


def gzip_bomb(inflated: int = INFLATED) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, 31)  # wbits 31 -> gzip container
    zeros = bytes(1 << 20)
    out = bytearray()
    for _ in range(inflated >> 20):
        out += compressor.compress(zeros)
    out += compressor.flush()
    return bytes(out)


def config_for(site: LocalSite) -> CrawlConfig:
    return CrawlConfig(
        base_url=site.base_url,
        user_agent=UA,
        delay_s=0.0,
        concurrency=1,
        max_pages=5,
        max_depth=2,
        timeout_s=30.0,
        max_bytes=MAX_BYTES,
        max_redirects=3,
    )


async def crawl(site: LocalSite) -> list[SourceDocument | SourceIssue]:
    source = PublicWebContentSource(
        config_for(site), clock=lambda: datetime(2026, 10, 18, tzinfo=UTC)
    )
    try:
        await source.site_scope()
        return [r async for r in source.iter_records()]
    finally:
        await source.aclose()


async def test_gzip_bomb_is_too_large_and_memory_stays_bounded() -> None:
    bomb = gzip_bomb()
    assert len(bomb) <= MAX_BYTES  # the wire body itself is under the cap
    routes: dict[str, RouteSpec] = {
        "/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n"),
        "/": (200, {**HTML, "Content-Encoding": "gzip"}, bomb),
    }
    with LocalSite(routes) as site:
        gc.collect()
        tracemalloc.start()
        try:
            baseline, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            records = await crawl(site)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert site.requests[-1].headers["accept-encoding"] == "identity"
    issues = [r for r in records if isinstance(r, SourceIssue)]
    assert [i.code for i in issues] == [IssueCode.TOO_LARGE]
    assert not [r for r in records if isinstance(r, SourceDocument)]
    growth = peak - baseline
    print(f"decompression-bomb peak growth: {growth / 1e6:.1f} MB")
    assert growth < PEAK_BUDGET, f"peak grew {growth / 1e6:.1f} MB"


async def test_honest_identity_response_is_unaffected() -> None:
    routes: dict[str, RouteSpec] = {
        "/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n"),
        "/": (200, {**HTML, "Content-Encoding": "identity"}, html_page("/a")),
        "/a": (200, HTML, html_page()),
    }
    with LocalSite(routes) as site:
        records = await crawl(site)
    documents = [r for r in records if isinstance(r, SourceDocument)]
    assert [d.final_url for d in documents] == [site.url("/"), site.url("/a")]
    assert not [r for r in records if isinstance(r, SourceIssue)]
