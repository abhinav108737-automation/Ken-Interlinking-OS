"""Phase 6 operator commands without a database: ``deployments-reconcile``, ``jobs-reap``,
``cms-sim-seed`` / ``cms-sim-serve`` argument parsing, output shape, the pure seed planner,
page rendering, and the simulated site's binding rules and routes over a fake page source
(a real loopback socket on an OS-assigned port; never 8765).
"""

from __future__ import annotations

import json
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from ken_contracts.cms import CMSPage
from ken_core.providers.cms.simulated import stable_page_id
from ken_services.deployment import cli as deploy_cli
from ken_services.deployment import cms_sim, sim_site
from ken_services.deployment.service import ReconcileSummary
from ken_services.jobs import reaper
from tests.conftest import make_settings

SECRET_URL = "postgresql+asyncpg://ken:hunter2@127.0.0.1:5433/ken_interlink_test"


def _no_secrets(text: str) -> None:
    assert "hunter2" not in text
    assert "postgresql" not in text


# --------------------------------------------------------------------------- #
# deployments-reconcile
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", ["-1", "abc", "nan", "inf", str(31 * 24 * 3600)])
def test_reconcile_rejects_bad_older_than(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert deploy_cli.main(["reconcile", "--older-than-s", value]) == deploy_cli.EXIT_USAGE


def test_reconcile_requires_a_command() -> None:
    assert deploy_cli.main([]) == deploy_cli.EXIT_USAGE


def test_reconcile_summary_line_shape() -> None:
    line = deploy_cli.summary_line(ReconcileSummary(examined=2, applied=1), cms_provider="mock")
    data = json.loads(line)
    assert data == {
        "command": "reconcile",
        "cms_provider": "mock",
        "examined": 2,
        "applied": 1,
        "failed": 0,
        "conflict": 0,
        "rolled_back": 0,
        "reverted": 0,
        "still_unknown": 0,
        "skipped_locked": 0,
    }
    assert deploy_cli.exit_code_for(ReconcileSummary(examined=2)) == deploy_cli.EXIT_OK
    assert deploy_cli.exit_code_for(ReconcileSummary(still_unknown=1)) == deploy_cli.EXIT_RUN_ERRORS


class _FakeCMS:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class _FakeService:
    def __init__(self, outcome: ReconcileSummary | BaseException) -> None:
        self.outcome = outcome
        self.calls: list[float | None] = []

    async def reconcile(self, *, older_than_s: float | None = None) -> ReconcileSummary:
        self.calls.append(older_than_s)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def reconcile_fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"cms": _FakeCMS(), "service": _FakeService(ReconcileSummary())}
    settings = make_settings(database_url=SECRET_URL, cms_provider="simulated")
    monkeypatch.setattr(deploy_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(deploy_cli, "build_cms_provider", lambda s: state["cms"])

    def build(**kwargs: Any) -> _FakeService:
        state["kwargs"] = kwargs
        service: _FakeService = state["service"]
        return service

    monkeypatch.setattr(deploy_cli, "build_deployment_service", build)
    return state


def test_reconcile_runs_through_the_composed_service(
    reconcile_fakes: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    reconcile_fakes["service"] = _FakeService(ReconcileSummary(examined=3, applied=2, failed=1))
    assert deploy_cli.main(["reconcile", "--older-than-s", "60"]) == deploy_cli.EXIT_OK
    out = capsys.readouterr()
    data = json.loads(out.out.strip())
    assert data["examined"] == 3 and data["applied"] == 2 and data["failed"] == 1
    assert reconcile_fakes["service"].calls == [60.0]
    assert reconcile_fakes["cms"].closed == 1
    assert set(reconcile_fakes["kwargs"]) == {"sessionmaker", "settings", "cms"}
    _no_secrets(out.out + out.err)


def test_reconcile_default_horizon_is_the_setting(reconcile_fakes: dict[str, Any]) -> None:
    assert deploy_cli.main(["reconcile"]) == deploy_cli.EXIT_OK
    assert reconcile_fakes["service"].calls == [None]  # the service applies the setting


def test_reconcile_failure_prints_a_class_name_only(
    reconcile_fakes: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    reconcile_fakes["service"] = _FakeService(OSError(f"connect {SECRET_URL} failed"))
    assert deploy_cli.main(["reconcile"]) == deploy_cli.EXIT_RUN_ERRORS
    out = capsys.readouterr()
    assert "OSError" in out.err
    assert out.out == ""
    _no_secrets(out.out + out.err)
    assert reconcile_fakes["cms"].closed == 1


def test_reconcile_is_refused_for_the_mock_provider(
    reconcile_fakes: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fresh in-process mock knows none of the stored operations: resending to it would
    record fictitious outcomes. Refused before any provider, engine or service is built."""
    settings = make_settings(database_url=SECRET_URL, cms_provider="mock")
    monkeypatch.setattr(deploy_cli, "get_settings", lambda: settings)

    def no_provider(_settings: object) -> object:
        raise AssertionError("no CMS provider may be built for a refused reconcile")

    def no_engine(_settings: object) -> object:
        raise AssertionError("no database engine may be built for a refused reconcile")

    monkeypatch.setattr(deploy_cli, "build_cms_provider", no_provider)
    monkeypatch.setattr(deploy_cli, "create_engine", no_engine)
    assert deploy_cli.main(["reconcile", "--older-than-s", "0"]) == deploy_cli.EXIT_USAGE
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.strip() == deploy_cli.MOCK_PROVIDER_REFUSAL
    assert reconcile_fakes["service"].calls == []
    assert "kwargs" not in reconcile_fakes
    _no_secrets(out.out + out.err)


def test_reconcile_unknown_outcome_exits_one(
    reconcile_fakes: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    reconcile_fakes["service"] = _FakeService(ReconcileSummary(examined=1, still_unknown=1))
    assert deploy_cli.main(["reconcile"]) == deploy_cli.EXIT_RUN_ERRORS
    assert json.loads(capsys.readouterr().out)["still_unknown"] == 1


# --------------------------------------------------------------------------- #
# jobs-reap
# --------------------------------------------------------------------------- #
def test_reaper_summary_line_shape() -> None:
    summary = reaper.ReapSummary(
        kinds_examined=2, runs_reaped=3, reaped={"analysis": 3}, skipped_live=["agent"]
    )
    assert json.loads(reaper.summary_line(summary)) == {
        "command": "jobs-reap",
        "kinds_examined": 2,
        "runs_reaped": 3,
        "reaped": {"analysis": 3},
        "skipped_live": ["agent"],
    }


def test_reaper_arguments() -> None:
    assert reaper.main(["--help"]) == reaper.EXIT_OK
    assert reaper.main(["--bogus"]) == reaper.EXIT_USAGE


def test_reaper_main_prints_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(database_url=SECRET_URL)
    monkeypatch.setattr(reaper, "get_settings", lambda: settings)

    async def fake_reap(sessionmaker: Any, **kwargs: Any) -> reaper.ReapSummary:
        return reaper.ReapSummary(kinds_examined=1, runs_reaped=1, reaped={"analysis": 1})

    monkeypatch.setattr(reaper, "reap_stale_runs", fake_reap)
    assert reaper.main([]) == reaper.EXIT_OK
    out = capsys.readouterr()
    assert json.loads(out.out)["runs_reaped"] == 1
    _no_secrets(out.out + out.err)


def test_reaper_failure_prints_a_class_name_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(database_url=SECRET_URL)
    monkeypatch.setattr(reaper, "get_settings", lambda: settings)

    async def boom(sessionmaker: Any, **kwargs: Any) -> reaper.ReapSummary:
        raise ConnectionError(SECRET_URL)

    monkeypatch.setattr(reaper, "reap_stale_runs", boom)
    assert reaper.main([]) == reaper.EXIT_RUN_ERRORS
    out = capsys.readouterr()
    assert "ConnectionError" in out.err
    _no_secrets(out.out + out.err)


def test_reaper_reason_is_fixed_text() -> None:
    assert reaper.REAPED_REASON == "reaped: no live runner held the lock"


# --------------------------------------------------------------------------- #
# cms-sim: arguments, output, the pure seed planner
# --------------------------------------------------------------------------- #
def test_cms_sim_arguments() -> None:
    assert cms_sim.main([]) == cms_sim.EXIT_USAGE
    assert cms_sim.main(["bogus"]) == cms_sim.EXIT_USAGE
    assert cms_sim.main(["seed", "--help"]) == cms_sim.EXIT_OK
    args = cms_sim._parser().parse_args(["seed", "--export", "x.jsonl", "--reset"])
    assert args.export == Path("x.jsonl") and args.reset is True
    args = cms_sim._parser().parse_args(["seed"])
    assert args.export is None and args.reset is False


def test_cms_sim_refuses_an_unsafe_database_before_any_io(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for url in (
        "postgresql+asyncpg://ken:hunter2@127.0.0.1:5432/ken_cms_sim",  # the developer's server
        "postgresql+asyncpg://ken:hunter2@127.0.0.1:5433/ken_interlink",  # the app database
        "postgresql+asyncpg://ken:hunter2@db.example.com:5433/ken_cms_sim",  # not local
    ):
        settings = make_settings(cms_sim_database_url=url)
        monkeypatch.setattr(cms_sim, "get_settings", lambda s=settings: s)
        assert cms_sim.main(["seed"]) == cms_sim.EXIT_USAGE
        assert cms_sim.main(["serve"]) == cms_sim.EXIT_USAGE
        out = capsys.readouterr()
        _no_secrets(out.out + out.err)


def test_cms_sim_missing_export_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings()
    monkeypatch.setattr(cms_sim, "get_settings", lambda: settings)
    missing = tmp_path / "nope.jsonl"
    assert cms_sim.main(["seed", "--export", str(missing)]) == cms_sim.EXIT_USAGE
    assert "--export" in capsys.readouterr().err


def test_seed_line_shape() -> None:
    data = json.loads(cms_sim.seed_line(cms_sim.SeedSummary(inventory_pages=4, seeded=3)))
    assert data == {
        "command": "cms-sim-seed",
        "database_created": False,
        "reset": False,
        "inventory_pages": 4,
        "seeded": 3,
        "already_present": 0,
        "from_export": 0,
        "skipped_conflict": 0,
    }


def _inv(
    path: str,
    *,
    external_id: str | None = None,
    source_type: str = "file_export",
    url: str | None = None,
    body: str = "<p>stored</p>",
) -> cms_sim.InventoryPage:
    canonical = f"https://site.test{path}"
    return cms_sim.InventoryPage(
        page_id=uuid.uuid4(),
        canonical_url=canonical,
        external_id=external_id,
        source_type=source_type,
        url=url or canonical,
        title=f"Title {path}",
        body_html=body,
        published_at=None,
    )


def test_cms_page_id_matches_the_deployment_service_resolution() -> None:
    cms_page = _inv("/a", external_id="cms-7", source_type="cms")
    exported = _inv("/b", external_id="exp-9", source_type="file_export")  # NOT a CMS id
    crawled = _inv("/c", source_type="public_web")
    blank = _inv("/d", external_id="", source_type="cms")
    assert cms_sim.cms_page_id_for(cms_page) == "cms-7"
    assert cms_sim.cms_page_id_for(exported) == stable_page_id("https://site.test/b")
    assert cms_sim.cms_page_id_for(crawled) == stable_page_id("https://site.test/c")
    assert cms_sim.cms_page_id_for(blank) == stable_page_id("https://site.test/d")


def test_build_sim_pages_prefers_the_original_export_body() -> None:
    alias = _inv("/a", url="https://site.test/a-old")  # fetched at an alias URL
    plain = _inv("/b")
    bodies = {
        "https://site.test/a-old": "<html><body><main><p>orig a</p></main></body></html>",
        "https://site.test/b": "<p>orig b</p>",
    }
    plan = cms_sim.build_sim_pages([plain, alias, _inv("/c")], export_bodies=bodies)
    by_url = {p.url: p for p in plan.pages}
    assert plan.from_export == 2 and plan.skipped_conflict == 0
    assert by_url["https://site.test/a"].body_html == bodies["https://site.test/a-old"]
    assert by_url["https://site.test/b"].body_html == "<p>orig b</p>"
    assert by_url["https://site.test/c"].body_html == "<p>stored</p>"
    for page in plan.pages:
        assert page.canonical_url == page.url  # resolve_page_id(url=canonical) finds it
        assert page.title.startswith("Title ")
        assert page.version.startswith("sim-seed-")
        assert page.protected is False


def test_build_sim_pages_skips_collisions() -> None:
    existing = [
        CMSPage(
            page_id="other",
            url="https://site.test/taken",
            title="t",
            body_html="<p>x</p>",
            version="v1",
        )
    ]
    one = _inv("/x", external_id="dup", source_type="cms")
    two = _inv("/y", external_id="dup", source_type="cms")  # same CMS id: second is skipped
    taken = _inv("/taken")  # url owned by a different existing page
    plan = cms_sim.build_sim_pages([one, two, taken], existing=existing)
    assert [p.page_id for p in plan.pages] == ["dup"]
    assert plan.skipped_conflict == 2


def test_build_sim_pages_keeps_an_existing_page_with_the_same_id() -> None:
    page = _inv("/a")
    existing = [
        CMSPage(
            page_id=stable_page_id(page.canonical_url),
            url=page.canonical_url,
            title="t",
            body_html="<p>deployed</p>",
            version="v9",
        )
    ]
    plan = cms_sim.build_sim_pages([page], existing=existing)
    assert len(plan.pages) == 1 and plan.skipped_conflict == 0  # upsert keeps the stored one


# --------------------------------------------------------------------------- #
# Simulated site: rendering, binding rules, routes (fake source, real loopback socket)
# --------------------------------------------------------------------------- #
def _page(path: str, body: str, *, title: str = "A <b> & title") -> CMSPage:
    return CMSPage(
        page_id=f"p{path}",
        url=f"https://site.test{path}",
        canonical_url=f"https://site.test{path}",
        title=title,
        body_html=body,
        version="v1",
        updated_at=datetime(2026, 10, 15, tzinfo=UTC),
    )


def test_render_page_wraps_a_fragment() -> None:
    rendered = sim_site.render_page(_page('/q?x="1"', "<main><p>Hi</p></main>"))
    assert rendered.startswith("<!DOCTYPE html>")
    assert "<title>A &lt;b&gt; &amp; title</title>" in rendered
    assert '<link rel="canonical" href="https://site.test/q?x=&quot;1&quot;">' in rendered
    assert "<body>\n<main><p>Hi</p></main>\n</body>" in rendered


def test_render_page_serves_a_full_document_verbatim() -> None:
    body = '<!doctype html><HTML lang="en"><head></head><body><p>x</p></body></HTML>'
    assert sim_site.render_page(_page("/a", body)) == body


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "localhost", "192.168.1.10", "::", "example.com"],  # noqa: S104 - refused
)
def test_site_refuses_non_loopback_hosts(host: str) -> None:
    with pytest.raises(sim_site.SimSiteError):
        sim_site.SimulatedSite(_FakeSource([]), host=host)


class _FakeSocket:
    """Pretends the OS assigned a forbidden port."""

    instances: list[_FakeSocket] = []  # noqa: RUF012 - test bookkeeping

    def __init__(self, family: int, kind: int) -> None:
        self.closed = False
        self.port = 8765
        _FakeSocket.instances.append(self)

    def bind(self, address: tuple[str, int]) -> None:
        assert address[1] == 0  # never a fixed port

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", self.port)

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("port", [8765, 0])
def test_bind_refuses_forbidden_ports(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    _FakeSocket.instances.clear()
    monkeypatch.setattr(socket, "socket", _FakeSocket)  # what sim_site binds with
    monkeypatch.setattr(_FakeSocket, "getsockname", lambda self: ("127.0.0.1", port))
    with pytest.raises(sim_site.SimSiteError, match=str(port)):
        sim_site._bind(sim_site._loopback("127.0.0.1"))
    assert _FakeSocket.instances and _FakeSocket.instances[0].closed


class _FakeSource:
    def __init__(self, pages: list[CMSPage], *, broken: bool = False) -> None:
        self.pages = {sim_site._path_of(p.url): p for p in pages}
        self.broken = broken

    async def page_by_path(self, path: str) -> CMSPage | None:
        if self.broken:
            raise ConnectionError("db down")
        return self.pages.get(path)

    async def list_pages(self) -> list[CMSPage]:
        return list(self.pages.values())


async def test_site_serves_pages_by_path() -> None:
    source = _FakeSource(
        [
            _page("/reports/a", "<main><p>Alpha</p></main>", title="Alpha"),
            _page("/q?id=7", "<p>Query page</p>", title="Q"),
        ]
    )
    async with sim_site.serve_simulated_site(source) as base_url:
        port = int(base_url.rsplit(":", 1)[1])
        assert base_url == f"http://127.0.0.1:{port}"
        assert port not in (0, 8765)
        async with httpx.AsyncClient(base_url=base_url, trust_env=False) as client:
            page = await client.get("/reports/a")
            assert page.status_code == 200
            assert page.headers["content-type"] == "text/html; charset=utf-8"
            assert "<p>Alpha</p>" in page.text
            assert '<link rel="canonical" href="https://site.test/reports/a">' in page.text
            assert (await client.get("/q?id=7")).status_code == 200
            assert (await client.get("/q?id=8")).status_code == 404
            assert (await client.get("/missing")).status_code == 404
            robots = await client.get("/robots.txt")
            assert robots.status_code == 200 and robots.text == "User-agent: *\nAllow: /\n"
            head = await client.head("/reports/a")
            assert head.status_code == 200 and head.content == b""
            assert int(head.headers["content-length"]) == len(page.content)
            post = await client.post("/reports/a", content=b"x")
            assert post.status_code == 405 and post.headers["allow"] == "GET, HEAD"
            index = await client.get("/")  # no page at "/": an index for crawl discovery
            assert index.status_code == 200
            assert 'href="/reports/a"' in index.text and 'href="/q?id=7"' in index.text
            assert 'content="noindex, follow"' in index.text
    with pytest.raises(httpx.ConnectError):  # stopped: nothing listens any more
        async with httpx.AsyncClient(trust_env=False) as client:
            await client.get(base_url + "/robots.txt")


async def test_site_storage_error_is_a_503() -> None:
    async with (
        sim_site.serve_simulated_site(_FakeSource([], broken=True)) as base_url,
        httpx.AsyncClient(base_url=base_url, trust_env=False) as client,
    ):
        response = await client.get("/a")
        assert response.status_code == 503
        assert "db down" not in response.text


async def test_listener_verification_requires_this_instances_nonce() -> None:
    site = sim_site.SimulatedSite(_FakeSource([]))
    base_url = await site.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            health = await client.get(base_url + sim_site.HEALTH_PATH)
        assert health.text == site._nonce and len(site._nonce) == 32
        site._nonce = "0" * 32  # another server would not know this instance's nonce
        with pytest.raises(sim_site.SimSiteError, match="not this server"):
            await site._verify()
    finally:
        await site.stop()
    assert site.port is None


async def test_site_start_fails_cleanly_when_verification_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def bad_verify(self: sim_site.SimulatedSite) -> None:
        raise sim_site.SimSiteError("the listener answered, but it is not this server")

    monkeypatch.setattr(sim_site.SimulatedSite, "_verify", bad_verify)
    site = sim_site.SimulatedSite(_FakeSource([]))
    with pytest.raises(sim_site.SimSiteError):
        await site.start()
    assert site.port is None and site._task is None


def test_ports_are_os_assigned() -> None:
    # The binding helper never asks for a specific port (0 = any free port).
    sock = sim_site._bind(sim_site._loopback("127.0.0.1"))
    try:
        host, port = sock.getsockname()[:2]
        assert host == "127.0.0.1" and port not in (0, 8765)
        assert sock.type == socket.SOCK_STREAM
    finally:
        sock.close()


async def test_serving_changes_no_global_logging_state() -> None:
    """Regression: uvicorn.Config(log_level=..., access_log=False) re-levels uvicorn.error
    and strips uvicorn.access process-wide, which broke ken_core.logging's bridging."""
    import logging

    names = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")

    def state() -> list[tuple[int, bool, list[logging.Handler]]]:
        return [(lg.level, lg.propagate, list(lg.handlers)) for lg in map(logging.getLogger, names)]

    before = state()
    async with (
        sim_site.serve_simulated_site(_FakeSource([])) as base_url,
        httpx.AsyncClient(trust_env=False) as client,
    ):
        assert (await client.get(base_url + "/robots.txt")).status_code == 200
    assert state() == before
