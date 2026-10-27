"""Sitemap-seeded crawling. Fully offline: ``site.test`` is reserved and the transport is a mock."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from ken_contracts.inventory import CrawlConfig, IssueCode
from ken_services.inventory.pilot import load_pilot, pilot_hash
from ken_services.inventory.sources.public_web import parse_sitemap
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

XML = {"content-type": "application/xml"}
SITEMAP = u("/product-sitemap.xml?p=2")
REPO = Path(__file__).resolve().parents[3]
ROBOTS_OPEN: tuple[int, dict[str, str], str] = (200, {}, "User-agent: *\nDisallow:\n")


def urlset(*locs: str) -> str:
    body = "".join(f"<url><loc>{loc}</loc><lastmod>2026-10-01</lastmod></url>" for loc in locs)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'


def cfg(**kw: Any) -> CrawlConfig:
    return make_config(sitemap_urls=(SITEMAP,), max_depth=0, **kw)


def test_parse_sitemap_handles_cdata_entities_and_index() -> None:
    is_index, locs = parse_sitemap(
        "<urlset><url><loc> https://a.test/x?a=1&amp;b=2 </loc></url>"
        "<url><loc><![CDATA[https://a.test/y]]></loc></url></urlset>"
    )
    assert not is_index
    assert locs == ["https://a.test/x?a=1&b=2", "https://a.test/y"]
    assert parse_sitemap("<sitemapindex><sitemap><loc>https://a.test/s.xml</loc></sitemap>")[0]


async def test_seeds_come_from_the_named_sitemap_and_links_are_not_followed() -> None:
    site = MockSite(
        {
            u("/robots.txt"): ROBOTS_OPEN,
            SITEMAP: (200, XML, urlset(u("/a-market"), u("/b-market"))),
            u("/a-market"): html("/never-followed"),
            u("/b-market"): html(),
            u("/never-followed"): html(),
        }
    )
    records, _ = await crawl(site, cfg())
    assert sorted(d.final_url for d in docs(records)) == [u("/a-market"), u("/b-market")]
    assert u("/") not in site.urls()  # the base URL is not crawled when a sitemap is named
    assert u("/never-followed") not in site.urls()


async def test_only_the_named_sitemap_is_requested() -> None:
    site = MockSite(
        {
            u("/robots.txt"): (
                200,
                {},
                "User-agent: *\nDisallow:\nSitemap: https://site.test/sitemap.xml\n",
            ),
            SITEMAP: (200, XML, urlset(u("/a-market"))),
            u("/sitemap.xml"): (200, XML, urlset(u("/hidden"))),
            u("/a-market"): html(),
        }
    )
    await crawl(site, cfg())
    assert u("/sitemap.xml") not in site.urls()
    assert u("/hidden") not in site.urls()


async def test_robots_and_the_rules_still_apply_to_every_listed_url() -> None:
    site = MockSite(
        {
            u("/robots.txt"): (200, {}, "User-agent: *\nDisallow: /private/\n"),
            SITEMAP: (
                200,
                XML,
                urlset(
                    u("/ok-market"),
                    u("/private/x"),
                    u("/login"),
                    u("/file.pdf"),
                    "https://evil.test/off-host",
                    "https://www.site.test/apex-twin",
                ),
            ),
            u("/ok-market"): html(),
        }
    )
    records, _ = await crawl(
        site, cfg(exclude_path_globs=("/login*",), exclude_extensions=(".pdf",))
    )
    assert [d.final_url for d in docs(records)] == [u("/ok-market")]
    assert site.page_urls() == [SITEMAP, u("/ok-market")]


async def test_the_page_budget_caps_how_many_listed_urls_are_fetched() -> None:
    paths = [f"/p{i}-market" for i in range(10)]
    routes: dict[str, Any] = {u("/robots.txt"): ROBOTS_OPEN}
    routes[SITEMAP] = (200, XML, urlset(*(u(p) for p in paths)))
    routes.update({u(p): html() for p in paths})
    site = MockSite(routes)
    records, _ = await crawl(site, cfg(max_pages=3))
    assert len(docs(records)) == 3
    assert len(site.page_urls()) == 1 + 3  # the sitemap plus three pages


async def test_the_sitemap_is_disallowed_by_robots_so_nothing_is_fetched() -> None:
    site = MockSite(
        {
            u("/robots.txt"): (200, {}, "User-agent: *\nDisallow: /product-sitemap.xml\n"),
            SITEMAP: (200, XML, urlset(u("/a-market"))),
            u("/a-market"): html(),
        }
    )
    records, _ = await crawl(site, cfg())
    assert not docs(records)
    assert issues(records, IssueCode.ROBOTS_DISALLOWED)
    assert SITEMAP not in site.urls()


async def test_a_redirecting_sitemap_is_not_followed() -> None:
    site = MockSite({u("/robots.txt"): ROBOTS_OPEN, SITEMAP: redirect("https://evil.test/s.xml")})
    records, _ = await crawl(site, cfg())
    assert not docs(records)
    assert all("evil.test" not in url for url in site.urls())
    assert any("not read" in i.message for i in issues(records))


async def test_a_sitemap_index_is_not_expanded() -> None:
    index = "<sitemapindex><sitemap><loc>https://site.test/child.xml</loc></sitemap></sitemapindex>"
    site = MockSite(
        {
            u("/robots.txt"): ROBOTS_OPEN,
            SITEMAP: (200, XML, index),
            u("/child.xml"): (200, XML, urlset(u("/a-market"))),
        }
    )
    records, _ = await crawl(site, cfg())
    assert not docs(records)
    assert u("/child.xml") not in site.urls()
    assert any("INDEX" in i.message for i in issues(records))


async def test_an_oversized_or_non_xml_sitemap_is_refused() -> None:
    big = MockSite(
        {
            u("/robots.txt"): ROBOTS_OPEN,
            SITEMAP: (200, XML, urlset(*(u(f"/p{i}-market") for i in range(200)))),
        }
    )
    records, _ = await crawl(big, cfg(sitemap_max_bytes=1024))
    assert not docs(records)
    assert issues(records, IssueCode.TOO_LARGE)
    wrong = MockSite(
        {
            u("/robots.txt"): ROBOTS_OPEN,
            SITEMAP: (200, {"content-type": "text/html"}, "<html></html>"),
        }
    )
    records, _ = await crawl(wrong, cfg())
    assert not docs(records)
    assert issues(records, IssueCode.NON_HTML)


async def test_the_sitemap_fetch_honours_the_request_spacing() -> None:
    fake = FakeTime()
    site = MockSite(
        {
            u("/robots.txt"): ROBOTS_OPEN,
            SITEMAP: (200, XML, urlset(u("/a-market"))),
            u("/a-market"): html(),
        },
        time=fake,
    )
    await crawl(site, cfg(delay_s=3.0), fake)
    times = [t for t, _ in site.starts]
    assert len(times) == 3
    assert all(b - a >= 3.0 for a, b in itertools.pairwise(times))


async def test_a_blocked_sitemap_stops_the_run_and_nothing_else_is_asked() -> None:
    site = MockSite({u("/robots.txt"): ROBOTS_OPEN, SITEMAP: (403, {}, "no")})
    records, _ = await crawl(site, cfg())
    assert not docs(records)
    assert site.page_urls() == [SITEMAP]


def test_the_real_sitemap_pilot_config_names_only_the_requested_sitemap() -> None:
    config = load_pilot(REPO / "pilots" / "kenresearch" / "pilot-sitemap-25.yaml")
    assert config.site.sitemap_sources == ("https://www.kenresearch.com/product-sitemap.xml?p=2",)
    assert config.limits.max_depth == 0
    assert config.limits.max_pages == 25
    assert config.limits.delay_s >= 3.0
    assert config.politeness.render == "browser"
    assert config.crawl_config("bot@example.invalid").sitemap_urls == config.site.sitemap_sources
    other = load_pilot(REPO / "pilots" / "kenresearch" / "pilot-25.yaml")
    assert pilot_hash(config) != pilot_hash(other)  # one approval cannot cover the other
