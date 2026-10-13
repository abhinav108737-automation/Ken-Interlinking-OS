"""End-to-end crawler tests against a local threaded HTTP server on 127.0.0.1.

Real sockets, real httpx transport, still fully offline: the server binds the
loopback interface on an ephemeral port and the crawl scope is that netloc only.
"""

from __future__ import annotations

import itertools
import socket
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from ken_contracts.inventory import (
    CrawlConfig,
    IssueCode,
    SourceDocument,
    SourceIssue,
    SourceSummary,
)
from ken_services.inventory.sources.public_web import PublicWebContentSource
from tests.support.local_site import HTML, LocalSite, RouteSpec, html_page

UA = "KenInterlinkBot/0.1 (+https://example.invalid/bot)"

type Record = SourceDocument | SourceIssue


def config_for(site: LocalSite, **overrides: Any) -> CrawlConfig:
    values: dict[str, Any] = {
        "base_url": site.base_url,
        "user_agent": UA,
        "delay_s": 0.0,
        "concurrency": 2,
        "max_pages": 50,
        "max_depth": 5,
        "timeout_s": 2.0,
        "max_bytes": 10_000,
        "max_redirects": 3,
    }
    values.update(overrides)
    return CrawlConfig(**values)


async def run(config: CrawlConfig) -> tuple[list[Record], SourceSummary]:
    source = PublicWebContentSource(config, clock=lambda: datetime(2026, 10, 13, tzinfo=UTC))
    try:
        await source.site_scope()
        records = [r async for r in source.iter_records()]
        return records, await source.summary()
    finally:
        await source.aclose()


def codes(records: list[Record]) -> list[IssueCode]:
    return [r.code for r in records if isinstance(r, SourceIssue)]


def closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


@pytest.fixture
def site() -> Iterator[LocalSite]:
    nofollow = '<meta name="robots" content="nofollow">'
    routes: dict[str, RouteSpec] = {
        "/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /private\n"),
        "/": (
            200,
            {**HTML, "Set-Cookie": "session=s3cr3t; Path=/"},
            html_page("/a", "/private/x", "/old", "https://external.test/", "/file.pdf", "/nf"),
        ),
        "/a": (200, HTML, html_page("/", "/b?x=1", "/b?x=1#frag")),
        "/b?x=1": (200, HTML, html_page("/hidden", extra=nofollow)),
        "/old": (301, {"Location": "/new"}, ""),
        "/new": (200, {**HTML, "Content-Language": "en"}, html_page("/a")),
        "/file.pdf": (200, {"Content-Type": "application/pdf"}, b"%PDF"),
        "/hidden": (200, HTML, html_page()),
    }
    with LocalSite(routes) as running:
        yield running


async def test_end_to_end_crawl(site: LocalSite) -> None:
    records, summary = await run(config_for(site))
    documents = [r for r in records if isinstance(r, SourceDocument)]
    assert [d.final_url for d in documents] == [
        site.url("/"),
        site.url("/a"),
        site.url("/new"),
        site.url("/b?x=1"),
    ]
    moved = documents[2]
    assert moved.requested_url == site.url("/old")
    assert moved.redirect_chain == (site.url("/old"),)
    assert moved.content_language == "en"
    assert codes(records) == [
        IssueCode.ROBOTS_DISALLOWED,
        IssueCode.NON_HTML,
        IssueCode.HTTP_NOT_FOUND,
    ]

    targets = site.targets()
    assert targets.count("/robots.txt") == 1
    assert "/private/x" not in targets
    assert "/hidden" not in targets  # nofollow page
    assert targets.count("/a") == 1
    assert targets.count("/b?x=1") == 1
    # discovered: / /a /private/x /old /new /file.pdf /nf /b?x=1 ; skipped: robots + non-HTML
    assert summary == SourceSummary(discovered=8, skipped=2)


async def test_requests_are_polite(site: LocalSite) -> None:
    await run(config_for(site))
    assert site.requests
    for request in site.requests:
        assert request.method == "GET"
        assert request.headers["user-agent"] == UA
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        assert request.headers["host"] == site.netloc


async def test_timeout_on_slow_route() -> None:
    routes: dict[str, RouteSpec] = {
        "/": (200, HTML, html_page("/slow", "/fast")),
        "/slow": (200, HTML, html_page(), 1.0),
        "/fast": (200, HTML, html_page()),
    }
    with LocalSite(routes) as site:
        started = time.monotonic()
        records, _ = await run(config_for(site, timeout_s=0.2))
        elapsed = time.monotonic() - started
    timeouts = [r for r in records if isinstance(r, SourceIssue) and r.code is IssueCode.TIMEOUT]
    assert [t.url for t in timeouts] == [site.url("/slow")]
    assert len([r for r in records if isinstance(r, SourceDocument)]) == 2
    assert elapsed < 1.0


async def test_network_errors() -> None:
    port = closed_port()
    routes: dict[str, RouteSpec] = {
        "/": (200, HTML, html_page("/drop", f"http://127.0.0.1:{port}/x", "/ok")),
        "/drop": (0, {}, ""),
        "/ok": (200, HTML, html_page()),
    }
    with LocalSite(routes) as site:
        records, _ = await run(config_for(site, allowed_hosts=[f"127.0.0.1:{port}"]))
    issues = [r for r in records if isinstance(r, SourceIssue)]
    by_code = {i.code: i for i in issues}
    assert by_code[IssueCode.NETWORK_ERROR].url == site.url("/drop")
    unavailable = by_code[IssueCode.ROBOTS_UNAVAILABLE]
    assert unavailable.url == f"http://127.0.0.1:{port}/robots.txt"
    assert "network_error" in unavailable.message
    assert by_code[IssueCode.ROBOTS_DISALLOWED].url == f"http://127.0.0.1:{port}/x"
    assert len([r for r in records if isinstance(r, SourceDocument)]) == 2


async def test_too_large_and_rate_limit() -> None:
    routes: dict[str, RouteSpec] = {
        "/": (200, HTML, html_page("/big", "/a", "/b")),
        "/big": (200, HTML, "x" * 20_000),
        "/a": (200, HTML, html_page()),
        "/b": (200, HTML, html_page()),
    }
    with LocalSite(routes) as site:
        records, _ = await run(config_for(site, delay_s=0.1, concurrency=3, max_bytes=5_000))
    assert IssueCode.TOO_LARGE in codes(records)
    received = sorted(r.received_at for r in site.requests)
    assert len(received) == 5  # robots + 4 pages
    gaps = [b - a for a, b in itertools.pairwise(received)]
    assert all(gap >= 0.07 for gap in gaps), gaps
