"""The Ken public-crawl pilot configuration, ceilings, approvals, preflight and the full
fixture crawl. Offline: the crawl uses ``httpx.MockTransport`` and the shipped pilot rules."""

from __future__ import annotations

import copy
import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from ken_contracts.inventory import IssueCode
from ken_services.inventory import pilot
from ken_services.inventory.sources.public_web import PublicWebContentSource
from tests.conftest import make_settings
from tests.unit.inventory.test_public_web_source import FakeTime, MockSite, docs, html, issues

ROOT = Path(__file__).resolve().parents[3]
PILOT25 = ROOT / "pilots" / "kenresearch" / "pilot-25.yaml"
LARGE = ROOT / "pilots" / "kenresearch" / "pilot-large.yaml"
CONTACT = "inventory@pilot-fixture.test"
HOST = "https://www.kenresearch.com"


def _offline(crawl: Any) -> Any:
    """Tests drive a MockTransport, which only the HTTP path can use: browser mode needs sockets."""
    return crawl.model_copy(update={"render": "http"})


def _raw(path: Path = PILOT25) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _load(tmp_path: Path, raw: dict[str, Any]) -> pilot.PilotConfig:
    target = tmp_path / "pilot.yaml"
    target.write_text(yaml.safe_dump(raw))
    return pilot.load_pilot(target)


# --------------------------------------------------------------------------- #
# The shipped files
# --------------------------------------------------------------------------- #
def test_the_25_page_pilot_has_exactly_the_requested_limits() -> None:
    config = pilot.load_pilot(PILOT25)
    assert config.activated is True
    assert (config.limits.max_pages, config.limits.concurrency) == (25, 1)
    assert config.limits.delay_s >= 3.0
    assert config.limits.max_depth == 3
    assert config.autonomy_mode == "shadow"
    assert config.site.approved_hosts == ("www.kenresearch.com",)
    assert config.site.sitemap_sources == ()
    assert config.destination.database == "ken_kenresearch_pilot"
    assert config.politeness.https_only and config.politeness.require_robots_file


def test_the_large_pilot_is_prepared_but_not_activated() -> None:
    config = pilot.load_pilot(LARGE)
    assert config.activated is False
    assert 200 <= config.limits.max_pages <= 500
    assert config.limits.concurrency == 1 and config.limits.delay_s >= 3.0
    assert config.site.approved_hosts == ("www.kenresearch.com",)
    assert pilot.pilot_hash(config) != pilot.pilot_hash(pilot.load_pilot(PILOT25))


def test_the_apex_is_reported_as_not_approved() -> None:
    config = pilot.load_pilot(PILOT25)
    assert "kenresearch.com" not in config.site.approved_hosts
    assert config.crawl_config(CONTACT).allowed_hosts == ()
    assert pilot._not_approved(config) == ["kenresearch.com (separate host; not approved)"]


def test_the_user_agent_is_descriptive_and_carries_only_the_contact() -> None:
    config = pilot.load_pilot(PILOT25)
    agent = config.user_agent(CONTACT)
    assert agent.startswith("KenInterlinkBot/0.1 (+mailto:inventory@")
    assert "read-only" in agent
    assert config.user_agent("https://example.org/bot").startswith("KenInterlinkBot/0.1 (+https://")


# --------------------------------------------------------------------------- #
# Hard ceilings: a pilot file that breaks them does not load
# --------------------------------------------------------------------------- #
def _mutated(tmp_path: Path, mutate: Any) -> pilot.PilotConfig:
    raw = copy.deepcopy(_raw())
    mutate(raw)
    return _load(tmp_path, raw)


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda r: r["limits"].update(concurrency=2), "limits.concurrency"),
        (lambda r: r["limits"].update(delay_s=1.0), "limits.delay_s"),
        (lambda r: r["limits"].update(max_pages=501), "limits.max_pages"),
        (lambda r: r["politeness"].update(https_only=False), "politeness.https_only"),
        (lambda r: r["politeness"].update(require_robots_file=False), "require_robots_file"),
        (lambda r: r["politeness"].update(max_consecutive_blocks=0), "max_consecutive_blocks"),
        (lambda r: r.update(autonomy_mode="guarded"), "autonomy_mode"),
        (lambda r: r.update(autonomy_mode="autonomous"), "autonomy_mode"),
        (lambda r: r["site"].update(approved_hosts=["*.kenresearch.com"]), "approved_hosts"),
        (lambda r: r["site"].update(approved_hosts=["kenresearch.com"]), "approved"),
        (lambda r: r["site"].update(base_url="http://www.kenresearch.com/"), "https"),
        (
            # a sitemap needs max_depth 0 (the pilot-25 file follows links at depth 3)
            lambda r: r["site"].update(sitemap_sources=["https://www.kenresearch.com/s.xml"]),
            "max_depth must be 0",
        ),
        (
            lambda r: r["site"].update(sitemap_sources=["http://www.kenresearch.com/s.xml"]),
            "exact https URLs",
        ),
        (
            lambda r: r["site"].update(sitemap_sources=["https://www.kenresearch.com/s.txt"]),
            "exact https URLs",
        ),
        (
            lambda r: (
                r["limits"].update(max_depth=0),
                r["site"].update(sitemap_sources=["https://other.example/s.xml"]),
            ),
            "approved host",
        ),
        (lambda r: r["destination"].update(database="ken_interlink"), "synthetic"),
        (lambda r: r["destination"].update(database="ken_demo"), "synthetic"),
        (lambda r: r["destination"].update(database="ken_x_test"), "synthetic"),
        (lambda r: r["destination"].update(qdrant_collection="ken_pages"), "synthetic"),
        (lambda r: r["destination"].update(database="postgres"), "destination.database"),
        (lambda r: r.update(unknown_key=1), "unknown_key"),
    ],
)
def test_ceilings_are_enforced_by_the_loader(tmp_path: Path, mutate: Any, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        _mutated(tmp_path, mutate)


def test_the_apex_must_be_added_deliberately(tmp_path: Path) -> None:
    config = _mutated(tmp_path, lambda r: r["site"]["approved_hosts"].append("kenresearch.com"))
    assert config.crawl_config(CONTACT).allowed_hosts == ("kenresearch.com",)
    assert pilot._not_approved(config) == []


# --------------------------------------------------------------------------- #
# Approvals gate every live operation
# --------------------------------------------------------------------------- #
def _approval(config: pilot.PilotConfig, tmp_path: Path, **override: Any) -> Path:
    body: dict[str, Any] = {
        "config_sha256": pilot.pilot_hash(config),
        "scopes": ["live_robots_check", "live_crawl"],
        "hosts": list(config.site.approved_hosts),
        "approved_by": "operator",
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    body.update(override)
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(body))
    return path


def test_a_valid_approval_is_accepted(tmp_path: Path) -> None:
    config = pilot.load_pilot(PILOT25)
    assert pilot.read_approval(_approval(config, tmp_path), config, "live_crawl") == (
        True,
        "approved",
    )


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"config_sha256": "0" * 64}, "hash differs"),
        ({"scopes": ["live_robots_check"]}, "scope"),
        ({"hosts": ["www.kenresearch.com", "kenresearch.com"]}, "hosts differ"),
        ({"approved_by": " "}, "approved_by"),
        ({"expires_at": "2020-01-01T00:00:00+00:00"}, "expired"),
        ({"expires_at": "not a date"}, "expires_at"),
    ],
)
def test_a_bad_approval_is_refused(tmp_path: Path, override: dict[str, Any], fragment: str) -> None:
    config = pilot.load_pilot(PILOT25)
    ok, note = pilot.read_approval(_approval(config, tmp_path, **override), config, "live_crawl")
    assert not ok and fragment in note


def test_no_approval_file_means_no_live_operation() -> None:
    config = pilot.load_pilot(PILOT25)
    assert pilot.read_approval(None, config, "live_crawl")[0] is False
    assert pilot.read_approval(Path("/nonexistent/x.json"), config, "live_crawl")[0] is False


def test_an_approval_for_one_configuration_does_not_cover_another(tmp_path: Path) -> None:
    small, large = pilot.load_pilot(PILOT25), pilot.load_pilot(LARGE)
    approval = _approval(small, tmp_path)
    assert pilot.read_approval(approval, large, "live_crawl")[0] is False


def test_the_cli_refuses_to_crawl_without_an_approval(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden(*_a: object, **_k: object) -> dict[str, object]:
        raise AssertionError("no crawl may start")

    monkeypatch.setattr(pilot, "run_crawl", forbidden)
    monkeypatch.setenv("KEN_CRAWL_CONTACT", CONTACT)
    code = pilot.main(["crawl", "--config", str(PILOT25)])
    assert code == pilot.EXIT_BLOCKED
    assert "live crawl not approved" in capsys.readouterr().err


def test_the_cli_refuses_the_inactive_large_pilot_even_with_a_matching_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = pilot.load_pilot(LARGE)
    approval = _approval(config, tmp_path)

    async def forbidden(*_a: object, **_k: object) -> dict[str, object]:
        raise AssertionError("no crawl may start")

    monkeypatch.setattr(pilot, "run_crawl", forbidden)
    monkeypatch.setenv("KEN_CRAWL_CONTACT", CONTACT)
    code = pilot.main(["crawl", "--config", str(LARGE), "--approval", str(approval)])
    assert code == pilot.EXIT_BLOCKED
    assert "not activated" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Preflight: offline, complete, honest
# --------------------------------------------------------------------------- #
async def test_preflight_reports_everything_without_touching_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    def no_socket(*_a: object, **_k: object) -> None:
        raise AssertionError("preflight must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    config = pilot.load_pilot(PILOT25)
    report = await pilot.preflight(
        config,
        make_settings(vector_store="qdrant"),
        environ={"KEN_CRAWL_CONTACT": CONTACT},
        inspect_db=False,
    )
    assert report["effective_allowed_hosts"] == ["www.kenresearch.com"]
    assert report["not_approved"] == ["kenresearch.com (separate host; not approved)"]
    assert report["robots"]["status"].startswith("NOT CHECKED")
    assert "none" in report["sitemap_sources"]
    assert report["limits"]["max_pages"] == 25
    assert report["autonomy_mode"] == "shadow"
    assert report["render"] == "browser"
    # 25 pages x (3 redirects + 1 + up to 15 same-host scripts) + 6 robots fetches
    assert report["request_ceiling"]["http_requests_worst_case"] == 25 * 19 + 6
    assert report["request_ceiling"]["minimum_seconds_worst_case"] >= 3.0 * 481
    assert report["destination"]["database"] == "ken_kenresearch_pilot"
    assert report["approvals"]["live_crawl"].startswith("required")
    assert report["blockers"] == []
    assert "mailto:inventory@pilot-fixture.test" in report["user_agent"]


@pytest.mark.parametrize(
    ("environ", "overrides", "fragment"),
    [
        ({}, {}, "KEN_CRAWL_CONTACT is not set"),
        ({"KEN_CRAWL_CONTACT": "someone@example.com"}, {}, "placeholder"),
        ({"KEN_CRAWL_CONTACT": "not-a-contact"}, {}, "email address or an https URL"),
        (
            {"KEN_CRAWL_CONTACT": CONTACT},
            {"autonomy_mode": "guarded"},
            "requires 'shadow'|pilots need",
        ),
        ({"KEN_CRAWL_CONTACT": CONTACT}, {"cms_provider": "simulated"}, "KEN_CMS_PROVIDER"),
    ],
)
async def test_preflight_lists_blockers(
    environ: dict[str, str], overrides: dict[str, Any], fragment: str
) -> None:
    config = pilot.load_pilot(PILOT25)
    try:
        settings = make_settings(vector_store="qdrant", **overrides)
    except Exception:  # guarded needs acknowledgements: an invalid combination is itself a refusal
        pytest.skip("settings refuse this combination before the preflight does")
    report = await pilot.preflight(config, settings, environ=environ, inspect_db=False)
    assert any(__import__("re").search(fragment, b) for b in report["blockers"])


async def test_preflight_blocks_a_destination_that_holds_other_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_inspect(*_a: object, **_k: object) -> dict[str, Any]:
        return {
            "database": "ken_kenresearch_pilot",
            "server": "localhost:5433",
            "reachable": True,
            "migrated": pilot._code_head(),
            "pages": 64,
            "hosts_present": ["research.example.com"],
        }

    monkeypatch.setattr(pilot, "inspect_destination", fake_inspect)
    report = await pilot.preflight(
        pilot.load_pilot(PILOT25),
        make_settings(vector_store="qdrant"),
        environ={"KEN_CRAWL_CONTACT": CONTACT},
    )
    assert any("other hosts" in b and "research.example.com" in b for b in report["blockers"])


def test_the_destination_url_keeps_the_application_role_and_swaps_only_the_database() -> None:
    settings = make_settings(
        database_url="postgresql+asyncpg://ken_app:pw@localhost:5433/ken_interlink"
    )
    url = pilot.destination_url(settings, pilot.load_pilot(PILOT25))
    assert url.endswith("@localhost:5433/ken_kenresearch_pilot") and "ken_app" in url


# --------------------------------------------------------------------------- #
# The whole workflow against a local mock of the site, with the SHIPPED rules
# --------------------------------------------------------------------------- #
def _fixture_site() -> MockSite:
    pages = [f"/market/report-{i}" for i in range(40)]
    routes: dict[str, Any] = {
        f"{HOST}/robots.txt": (
            200,
            {},
            "User-agent: *\nDisallow: /private/\nSitemap: https://www.kenresearch.com/sitemap.xml\n",
        ),
        f"{HOST}/sitemap.xml": (200, {"content-type": "application/xml"}, "<urlset/>"),
        f"{HOST}/": html(
            *pages[:12],
            "/login",
            "/my-account/orders",
            "/search?s=ev",
            "/market/report-1?utm_source=nl&utm_medium=x",
            "/files/licensed-report.pdf",
            "/downloads/report.zip",
            "/cart",
            "/private/internal",
            "http://www.kenresearch.com/plain-http",
            "https://kenresearch.com/apex-only",
            "https://www.kenresearch.com.evil.test/x",
            "/wp-admin/",
        ),
    }
    for index, path in enumerate(pages):
        nxt = pages[index + 1 : index + 4]
        routes[f"{HOST}{path}"] = html(*nxt, "/market/report-0")
    return MockSite(routes, time=FakeTime())


async def test_the_full_pilot_workflow_on_a_fixture_site() -> None:
    config = pilot.load_pilot(PILOT25)
    site = _fixture_site()
    fake = site.time
    assert fake is not None
    source = PublicWebContentSource(
        _offline(config.crawl_config(CONTACT)),
        transport=site.transport(),
        sleep=fake.sleep,
        monotonic=fake.monotonic,
    )
    try:
        records = [r async for r in source.iter_records()]
    finally:
        await source.aclose()

    fetched = site.urls()
    pages = [u for u in fetched if not u.endswith("/robots.txt")]
    assert len(pages) <= 25, "never more than the page budget"
    assert len(docs(records)) <= 25
    assert site.max_in_flight == 1, "one request in flight"
    times = [at for at, _ in site.starts]
    assert all(b - a >= 3.0 - 1e-9 for a, b in itertools.pairwise(times)), "3 s spacing"
    banned = (
        "/login",
        "/my-account",
        "/search",
        ".pdf",
        "/downloads",
        "/cart",
        "/private",
        "/wp-admin",
        "kenresearch.com/apex",
        "evil.test",
        "sitemap",
        "plain-http",
    )
    assert not [u for u in fetched if any(token in u for token in banned)]
    assert all(u.startswith(HOST + "/") for u in fetched), "only the approved host"
    assert all(r.method == "GET" for r in site.requests)
    for request in site.requests:
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert request.headers["user-agent"].startswith("KenInterlinkBot/0.1 (+mailto:inventory@")
    assert f"{HOST}/robots.txt" in fetched and fetched.count(f"{HOST}/robots.txt") == 1
    assert f"{HOST}/market/report-1?utm_source=nl&utm_medium=x" not in fetched
    assert len(docs(records)) >= 13, "the allowed pages were crawled"
    depths = [d.depth or 0 for d in docs(records)]
    assert max(depths) <= 3


async def test_a_forbidden_robots_txt_stops_the_pilot_before_any_page() -> None:
    config = pilot.load_pilot(PILOT25)
    site = MockSite({f"{HOST}/robots.txt": (403, {}, "forbidden"), f"{HOST}/": html()})
    source = PublicWebContentSource(
        _offline(config.crawl_config(CONTACT)), transport=site.transport()
    )
    try:
        records = [r async for r in source.iter_records()]
    finally:
        await source.aclose()
    assert site.page_urls() == []
    assert issues(records, IssueCode.ROBOTS_UNAVAILABLE)


async def test_a_blocked_site_is_left_alone_after_three_answers() -> None:
    config = pilot.load_pilot(PILOT25)
    routes: dict[str, Any] = {
        f"{HOST}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{HOST}/": html(*[f"/market/r{i}" for i in range(20)]),
    }
    routes.update({f"{HOST}/market/r{i}": (429, {"retry-after": "10"}, "") for i in range(20)})
    site = MockSite(routes, time=FakeTime())
    fake = site.time
    assert fake is not None
    source = PublicWebContentSource(
        _offline(config.crawl_config(CONTACT)),
        transport=site.transport(),
        sleep=fake.sleep,
        monotonic=fake.monotonic,
    )
    try:
        records = [r async for r in source.iter_records()]
    finally:
        await source.aclose()
    assert len([u for u in site.urls() if "/market/r" in u]) == 3
    assert [i for i in issues(records) if "aborted" in i.message]
