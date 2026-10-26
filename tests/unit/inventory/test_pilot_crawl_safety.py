"""Crawl-politeness and rule features added for the Ken public-site pilot. Fully offline.

Every test drives ``PublicWebContentSource`` through ``httpx.MockTransport`` (no socket): the hosts
are fictional or reserved and nothing here can reach a real website.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ken_contracts.inventory import IssueCode
from ken_services.inventory.sources.public_web import rule_reason
from tests.unit.inventory.test_public_web_source import (
    FakeTime,
    MockSite,
    crawl,
    docs,
    html,
    issues,
    make_config,
    redirect,
    u,
)


def _paths(site: MockSite) -> list[str]:
    return [url.replace("https://site.test", "") for url in site.page_urls()]


# --------------------------------------------------------------------------- #
# Include / exclude rules
# --------------------------------------------------------------------------- #
async def test_excluded_paths_queries_and_extensions_are_never_requested() -> None:
    site = MockSite(
        {
            u("/"): html(
                "/reports/ev-market",
                "/login",
                "/account/orders",
                "/search?s=ev",
                "/reports/ev-market?utm_source=x",
                "/files/report.pdf",
                "/cart/",
                "/insights/post?q=1",
                "/insights/post",
            ),
            u("/reports/ev-market"): html(),
            u("/insights/post"): html(),
        }
    )
    config = make_config(
        exclude_path_globs=["/login*", "/account*", "/search*", "/cart*"],
        exclude_query_params=["s", "q"],
        exclude_extensions=[".pdf"],
    )
    records, _ = await crawl(site, config)
    assert sorted(_paths(site)) == ["/", "/insights/post", "/reports/ev-market"]
    assert {d.final_url for d in docs(records)} == {
        u("/"),
        u("/insights/post"),
        u("/reports/ev-market"),
    }


async def test_a_redirect_into_an_excluded_path_is_not_followed() -> None:
    site = MockSite({u("/"): html("/go"), u("/go"): redirect(u("/login"))})
    records, _ = await crawl(site, make_config(exclude_path_globs=["/login*"]))
    assert _paths(site) == ["/", "/go"], "the login page is never requested"
    skipped = issues(records, IssueCode.OUT_OF_SCOPE)
    assert skipped and "crawl rules" in skipped[0].message


async def test_include_prefixes_restrict_the_crawl() -> None:
    site = MockSite(
        {u("/"): html("/reports/a", "/team/b"), u("/reports/a"): html(), u("/team/b"): html()}
    )
    config = make_config(include_path_prefixes=["/reports"])
    records, _ = await crawl(site, config)
    # the seed "/" is outside the include list, so nothing is requested at all
    assert [p for p in _paths(site) if p != "/robots.txt"] == []
    assert issues(records, IssueCode.OUT_OF_SCOPE)


async def test_https_only_skips_http_links() -> None:
    site = MockSite({u("/"): html("http://site.test/plain", "/ok"), u("/ok"): html()})
    await crawl(site, make_config(https_only=True))
    assert "http://site.test/plain" not in site.urls()
    assert u("/ok") in site.urls()


def test_rule_reason_is_pure_and_case_insensitive() -> None:
    config = make_config(exclude_path_globs=["/Login*"], exclude_extensions=["PDF"])
    assert rule_reason(config, "https://site.test/LOGIN/x") == "matches an exclude pattern"
    assert rule_reason(config, "https://site.test/a.PDF") == "excluded file type"
    assert rule_reason(config, "https://site.test/a") is None


# --------------------------------------------------------------------------- #
# robots.txt fails closed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [401, 403, 404, 410, 429, 500, 503])
async def test_require_robots_file_fails_closed_on_every_non_2xx(status: int) -> None:
    site = MockSite({u("/robots.txt"): (status, {}, ""), u("/"): html("/a"), u("/a"): html()})
    records, _ = await crawl(site, make_config(require_robots_file=True))
    assert site.page_urls() == [], "no page is requested without a usable robots.txt"
    assert issues(records, IssueCode.ROBOTS_UNAVAILABLE)


async def test_without_the_flag_a_404_robots_still_allows_everything() -> None:
    """The previous behaviour (RFC 9309) is unchanged for the non-pilot crawler."""
    site = MockSite({u("/"): html()})
    records, _ = await crawl(site, make_config())
    assert docs(records)


async def test_disallow_rules_are_honoured_in_the_pilot_mode() -> None:
    site = MockSite(
        {
            u("/robots.txt"): (200, {}, "User-agent: *\nDisallow: /private\n"),
            u("/"): html("/private/x", "/public"),
            u("/public"): html(),
            u("/private/x"): html(),
        }
    )
    await crawl(site, make_config(require_robots_file=True))
    assert "/private/x" not in _paths(site)
    assert "/public" in _paths(site)


# --------------------------------------------------------------------------- #
# Sitemaps never widen anything
# --------------------------------------------------------------------------- #
async def test_sitemap_urls_are_never_requested_or_followed() -> None:
    site = MockSite(
        {
            u("/robots.txt"): (
                200,
                {},
                "User-agent: *\nDisallow:\nSitemap: https://site.test/sitemap.xml\n"
                "Sitemap: https://other.test/sitemap.xml\n",
            ),
            u("/sitemap.xml"): (200, {"content-type": "application/xml"}, "<urlset/>"),
            u("/"): html("/a"),
            u("/a"): html(),
        }
    )
    await crawl(site, make_config())
    assert all("sitemap" not in url for url in site.urls())
    assert all(url.startswith("https://site.test") for url in site.urls())


# --------------------------------------------------------------------------- #
# Hosts: www and the apex are different hosts
# --------------------------------------------------------------------------- #
async def test_the_apex_is_a_separate_host_until_it_is_approved() -> None:
    site = MockSite(
        {
            "https://www.site.test/robots.txt": (404, {}, ""),
            "https://www.site.test/": html("https://site.test/apex-page", "/ok"),
            "https://www.site.test/ok": html(),
            "https://site.test/apex-page": html(),
        }
    )
    config = make_config(base_url="https://www.site.test/")
    await crawl(site, config)
    assert not any(url.startswith("https://site.test") for url in site.urls())


async def test_a_redirect_from_www_to_the_apex_is_refused_not_followed() -> None:
    site = MockSite(
        {
            "https://www.site.test/": redirect("https://site.test/"),
            "https://site.test/": html(),
        }
    )
    records, _ = await crawl(site, make_config(base_url="https://www.site.test/"))
    assert not any(url.startswith("https://site.test") for url in site.urls())
    assert issues(records, IssueCode.REDIRECT_OUT_OF_SCOPE)


# --------------------------------------------------------------------------- #
# Back-off: 429 / 403 / 5xx / Retry-After never hammer the site
# --------------------------------------------------------------------------- #
async def test_the_crawl_aborts_after_consecutive_blocks_and_stops_asking() -> None:
    routes = {u("/"): html(*[f"/p{i}" for i in range(10)])}
    routes.update({u(f"/p{i}"): (429, {}, "slow down") for i in range(10)})
    site = MockSite(routes)
    config = make_config(max_consecutive_blocks=3, concurrency=1)
    records, _ = await crawl(site, config)
    requested = [p for p in _paths(site) if p.startswith("/p")]
    assert len(requested) == 3, "exactly the allowed number of blocked answers, then silence"
    aborted = [i for i in issues(records, IssueCode.HTTP_ERROR) if "aborted" in i.message]
    assert aborted and "consecutive" in aborted[0].message


@pytest.mark.parametrize("status", [403, 500, 502, 503])
async def test_403_and_5xx_count_as_blocks(status: int) -> None:
    routes = {u("/"): html(*[f"/p{i}" for i in range(6)])}
    routes.update({u(f"/p{i}"): (status, {}, "no") for i in range(6)})
    site = MockSite(routes)
    await crawl(site, make_config(max_consecutive_blocks=2, concurrency=1))
    assert len([p for p in _paths(site) if p.startswith("/p")]) == 2


async def test_a_success_resets_the_block_counter() -> None:
    routes = {
        u("/"): html("/a", "/b", "/c", "/d", "/e"),
        u("/a"): (429, {}, ""),
        u("/b"): (429, {}, ""),
        u("/c"): html(),
        u("/d"): (429, {}, ""),
        u("/e"): (429, {}, ""),
    }
    site = MockSite(routes)
    records, _ = await crawl(site, make_config(max_consecutive_blocks=3, concurrency=1))
    assert len([p for p in _paths(site) if p != "/"]) == 5, "never three in a row"
    assert not [i for i in issues(records) if "aborted" in i.message]


async def test_retry_after_pauses_every_later_request() -> None:
    fake = FakeTime()
    site = MockSite(
        {
            u("/"): html("/a", "/b"),
            u("/a"): (429, {"retry-after": "30"}, ""),
            u("/b"): html(),
        },
        time=fake,
    )
    config = make_config(
        delay_s=1.0, concurrency=1, max_consecutive_blocks=5, max_retry_after_s=60.0
    )
    await crawl(site, config, fake)
    starts = {url.replace("https://site.test", ""): at for at, url in site.starts}
    assert starts["/b"] - starts["/a"] >= 30.0, "the next request waited out Retry-After"


async def test_a_retry_after_beyond_the_cap_aborts_the_crawl() -> None:
    site = MockSite(
        {u("/"): html("/a", "/b"), u("/a"): (503, {"retry-after": "3600"}, ""), u("/b"): html()}
    )
    config = make_config(concurrency=1, max_consecutive_blocks=5, max_retry_after_s=120.0)
    records, _ = await crawl(site, config)
    assert "/b" not in _paths(site)
    assert [i for i in issues(records, IssueCode.HTTP_ERROR) if "Retry-After" in i.message]


async def test_an_http_date_retry_after_is_understood() -> None:
    fake = FakeTime()
    when = datetime(2026, 10, 13, 12, 0, 20, tzinfo=UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
    site = MockSite(
        {
            u("/"): html("/a", "/b"),
            u("/a"): (429, {"retry-after": when}, ""),
            u("/b"): html(),
        },
        time=fake,
    )
    config = make_config(delay_s=0.0, concurrency=1, max_consecutive_blocks=5, max_retry_after_s=60)
    await crawl(site, config, fake)
    starts = {url.replace("https://site.test", ""): at for at, url in site.starts}
    assert starts["/b"] - starts["/a"] >= 20.0


async def test_pages_are_never_retried() -> None:
    site = MockSite({u("/"): html("/a"), u("/a"): (503, {}, "")})
    await crawl(site, make_config(max_consecutive_blocks=5))
    assert _paths(site).count("/a") == 1
