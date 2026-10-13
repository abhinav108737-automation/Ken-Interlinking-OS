"""PublicWebContentSource against httpx.MockTransport. Fully offline.

``site.test`` is a reserved TLD and the transport is a mock: nothing here can
reach a real host.
"""

from __future__ import annotations

import asyncio
import itertools
import random
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from ken_contracts.inventory import (
    ContentSourceProvider,
    CrawlConfig,
    IssueCode,
    IssueSeverity,
    SourceDocument,
    SourceIssue,
    SourceSummary,
    SourceType,
)
from ken_services.inventory.sources.public_web import PublicWebContentSource

BASE = "https://site.test"
UA = "KenInterlinkBot/0.1 (+https://example.invalid/bot)"
FIXED_NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
HTML = {"content-type": "text/html; charset=utf-8"}

type Record = SourceDocument | SourceIssue
type Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]
type Route = tuple[int, Mapping[str, str], bytes | str] | BaseException | Handler


def page(*hrefs: str, head: str = "", body: str = "") -> str:
    links = "".join(f'<a href="{h}">{h}</a>' for h in hrefs)
    return f"<html><head><title>t</title>{head}</head><body>{links}{body}</body></html>"


def html(*hrefs: str, head: str = "") -> tuple[int, Mapping[str, str], str]:
    return (200, HTML, page(*hrefs, head=head))


def redirect(location: str, status: int = 301) -> tuple[int, Mapping[str, str], str]:
    return (status, {"location": location}, "")


class FakeTime:
    """Monotonic clock advanced only by the injected sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class MockSite:
    """Routes keyed by absolute URL; unknown URLs answer 404 (robots.txt included)."""

    def __init__(
        self,
        routes: Mapping[str, Route],
        *,
        time: FakeTime | None = None,
        latency: Callable[[str], float] | None = None,
    ) -> None:
        self.routes = dict(routes)
        self.time = time
        self.latency = latency
        self.requests: list[httpx.Request] = []
        self.starts: list[tuple[float, str]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    def page_urls(self) -> list[str]:
        return [u for u in self.urls() if not u.endswith("/robots.txt")]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(request)
        self.starts.append((self.time.now if self.time else 0.0, url))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.latency is not None:
                await asyncio.sleep(self.latency(url))
            route = self.routes.get(url)
            if route is None:
                return httpx.Response(404, headers={"content-type": "text/plain"}, text="nf")
            if isinstance(route, BaseException):
                raise route
            if callable(route):
                result = route(request)
                return result if isinstance(result, httpx.Response) else await result
            status, headers, body = route
            content = body.encode() if isinstance(body, str) else body
            return httpx.Response(status, headers=dict(headers), content=content)
        finally:
            self.in_flight -= 1

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def make_config(**overrides: Any) -> CrawlConfig:
    values: dict[str, Any] = {
        "base_url": f"{BASE}/",
        "user_agent": UA,
        "delay_s": 0.0,
        "concurrency": 2,
        "max_pages": 100,
        "max_depth": 10,
        "timeout_s": 5.0,
        "max_bytes": 100_000,
        "max_redirects": 5,
    }
    values.update(overrides)
    return CrawlConfig(**values)


def make_source(
    site: MockSite, config: CrawlConfig | None = None, fake: FakeTime | None = None
) -> PublicWebContentSource:
    fake = fake or site.time or FakeTime()
    return PublicWebContentSource(
        config or make_config(),
        transport=site.transport(),
        sleep=fake.sleep,
        clock=lambda: FIXED_NOW,
        monotonic=fake.monotonic,
    )


async def crawl(
    site: MockSite, config: CrawlConfig | None = None, fake: FakeTime | None = None
) -> tuple[list[Record], SourceSummary]:
    source = make_source(site, config, fake)
    try:
        await source.site_scope()
        records = [record async for record in source.iter_records()]
        return records, await source.summary()
    finally:
        await source.aclose()


def docs(records: list[Record]) -> list[SourceDocument]:
    return [r for r in records if isinstance(r, SourceDocument)]


def issues(records: list[Record], code: IssueCode | None = None) -> list[SourceIssue]:
    return [r for r in records if isinstance(r, SourceIssue) and (code is None or r.code is code)]


def u(path: str) -> str:
    return f"{BASE}{path}"


# --------------------------------------------------------------------------- #
# Provider surface
# --------------------------------------------------------------------------- #
async def test_provider_surface() -> None:
    source = make_source(MockSite({}), make_config(allowed_hosts=["www.site.test"]))
    assert isinstance(source, ContentSourceProvider)
    assert source.name == "public_web"
    assert source.source_type is SourceType.PUBLIC_WEB
    scope = await source.site_scope()
    assert scope.allowed_hosts == frozenset({"site.test", "www.site.test"})
    snapshot = source.config_snapshot()
    assert snapshot["base_url"] == f"{BASE}/"
    assert snapshot["user_agent"] == UA
    await source.aclose()
    await source.aclose()  # idempotent


async def test_iter_records_consumed_once() -> None:
    source = make_source(MockSite({u("/"): html()}))
    _ = [r async for r in source.iter_records()]
    with pytest.raises(RuntimeError):
        _ = [r async for r in source.iter_records()]
    await source.aclose()


async def test_basic_bfs_crawl_and_document_fields() -> None:
    site = MockSite(
        {
            u("/"): html("/a", "/b"),
            u("/a"): (200, {**HTML, "content-language": "en-GB"}, page("/c")),
            u("/b"): html(),
            u("/c"): html(),
        }
    )
    records, summary = await crawl(site)
    assert [d.final_url for d in docs(records)] == [u("/"), u("/a"), u("/b"), u("/c")]
    assert [d.depth for d in docs(records)] == [0, 1, 1, 2]
    a = docs(records)[1]
    assert a.source_type is SourceType.PUBLIC_WEB
    assert a.source_id == a.final_url == a.requested_url == u("/a")
    assert a.redirect_chain == ()
    assert a.http_status == 200
    assert a.content_type == "text/html"
    assert a.content_language == "en-GB"
    assert a.fetched_at == FIXED_NOW
    assert "<a href" in a.html
    assert issues(records) == []
    assert summary == SourceSummary(discovered=4, skipped=0)
    assert site.urls()[0] == u("/robots.txt")
    assert site.urls().count(u("/robots.txt")) == 1


async def test_charset_decoding_and_fallback() -> None:
    site = MockSite(
        {
            u("/"): html("/latin", "/bogus"),
            u("/latin"): (
                200,
                {"content-type": "text/html; charset=iso-8859-1"},
                "café".encode("latin-1"),
            ),
            u("/bogus"): (200, {"content-type": "text/html; charset=nope-42"}, b"ok \xff"),
        }
    )
    records, _ = await crawl(site)
    by_url = {d.final_url: d for d in docs(records)}
    assert by_url[u("/latin")].html == "café"
    assert by_url[u("/bogus")].html == "ok �"


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #
async def test_crawl_respects_robots() -> None:
    robots = "User-agent: *\nDisallow: /private\n"
    site = MockSite(
        {
            u("/robots.txt"): (200, {"content-type": "text/plain"}, robots),
            u("/"): html("/private/x", "/public"),
            u("/public"): html(),
        }
    )
    records, summary = await crawl(site)
    assert u("/private/x") not in site.urls()
    [blocked] = issues(records, IssueCode.ROBOTS_DISALLOWED)
    assert blocked.url == u("/private/x")
    assert blocked.severity is IssueSeverity.INFO
    assert summary.skipped == 1
    assert [d.final_url for d in docs(records)] == [u("/"), u("/public")]


async def test_robots_404_allows_all() -> None:
    site = MockSite({u("/"): html("/a"), u("/a"): html()})
    records, _ = await crawl(site)
    assert len(docs(records)) == 2
    assert issues(records) == []


@pytest.mark.parametrize(
    "route",
    [
        (503, {}, "down"),
        (500, {}, ""),
        httpx.ReadTimeout("secret detail"),
        httpx.ConnectError("secret detail"),
    ],
)
async def test_robots_unavailable_disallows_all(route: Route) -> None:
    site = MockSite({u("/robots.txt"): route, u("/"): html("/a")})
    records, summary = await crawl(site)
    assert site.urls() == [u("/robots.txt")]
    [unavailable] = issues(records, IssueCode.ROBOTS_UNAVAILABLE)
    assert unavailable.severity is IssueSeverity.ERROR
    assert unavailable.url == u("/robots.txt")
    assert "secret" not in unavailable.message
    [blocked] = issues(records, IssueCode.ROBOTS_DISALLOWED)
    assert blocked.url == u("/")
    assert records[0] is unavailable
    assert docs(records) == []
    assert summary == SourceSummary(discovered=1, skipped=1)


async def test_robots_redirect_off_scope_disallows() -> None:
    site = MockSite(
        {u("/robots.txt"): redirect("https://elsewhere.test/robots.txt"), u("/"): html()}
    )
    records, _ = await crawl(site)
    assert "https://elsewhere.test/robots.txt" not in site.urls()
    assert len(issues(records, IssueCode.ROBOTS_UNAVAILABLE)) == 1
    assert docs(records) == []


async def test_robots_redirect_in_scope_followed() -> None:
    site = MockSite(
        {
            u("/robots.txt"): redirect("/real-robots.txt"),
            u("/real-robots.txt"): (200, {}, "User-agent: *\nDisallow: /no\n"),
            u("/"): html("/no", "/yes"),
            u("/yes"): html(),
        }
    )
    records, _ = await crawl(site)
    assert [d.final_url for d in docs(records)] == [u("/"), u("/yes")]
    assert len(issues(records, IssueCode.ROBOTS_DISALLOWED)) == 1


async def test_robots_redirect_loop_disallows() -> None:
    site = MockSite({u("/robots.txt"): redirect("/robots.txt"), u("/"): html()})
    records, _ = await crawl(site)
    assert site.urls().count(u("/robots.txt")) == 6
    assert len(issues(records, IssueCode.ROBOTS_UNAVAILABLE)) == 1


async def test_robots_body_capped() -> None:
    body = "User-agent: *\n" + "#" * 600_000 + "\nDisallow: /\n"
    site = MockSite({u("/robots.txt"): (200, {}, body), u("/"): html()})
    records, _ = await crawl(site)
    assert len(docs(records)) == 1  # the Disallow beyond 500 KiB is ignored


async def test_robots_fetched_once_per_origin() -> None:
    site = MockSite(
        {
            u("/"): html("http://site.test/plain", "/a"),
            u("/a"): html("/b"),
            u("/b"): html(),
            "http://site.test/plain": html(),
        }
    )
    await crawl(site)
    assert site.urls().count(u("/robots.txt")) == 1
    assert site.urls().count("http://site.test/robots.txt") == 1


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
async def test_external_links_never_requested_and_www_not_implied() -> None:
    site = MockSite(
        {
            u("/"): html(
                "https://other.test/x",
                "https://www.site.test/y",
                "//cdn.test/z",
                "mailto:a@b.test",
                "javascript:void(0)",
                "http://site.test/insecure",
            ),
            "http://site.test/insecure": html(),
        }
    )
    records, summary = await crawl(site)
    hosts = {httpx.URL(url).host for url in site.urls()}
    assert hosts == {"site.test"}
    assert [d.final_url for d in docs(records)] == [u("/"), "http://site.test/insecure"]
    assert summary.discovered == 2


async def test_allowed_hosts_extend_scope() -> None:
    site = MockSite({u("/"): html("https://www.site.test/y"), "https://www.site.test/y": html()})
    records, _ = await crawl(site, make_config(allowed_hosts=["www.site.test"]))
    assert [d.final_url for d in docs(records)] == [u("/"), "https://www.site.test/y"]
    assert "https://www.site.test/robots.txt" in site.urls()


async def test_base_href_in_scope_used_and_off_scope_ignored() -> None:
    site = MockSite(
        {
            u("/"): html("/in", "/out"),
            u("/in"): html("rel", head='<base href="/dir/">'),
            u("/out"): html("rel2", head='<base href="https://other.test/x/">'),
            u("/dir/rel"): html(),
            u("/rel2"): html(),
        }
    )
    records, _ = await crawl(site)
    finals = [d.final_url for d in docs(records)]
    assert u("/dir/rel") in finals
    assert u("/rel2") in finals
    assert all("other.test" not in url for url in site.urls())


# --------------------------------------------------------------------------- #
# Redirects
# --------------------------------------------------------------------------- #
async def test_in_scope_redirect_followed_with_chain() -> None:
    site = MockSite(
        {
            u("/"): html("/old"),
            u("/old"): redirect("/mid", 302),
            u("/mid"): redirect("http://site.test/new", 308),
            "http://site.test/new": html(),
        }
    )
    records, summary = await crawl(site)
    doc = docs(records)[1]
    assert doc.requested_url == u("/old")
    assert doc.final_url == doc.source_id == "http://site.test/new"
    assert doc.redirect_chain == (u("/old"), u("/mid"))
    assert doc.depth == 1
    assert summary.discovered == 4


async def test_redirect_out_of_scope_refused() -> None:
    site = MockSite({u("/"): html("/go"), u("/go"): redirect("https://other.test/landing")})
    records, _ = await crawl(site)
    [issue] = issues(records, IssueCode.REDIRECT_OUT_OF_SCOPE)
    assert issue.severity is IssueSeverity.WARNING
    assert issue.url == u("/go")
    assert issue.related_url == "https://other.test/landing"
    assert issue.http_status == 301
    assert all("other.test" not in url for url in site.urls())


async def test_redirect_loop_detected() -> None:
    site = MockSite({u("/"): html("/l1"), u("/l1"): redirect("/l2"), u("/l2"): redirect("/l1")})
    records, _ = await crawl(site)
    [issue] = issues(records, IssueCode.REDIRECT_LOOP)
    assert issue.severity is IssueSeverity.ERROR
    assert issue.url == u("/l1")
    assert issue.related_url == u("/l1")
    assert site.page_urls().count(u("/l1")) == 1
    assert site.page_urls().count(u("/l2")) == 1


async def test_too_many_redirects() -> None:
    routes: dict[str, Route] = {u("/"): html("/r0")}
    for i in range(5):
        routes[u(f"/r{i}")] = redirect(f"/r{i + 1}")
    routes[u("/r5")] = html()
    records, _ = await crawl(MockSite(routes), make_config(max_redirects=2))
    [issue] = issues(records, IssueCode.TOO_MANY_REDIRECTS)
    assert issue.severity is IssueSeverity.ERROR
    assert issue.url == u("/r0")
    assert issue.related_url == u("/r3")
    assert len(docs(records)) == 1


async def test_redirect_to_already_visited_is_duplicate() -> None:
    site = MockSite({u("/"): html("/alias"), u("/alias"): redirect("/")})
    records, _ = await crawl(site)
    [issue] = issues(records, IssueCode.DUPLICATE_URL)
    assert issue.severity is IssueSeverity.INFO
    assert issue.url == u("/alias")
    assert issue.related_url == u("/")
    assert site.page_urls().count(u("/")) == 1


async def test_two_redirects_to_same_target_fetch_it_once() -> None:
    site = MockSite(
        {
            u("/"): html("/r1", "/r2"),
            u("/r1"): redirect("/target"),
            u("/r2"): redirect("/target"),
            u("/target"): html(),
        }
    )
    records, _ = await crawl(site)
    assert site.page_urls().count(u("/target")) == 1
    [doc] = [d for d in docs(records) if d.final_url == u("/target")]
    assert doc.requested_url == u("/r1")
    [dup] = issues(records, IssueCode.DUPLICATE_URL)
    assert (dup.url, dup.related_url) == (u("/r2"), u("/target"))


async def test_redirect_hop_robots_checked_and_invalid_target() -> None:
    site = MockSite(
        {
            u("/robots.txt"): (200, {}, "User-agent: *\nDisallow: /secret\n"),
            u("/"): html("/to-secret", "/to-bad", "/no-location"),
            u("/to-secret"): redirect("/secret/page"),
            u("/to-bad"): redirect("http://[bad"),
            u("/no-location"): (302, {}, ""),
        }
    )
    records, _ = await crawl(site)
    assert u("/secret/page") not in site.urls()
    [blocked] = issues(records, IssueCode.ROBOTS_DISALLOWED)
    assert (blocked.url, blocked.related_url) == (u("/to-secret"), u("/secret/page"))
    [invalid] = issues(records, IssueCode.INVALID_URL)
    assert invalid.url == u("/to-bad")
    assert invalid.severity is IssueSeverity.ERROR
    [no_location] = issues(records, IssueCode.HTTP_ERROR)
    assert no_location.http_status == 302


# --------------------------------------------------------------------------- #
# Loops, duplicates, limits
# --------------------------------------------------------------------------- #
async def test_canonical_is_not_followed_or_used_for_crawling() -> None:
    site = MockSite(
        {
            u("/"): html("/a"),
            u("/a"): html("/b", head=f'<link rel="canonical" href="{BASE}/canonical">'),
            u("/b"): html(),
        }
    )
    records, _ = await crawl(site)
    assert u("/canonical") not in site.urls()
    assert [d.final_url for d in docs(records)] == [u("/"), u("/a"), u("/b")]


async def test_malformed_hrefs_ignored_without_crashing() -> None:
    site = MockSite(
        {
            u("/"): html(
                "http://[bad", "https://[::1", "http://site.test:99999/", "", "#top", "/ok"
            ),
            u("/ok"): html(),
        }
    )
    records, _ = await crawl(site)
    assert [d.final_url for d in docs(records)] == [u("/"), u("/ok")]
    assert issues(records) == []


async def test_duplicate_spellings_requested_once() -> None:
    site = MockSite(
        {
            u("/"): html(
                "/a", "/a#frag", "/x/../a", "/%61", "https://SITE.test:443/a", "/a?utm_source=x"
            ),
            u("/a"): html(),
        }
    )
    _records, summary = await crawl(site)
    assert site.page_urls() == [u("/"), u("/a")]
    assert summary.discovered == 2


async def test_link_loop_fetched_once_each() -> None:
    site = MockSite({u("/"): html("/a"), u("/a"): html("/b", "/"), u("/b"): html("/a", "/")})
    records, _ = await crawl(site)
    assert sorted(site.page_urls()) == [u("/"), u("/a"), u("/b")]
    assert len(docs(records)) == 3


async def test_depth_limit() -> None:
    site = MockSite(
        {u("/"): html("/1"), u("/1"): html("/2", "/2b"), u("/2"): html("/3"), u("/2b"): html()}
    )
    records, summary = await crawl(site, make_config(max_depth=1))
    assert site.page_urls() == [u("/"), u("/1")]
    assert summary == SourceSummary(discovered=4, skipped=2)
    assert issues(records) == []


async def test_max_depth_zero_fetches_only_seed() -> None:
    site = MockSite({u("/"): html("/1")})
    _, summary = await crawl(site, make_config(max_depth=0))
    assert site.page_urls() == [u("/")]
    assert summary.skipped == 1


async def test_max_pages_counts_errors_too() -> None:
    routes: dict[str, Route] = {u("/"): html(*(f"/e{i}" for i in range(6)))}
    for i in range(6):
        routes[u(f"/e{i}")] = (500, {}, "boom")
    site = MockSite(routes)
    records, summary = await crawl(site, make_config(max_pages=3))
    assert site.page_urls() == [u("/"), u("/e0"), u("/e1")]
    [limit] = issues(records, IssueCode.LIMIT_REACHED)
    assert limit.severity is IssueSeverity.INFO
    assert "4" in limit.message
    assert records[-1] is limit
    assert len(issues(records, IssueCode.HTTP_ERROR)) == 2
    assert summary == SourceSummary(discovered=7, skipped=4)


async def test_max_pages_counts_redirect_chain_as_one_request() -> None:
    site = MockSite({u("/"): redirect("/home"), u("/home"): html("/a"), u("/a"): html()})
    records, _ = await crawl(site, make_config(max_pages=2))
    assert site.page_urls() == [u("/"), u("/home"), u("/a")]
    assert issues(records, IssueCode.LIMIT_REACHED) == []


async def test_query_variant_trap_capped() -> None:
    hrefs = [f"/list?p={i}" for i in range(20)]
    routes: dict[str, Route] = {u("/"): html(*hrefs, "/other?q=1")}
    for href in [*hrefs, "/other?q=1"]:
        routes[u(href)] = html(*hrefs)
    site = MockSite(routes)
    records, summary = await crawl(site, make_config(max_query_variants_per_path=3))
    list_requests = [x for x in site.page_urls() if "/list" in x]
    assert list_requests == [u("/list?p=0"), u("/list?p=1"), u("/list?p=2")]
    [limit] = issues(records, IssueCode.LIMIT_REACHED)
    assert limit.url == u("/list")
    assert summary.skipped == 17


async def test_meta_nofollow_respected() -> None:
    nofollow = '<meta name="robots" content="noindex, nofollow">'
    routes: dict[str, Route] = {u("/"): html("/a", head=nofollow), u("/a"): html()}
    site = MockSite(routes)
    records, _ = await crawl(site)
    assert site.page_urls() == [u("/")]
    assert len(docs(records)) == 1

    site2 = MockSite(routes)
    await crawl(site2, make_config(respect_meta_nofollow=False))
    assert site2.page_urls() == [u("/"), u("/a")]


# --------------------------------------------------------------------------- #
# Responses and failures
# --------------------------------------------------------------------------- #
class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.read = False

    async def __aiter__(self) -> Any:
        self.read = True
        yield self.data


async def test_non_html_skipped_without_reading_body() -> None:
    stream = TrackingStream(b"%PDF-1.7")
    site = MockSite(
        {
            u("/"): html("/doc.pdf", "/plain"),
            u("/doc.pdf"): lambda _r: httpx.Response(
                200, headers={"content-type": "application/pdf"}, stream=stream
            ),
            u("/plain"): (200, {}, "no content type"),
        }
    )
    records, summary = await crawl(site)
    assert stream.read is False
    non_html = issues(records, IssueCode.NON_HTML)
    assert [i.url for i in non_html] == [u("/doc.pdf"), u("/plain")]
    assert non_html[0].severity is IssueSeverity.INFO
    assert non_html[0].http_status == 200
    assert summary == SourceSummary(discovered=3, skipped=2)


async def test_xhtml_is_html() -> None:
    site = MockSite({u("/"): (200, {"content-type": "application/xhtml+xml"}, page())})
    records, _ = await crawl(site)
    assert docs(records)[0].content_type == "application/xhtml+xml"


async def test_too_large_declared_and_streamed() -> None:
    big = b"<html>" + b"x" * 5000
    site = MockSite(
        {
            u("/"): html("/declared", "/streamed", "/ok"),
            u("/declared"): (200, {**HTML, "content-length": "999999"}, b"<html></html>"),
            u("/streamed"): lambda _r: httpx.Response(
                200, headers=HTML, stream=TrackingStream(big)
            ),
            u("/ok"): html(),
        }
    )
    records, _ = await crawl(site, make_config(max_bytes=2048))
    too_large = issues(records, IssueCode.TOO_LARGE)
    assert [i.url for i in too_large] == [u("/declared"), u("/streamed")]
    assert all(i.severity is IssueSeverity.WARNING for i in too_large)
    assert [d.final_url for d in docs(records)] == [u("/"), u("/ok")]


async def test_status_mapping_and_errors_never_crash() -> None:
    site = MockSite(
        {
            u("/"): html(
                "/404", "/410", "/500", "/403", "/timeout", "/net", "/weird", "/204", "/ok"
            ),
            u("/404"): (404, HTML, "gone?"),
            u("/410"): (410, HTML, "gone"),
            u("/500"): (500, HTML, "err"),
            u("/403"): (403, HTML, "forbidden"),
            u("/timeout"): httpx.ReadTimeout("token=abc123"),
            u("/net"): httpx.ConnectError("password=hunter2"),
            u("/weird"): (999, HTML, "?"),
            u("/204"): (204, {"content-type": "text/plain"}, ""),
            u("/ok"): html(),
        }
    )
    records, _ = await crawl(site, make_config(concurrency=4))
    by_url = {i.url: i for i in issues(records)}
    assert (by_url[u("/404")].code, by_url[u("/404")].severity) == (
        IssueCode.HTTP_NOT_FOUND,
        IssueSeverity.WARNING,
    )
    assert by_url[u("/404")].http_status == 404
    assert (by_url[u("/410")].code, by_url[u("/410")].http_status) == (IssueCode.HTTP_GONE, 410)
    assert (by_url[u("/500")].code, by_url[u("/500")].severity) == (
        IssueCode.HTTP_ERROR,
        IssueSeverity.ERROR,
    )
    assert by_url[u("/403")].http_status == 403
    assert by_url[u("/timeout")].code is IssueCode.TIMEOUT
    assert by_url[u("/net")].code is IssueCode.NETWORK_ERROR
    assert by_url[u("/weird")].code is IssueCode.HTTP_ERROR
    assert by_url[u("/weird")].http_status is None
    assert by_url[u("/204")].code is IssueCode.NON_HTML
    for issue in issues(records):
        assert "abc123" not in issue.message
        assert "hunter2" not in issue.message
    assert [d.final_url for d in docs(records)] == [u("/"), u("/ok")]


async def test_failure_after_redirect_reports_answering_url() -> None:
    site = MockSite({u("/"): html("/moved"), u("/moved"): redirect("/missing")})
    records, _ = await crawl(site)
    [issue] = issues(records, IssueCode.HTTP_NOT_FOUND)
    assert (issue.url, issue.related_url) == (u("/missing"), u("/moved"))


async def test_request_deadline_covers_slow_responses() -> None:
    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, headers=HTML, text=page())

    site = MockSite({u("/"): html("/slow", "/ok"), u("/slow"): slow, u("/ok"): html()})
    records, _ = await crawl(site, make_config(timeout_s=0.05))
    [issue] = issues(records, IssueCode.TIMEOUT)
    assert issue.url == u("/slow")
    assert len(docs(records)) == 2


# --------------------------------------------------------------------------- #
# Politeness: rate, concurrency, headers
# --------------------------------------------------------------------------- #
def gaps(starts: list[tuple[float, str]]) -> list[float]:
    times = sorted(t for t, _ in starts)
    return [b - a for a, b in itertools.pairwise(times)]


async def test_rate_limit_between_request_starts() -> None:
    fake = FakeTime()
    site = MockSite(
        {u("/"): html(*(f"/p{i}" for i in range(6))), **{u(f"/p{i}"): html() for i in range(6)}},
        time=fake,
    )
    await crawl(site, make_config(delay_s=1.0, concurrency=4))
    assert len(site.starts) == 8  # robots + seed + 6
    assert all(gap >= 1.0 - 1e-9 for gap in gaps(site.starts))


async def test_crawl_delay_honoured_when_larger_and_capped() -> None:
    for delay, expected in [("5", 5.0), ("1000", 60.0), ("0.1", 1.0)]:
        fake = FakeTime()
        site = MockSite(
            {
                u("/robots.txt"): (200, {}, f"User-agent: *\nCrawl-delay: {delay}\n"),
                u("/"): html("/a", "/b"),
                u("/a"): html(),
                u("/b"): html(),
            },
            time=fake,
        )
        await crawl(site, make_config(delay_s=1.0, concurrency=2))
        page_gaps = gaps(site.starts)[1:]  # after the robots.txt request
        assert page_gaps
        assert all(gap >= expected - 1e-9 for gap in page_gaps), (delay, page_gaps)
        assert max(page_gaps) <= expected + 1e-9


async def test_concurrency_bounded() -> None:
    n = 20
    site = MockSite(
        {u("/"): html(*(f"/p{i}" for i in range(n))), **{u(f"/p{i}"): html() for i in range(n)}},
        latency=lambda _url: 0.005,
    )
    records, _ = await crawl(site, make_config(concurrency=3))
    assert len(docs(records)) == n + 1
    assert 2 <= site.max_in_flight <= 3


async def test_get_only_user_agent_and_no_cookies_or_auth() -> None:
    site = MockSite(
        {
            u("/"): (200, {**HTML, "set-cookie": "session=s3cr3t; Path=/"}, page("/a")),
            u("/a"): (200, {**HTML, "set-cookie": "other=1; Domain=site.test"}, page("/b")),
            u("/b"): html(),
        }
    )
    await crawl(site)
    assert len(site.requests) == 4
    for request in site.requests:
        assert request.method == "GET"
        assert request.headers["user-agent"] == UA
        assert request.headers["accept"].startswith("text/html")
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers


# --------------------------------------------------------------------------- #
# Determinism and summary
# --------------------------------------------------------------------------- #
def trap_site(seed: int) -> MockSite:
    rng = random.Random(seed)  # noqa: S311 - test latency jitter, not security
    latencies: dict[str, float] = {}

    def latency(url: str) -> float:
        return latencies.setdefault(url, rng.uniform(0, 0.004))

    routes: dict[str, Route] = {
        u("/robots.txt"): (200, {}, "User-agent: *\nDisallow: /blocked\n"),
        u("/"): html("/a", "/b", "/c", "/r1", "/r2", "/blocked", "/404"),
        u("/a"): html("/a1", "/a2", "/b", "/r3"),
        u("/b"): html("/b1", "/a", "/x?q=1", "/x?q=2", "/x?q=3"),
        u("/c"): html("/c1", "https://other.test/"),
        u("/r1"): redirect("/target"),
        u("/r2"): redirect("/target"),
        u("/r3"): redirect("/a1"),
        u("/target"): html("/t1"),
    }
    for path in ["/a1", "/a2", "/b1", "/c1", "/t1", "/x?q=1", "/x?q=2", "/x?q=3"]:
        routes[u(path)] = html("/", "/a")
    return MockSite(routes, latency=latency)


async def test_determinism_under_random_latency() -> None:
    config = make_config(concurrency=4, max_query_variants_per_path=2)
    first, summary1 = await crawl(trap_site(1), config)
    second, summary2 = await crawl(trap_site(2), config)
    assert first == second
    assert summary1 == summary2
    assert len(docs(first)) > 5


async def test_summary_counts_mixed_site() -> None:
    site = MockSite(
        {
            u("/robots.txt"): (200, {}, "User-agent: *\nDisallow: /blocked\n"),
            u("/"): html("/a", "/blocked", "/file.zip", "/missing", "https://other.test/"),
            u("/a"): html("/deep"),
            u("/file.zip"): (200, {"content-type": "application/zip"}, b"PK"),
        }
    )
    records, summary = await crawl(site, make_config(max_depth=1))
    # discovered: /, /a, /blocked, /file.zip, /missing, /deep
    # skipped: /blocked (robots), /deep (depth), /file.zip (non-HTML); /missing is an error
    assert summary == SourceSummary(discovered=6, skipped=3)
    assert len(issues(records, IssueCode.HTTP_NOT_FOUND)) == 1
