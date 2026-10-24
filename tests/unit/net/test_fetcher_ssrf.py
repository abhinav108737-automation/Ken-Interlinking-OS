"""SSRF guard wired into the crawler and the verification probe (loopback fixtures, port 0).

No real DNS: the guard's resolver is injected; for the rebinding case the OS-level lookup httpx
performs is patched to point a public-looking name at the loopback fixture.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from ken_contracts.inventory import (
    CrawlConfig,
    IssueCode,
    SourceDocument,
    SourceIssue,
)
from ken_core.net import NetworkGuard
from ken_services.deployment.http_probe import FetchOutcome, HttpProbe
from ken_services.inventory.sources.public_web import PublicWebContentSource
from tests.conftest import make_settings
from tests.support.local_site import HTML, LocalSite, RouteSpec, html_page

UA = "KenInterlinkBot/0.1 (+https://example.invalid/bot)"
PUBLIC = "93.184.216.34"
type Record = SourceDocument | SourceIssue


def _config(base: str, *hosts: str) -> CrawlConfig:
    return CrawlConfig(
        base_url=base,
        allowed_hosts=hosts,
        user_agent=UA,
        delay_s=0.0,
        concurrency=1,
        max_pages=10,
        max_depth=2,
        timeout_s=2.0,
        max_bytes=10_000,
        max_redirects=3,
    )


async def _crawl(source: PublicWebContentSource) -> list[Record]:
    try:
        await source.site_scope()
        return [r async for r in source.iter_records()]
    finally:
        await source.aclose()


def _documents(records: list[Record]) -> list[SourceDocument]:
    return [r for r in records if isinstance(r, SourceDocument)]


def _refusals(records: list[Record]) -> list[SourceIssue]:
    return [
        r
        for r in records
        if isinstance(r, SourceIssue)
        and r.code in {IssueCode.OUT_OF_SCOPE, IssueCode.REDIRECT_OUT_OF_SCOPE}
    ]


def _rebind(monkeypatch: pytest.MonkeyPatch, name: str, ip: str) -> None:
    """Make the OS-level lookup of ``name`` (what httpx connects with) return ``ip``."""
    real = socket.getaddrinfo

    def patched(host: Any, *args: Any, **kwargs: Any) -> Any:
        text = host.decode() if isinstance(host, bytes) else host
        return real(ip if text == name else host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", patched)


@pytest.fixture
def fixture_site() -> Any:
    routes: dict[str, RouteSpec] = {
        "/robots.txt": (404, {}, ""),
        "/": (200, HTML, html_page("/a")),
        "/a": (200, HTML, html_page()),
        "/to-metadata": (302, {"Location": "http://169.254.169.254/latest/meta-data/"}, ""),
        "/to-loopback": (302, {"Location": "http://127.0.0.1:9/x"}, ""),
        "/to-private-name": (302, {"Location": "http://intranet.test/x"}, ""),
        "/to-other": (302, {"Location": "http://elsewhere.test/x"}, ""),
        "/hop": (302, {"Location": "/a"}, ""),
    }
    with LocalSite(routes) as site:
        yield site


# --------------------------------------------------------------- escape hatch
async def test_loopback_fixture_is_refused_without_the_escape_hatch(
    fixture_site: LocalSite,
) -> None:
    records = await _crawl(PublicWebContentSource(_config(fixture_site.base_url)))
    assert _documents(records) == []
    # robots.txt cannot be fetched, so the origin is treated as disallowed (fail closed)
    assert (
        next(r.code for r in records if isinstance(r, SourceIssue)) is IssueCode.ROBOTS_UNAVAILABLE
    )
    assert fixture_site.targets() == []  # nothing was ever sent


async def test_loopback_fixture_is_allowed_with_the_escape_hatch(fixture_site: LocalSite) -> None:
    source = PublicWebContentSource(
        _config(fixture_site.base_url),
        clock=lambda: datetime(2026, 10, 13, tzinfo=UTC),
        allow_private_networks=True,
    )
    records = await _crawl(source)
    assert [d.final_url for d in _documents(records)] == [
        fixture_site.url("/"),
        fixture_site.url("/a"),
    ]


# ----------------------------------------------------------------- redirects
async def _redirect_crawl(
    fixture_site: LocalSite,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    *hosts: str,
    table: dict[str, list[str]],
) -> list[Record]:
    """Start at a public-LOOKING name that connects to the fixture, then hit ``route``."""
    port = fixture_site.netloc.rsplit(":", 1)[1]
    _rebind(monkeypatch, "start.test", "127.0.0.1")

    async def resolver(host: str, _port: int | None) -> list[str]:
        return table[host]

    class NoConnectedCheck(NetworkGuard):
        # the connected-address rule is covered separately; here the first hop reaches
        # loopback by construction, and the hops AFTER it are what is under test.
        def check_response(self, response: Any) -> None:
            return None

    guard = NoConnectedCheck(resolver=resolver)
    config = _config(f"http://start.test:{port}{route}", *hosts)
    return await _crawl(PublicWebContentSource(config, guard=guard))


@pytest.mark.parametrize(
    ("route", "extra_host", "table"),
    [
        ("/to-metadata", "169.254.169.254", {"start.test": [PUBLIC]}),
        ("/to-loopback", "127.0.0.1:9", {"start.test": [PUBLIC]}),
        (
            "/to-private-name",
            "intranet.test",
            {"start.test": [PUBLIC], "intranet.test": ["10.0.0.5"]},
        ),
    ],
    ids=["metadata-ip", "loopback-ip", "private-name"],
)
async def test_redirect_to_a_non_public_address_is_refused(
    fixture_site: LocalSite,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    extra_host: str,
    table: dict[str, list[str]],
) -> None:
    # The target host is IN SCOPE (listed), so only the network guard can stop it.
    records = await _redirect_crawl(fixture_site, monkeypatch, route, extra_host, table=table)
    assert _documents(records) == []
    refusals = _refusals(records)
    assert [r.code for r in refusals] == [IssueCode.REDIRECT_OUT_OF_SCOPE]
    assert "network guard (non_public_address)" in refusals[0].message
    assert fixture_site.targets() == ["/robots.txt", route]  # the target was never requested


async def test_redirect_to_a_different_disallowed_host_is_refused(
    fixture_site: LocalSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = await _redirect_crawl(
        fixture_site,
        monkeypatch,
        "/to-other",
        table={"start.test": [PUBLIC], "elsewhere.test": [PUBLIC]},
    )
    assert _documents(records) == []
    assert [r.code for r in _refusals(records)] == [IssueCode.REDIRECT_OUT_OF_SCOPE]
    assert fixture_site.targets() == ["/robots.txt", "/to-other"]  # elsewhere.test never asked


async def test_redirect_downgrading_https_to_http_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"Location": "http://site.test/plain"})

    source = PublicWebContentSource(
        _config("https://site.test/"), transport=httpx.MockTransport(handler)
    )
    records = await _crawl(source)
    assert _documents(records) == []
    issue = _refusals(records)[0]
    assert issue.code is IssueCode.REDIRECT_OUT_OF_SCOPE
    assert "https to http" in issue.message


# ---------------------------------------------------------------- rebinding
async def test_dns_rebinding_is_caught_by_the_connected_address_check(
    fixture_site: LocalSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard's resolver says PUBLIC (check passes); the socket actually reaches loopback."""
    port = fixture_site.netloc.rsplit(":", 1)[1]
    _rebind(monkeypatch, "rebind.test", "127.0.0.1")

    async def says_public(host: str, _port: int | None) -> list[str]:
        return [PUBLIC]

    guard = NetworkGuard(resolver=says_public)
    await guard.check_url(f"http://rebind.test:{port}/")  # resolve-then-check alone passes

    records = await _crawl(
        PublicWebContentSource(_config(f"http://rebind.test:{port}/"), guard=guard)
    )
    assert _documents(records) == []
    # the request reached the fixture (robots.txt), its response was discarded, and no page
    # was ever requested: the origin is then treated as disallowed (fail closed).
    assert fixture_site.targets().count("/robots.txt") == 1
    assert "/" not in fixture_site.targets()
    assert any(
        isinstance(r, SourceIssue) and r.code is IssueCode.ROBOTS_UNAVAILABLE for r in records
    )

    probe = HttpProbe(base_url=f"http://rebind.test:{port}", timeout_s=2.0, user_agent=UA)
    probe._guard = guard
    result = await probe.fetch(f"http://rebind.test:{port}/")
    assert result.outcome is FetchOutcome.FAILED
    assert result.detail == "unsafe_destination"
    assert result.body is None


# -------------------------------------------------------------------- probe
async def test_probe_refuses_loopback_without_hatch_and_allows_with_it(
    fixture_site: LocalSite,
) -> None:
    base = fixture_site.base_url.rstrip("/")
    strict = HttpProbe(base_url=base, timeout_s=2.0, user_agent=UA)
    result = await strict.fetch(base + "/a")
    assert (result.outcome, result.detail) == (FetchOutcome.FAILED, "unsafe_destination")
    assert fixture_site.targets() == []

    lenient = HttpProbe(base_url=base, timeout_s=2.0, user_agent=UA, allow_private_networks=True)
    ok = await lenient.fetch(base + "/a")
    assert ok.outcome is FetchOutcome.OK


async def test_probe_does_not_follow_redirects_to_metadata(fixture_site: LocalSite) -> None:
    base = fixture_site.base_url.rstrip("/")
    probe = HttpProbe(base_url=base, timeout_s=2.0, user_agent=UA, allow_private_networks=True)
    result = await probe.fetch(base + "/to-metadata")
    assert (result.outcome, result.detail) == (FetchOutcome.FAILED, "redirect")
    assert fixture_site.targets() == ["/to-metadata"]


async def test_probe_target_with_credentials_is_refused_without_echo() -> None:
    probe = HttpProbe(base_url="http://example.test", timeout_s=1.0, user_agent=UA)
    result = await probe.fetch("http://admin:hunter2@example.test/x")
    assert result.outcome is FetchOutcome.FAILED
    assert "hunter2" not in repr(result)


# ----------------------------------------------------------------- settings
def test_escape_hatch_default_is_off() -> None:
    assert make_settings().crawl_allow_private_networks is False


@pytest.mark.parametrize("environment", ["local", "ci"])
def test_escape_hatch_allowed_in_local_and_ci(environment: str) -> None:
    settings = make_settings(
        environment=environment,
        crawl_allow_private_networks=True,
    )
    assert settings.crawl_allow_private_networks is True


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_escape_hatch_refused_in_staging_and_production(environment: str) -> None:
    with pytest.raises(ValueError, match="KEN_CRAWL_ALLOW_PRIVATE_NETWORKS"):
        make_settings(
            environment=environment,
            database_url="postgresql+asyncpg://real:realpw@db.example:5432/ken",
            crawl_allow_private_networks=True,
        )
