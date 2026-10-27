"""Browser-rendered crawling (Playwright) against a LOCAL loopback site. No external network.

The fixture server is on 127.0.0.1 with an ephemeral port; the crawler's SSRF guard admits loopback
only through the explicit test switch. Skips when Playwright or Chromium is not installed.
"""

from __future__ import annotations

import http.server
import itertools
import socketserver
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ken_contracts.inventory import CrawlConfig, IssueCode, SourceDocument, SourceIssue
from ken_services.inventory.sources.public_web import PublicWebContentSource

pytestmark = pytest.mark.browser

try:  # pragma: no cover - environment dependent
    from playwright.sync_api import sync_playwright

    with sync_playwright() as _p:
        _AVAILABLE = Path(_p.chromium.executable_path).exists()
except Exception:
    _AVAILABLE = False

if not _AVAILABLE:
    pytest.skip("Playwright/Chromium not installed", allow_module_level=True)

HTML = "text/html; charset=utf-8"


class Site:
    """A tiny local site: routes -> (status, headers, body); logs every request."""

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], bytes]]) -> None:
        self.routes = routes
        self.log: list[tuple[str, float, dict[str, str]]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.log.append((self.path, time.monotonic(), dict(self.headers)))
                status, headers, body = outer.routes.get(self.path, (404, {}, b"nf"))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a: Any) -> None:
                return

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def paths(self) -> list[str]:
        return [p for p, _, _ in self.log]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def sites() -> Iterator[list[Site]]:
    made: list[Site] = []
    yield made
    for site in made:
        site.close()


def page(body: str, head: str = "") -> bytes:
    return f"<html><head><title>t</title>{head}</head><body>{body}</body></html>".encode()


def config(site: Site, **overrides: Any) -> CrawlConfig:
    values: dict[str, Any] = {
        "base_url": f"{site.base}/",
        "user_agent": (
            "KenInterlinkBot/0.1 (+mailto:inventory@pilot-fixture.test; read-only inventory)"
        ),
        "delay_s": 0.0,
        "concurrency": 1,
        "max_pages": 20,
        "max_depth": 3,
        "timeout_s": 15.0,
        "max_bytes": 200_000,
        "max_redirects": 3,
        "render": "browser",
    }
    values.update(overrides)
    return CrawlConfig(**values)


async def crawl(cfg: CrawlConfig) -> list[SourceDocument | SourceIssue]:
    source = PublicWebContentSource(cfg, allow_private_networks=True)
    try:
        await source.site_scope()
        return [r async for r in source.iter_records()]
    finally:
        await source.aclose()


def docs(records: list[SourceDocument | SourceIssue]) -> list[SourceDocument]:
    return [r for r in records if isinstance(r, SourceDocument)]


def codes(records: list[SourceDocument | SourceIssue]) -> list[str]:
    return [r.code.value for r in records if isinstance(r, SourceIssue)]


JS = b"document.getElementById('a').innerHTML='<a href=\"/rendered\">rendered link</a>';"


async def test_javascript_built_content_is_rendered_and_its_links_are_followed(
    sites: list[Site],
) -> None:
    site = Site(
        {
            "/": (
                200,
                {"Content-Type": HTML},
                page('<div id="a">loading</div><script src="/app.js"></script>'),
            ),
            "/app.js": (200, {"Content-Type": "application/javascript"}, JS),
            "/rendered": (200, {"Content-Type": HTML}, page("<p>second page</p>")),
        }
    )
    sites.append(site)
    records = await crawl(config(site))
    first = docs(records)[0]
    assert "rendered link" in first.html and "loading" not in first.html
    assert f"{site.base}/rendered" in [d.final_url for d in docs(records)]


async def test_only_the_document_and_same_host_scripts_are_requested(sites: list[Site]) -> None:
    other = Site({"/evil.js": (200, {"Content-Type": "application/javascript"}, b"")})
    sites.append(other)
    site = Site(
        {
            "/": (
                200,
                {"Content-Type": HTML},
                page(
                    '<script src="/app.js"></script><img src="/pic.png">'
                    '<link rel="stylesheet" href="/s.css">'
                    f'<script src="{other.base}/evil.js"></script><iframe src="/frame"></iframe>',
                ),
            ),
            "/app.js": (200, {"Content-Type": "application/javascript"}, b""),
        }
    )
    sites.append(site)
    await crawl(config(site))
    assert sorted(set(site.paths()) - {"/robots.txt"}) == ["/", "/app.js"]
    assert other.paths() == [], "a third-party script is never fetched"


async def test_the_subrequest_budget_is_enforced(sites: list[Site]) -> None:
    scripts = "".join(f'<script src="/s{i}.js"></script>' for i in range(10))
    routes: dict[str, tuple[int, dict[str, str], bytes]] = {
        "/": (200, {"Content-Type": HTML}, page(scripts))
    }
    routes.update(
        {f"/s{i}.js": (200, {"Content-Type": "application/javascript"}, b"") for i in range(10)}
    )
    site = Site(routes)
    sites.append(site)
    await crawl(config(site, browser_max_subrequests=3))
    assert len([p for p in site.paths() if p.endswith(".js")]) == 3


async def test_redirects_are_checked_by_the_crawler_not_followed_by_the_browser(
    sites: list[Site],
) -> None:
    outside = Site({"/": (200, {"Content-Type": HTML}, page("outside"))})
    sites.append(outside)
    site = Site(
        {
            "/": (200, {"Content-Type": HTML}, page('<a href="/r1">a</a><a href="/leak">b</a>')),
            "/r1": (302, {"Location": "/r2"}, b""),
            "/r2": (200, {"Content-Type": HTML}, page("landed")),
            "/leak": (302, {"Location": f"{outside.base}/"}, b""),
        }
    )
    sites.append(site)
    records = await crawl(config(site))
    landed = [d for d in docs(records) if d.final_url.endswith("/r2")]
    assert landed and landed[0].redirect_chain == (f"{site.base}/r1",)
    assert IssueCode.REDIRECT_OUT_OF_SCOPE.value in codes(records)
    assert outside.paths() == [], "the browser never followed the redirect off the approved host"


async def test_robots_disallow_applies_to_pages_and_to_scripts(sites: list[Site]) -> None:
    site = Site(
        {
            "/robots.txt": (200, {}, b"User-agent: *\nDisallow: /private\nDisallow: /blocked.js\n"),
            "/": (
                200,
                {"Content-Type": HTML},
                page('<a href="/private/x">p</a><script src="/blocked.js"></script>'),
            ),
            "/private/x": (200, {"Content-Type": HTML}, page("no")),
            "/blocked.js": (200, {"Content-Type": "application/javascript"}, b""),
        }
    )
    sites.append(site)
    await crawl(config(site))
    assert "/private/x" not in site.paths() and "/blocked.js" not in site.paths()


async def test_every_request_is_paced_by_the_shared_limiter(sites: list[Site]) -> None:
    site = Site(
        {
            "/": (200, {"Content-Type": HTML}, page('<script src="/a.js"></script>')),
            "/a.js": (200, {"Content-Type": "application/javascript"}, b""),
        }
    )
    sites.append(site)
    await crawl(config(site, delay_s=0.4, max_pages=1))
    times = [t for p, t, _ in site.log]
    assert len(times) == 3  # robots.txt, the document, the script
    assert all(b - a >= 0.35 for a, b in itertools.pairwise(times))


async def test_requests_are_cookieless_and_carry_the_honest_user_agent(sites: list[Site]) -> None:
    site = Site(
        {
            "/": (
                200,
                {"Content-Type": HTML, "Set-Cookie": "sid=1; Path=/"},
                page('<a href="/b">b</a>'),
            ),
            "/b": (200, {"Content-Type": HTML}, page("b")),
        }
    )
    sites.append(site)
    await crawl(config(site))
    for _, _, raw in site.log:
        headers = {k.lower(): v for k, v in raw.items()}
        assert "cookie" not in headers and "authorization" not in headers
        assert headers["user-agent"].startswith("KenInterlinkBot/0.1 (+mailto:inventory@")


async def test_status_type_and_size_are_reported_without_rendering(sites: list[Site]) -> None:
    site = Site(
        {
            "/": (
                200,
                {"Content-Type": HTML},
                page('<a href="/missing">m</a><a href="/pdf">p</a><a href="/big">b</a>'),
            ),
            "/pdf": (200, {"Content-Type": "application/pdf"}, b"%PDF"),
            "/big": (200, {"Content-Type": HTML}, b"<p>" + b"x" * 300_000 + b"</p>"),
        }
    )
    sites.append(site)
    records = await crawl(config(site))
    found = codes(records)
    assert {"http_not_found", "non_html", "too_large"} <= set(found)


async def test_backoff_stops_a_browser_crawl_after_consecutive_blocks(sites: list[Site]) -> None:
    routes: dict[str, tuple[int, dict[str, str], bytes]] = {
        "/": (
            200,
            {"Content-Type": HTML},
            page("".join(f'<a href="/p{i}">x</a>' for i in range(8))),
        )
    }
    routes.update({f"/p{i}": (429, {"Retry-After": "0"}, b"slow") for i in range(8)})
    site = Site(routes)
    sites.append(site)
    records = await crawl(config(site, max_consecutive_blocks=3, max_retry_after_s=5.0))
    assert len([p for p in site.paths() if p.startswith("/p")]) == 3
    assert [r for r in records if isinstance(r, SourceIssue) and "aborted" in r.message]


async def test_a_page_that_navigates_itself_is_not_followed(sites: list[Site]) -> None:
    outside = Site({"/": (200, {"Content-Type": HTML}, page("outside"))})
    sites.append(outside)
    site = Site(
        {
            "/": (
                200,
                {"Content-Type": HTML},
                page(f'<script>location.replace("{outside.base}/")</script>'),
            )
        }
    )
    sites.append(site)
    records = await crawl(config(site))
    assert not docs(records)
    assert outside.paths() == [], "a script-driven navigation off the host is blocked"


async def test_screenshots_are_saved_only_when_asked(sites: list[Site], tmp_path: Path) -> None:
    site = Site({"/": (200, {"Content-Type": HTML}, page("<h1>hello</h1>"))})
    sites.append(site)
    source = PublicWebContentSource(
        config(site, max_pages=1), allow_private_networks=True, screenshot_dir=tmp_path / "shots"
    )
    try:
        _ = [r async for r in source.iter_records()]
    finally:
        await source.aclose()
    assert len(list((tmp_path / "shots").glob("*.png"))) == 1


async def test_browser_mode_refuses_an_injected_transport(sites: list[Site]) -> None:
    """A browser opens real sockets: it must never run in place of a mock transport (this once let a
    test drive the shipped pilot configuration against the real site)."""
    import httpx

    site = Site({"/": (200, {"Content-Type": HTML}, page("x"))})
    sites.append(site)
    source = PublicWebContentSource(
        config(site), transport=httpx.MockTransport(lambda request: httpx.Response(404))
    )
    try:
        with pytest.raises(RuntimeError, match="real network path"):
            _ = [r async for r in source.iter_records()]
    finally:
        await source.aclose()
