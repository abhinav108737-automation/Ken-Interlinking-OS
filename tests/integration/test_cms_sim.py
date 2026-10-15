"""``make cms-sim-seed`` / ``make cms-sim-serve`` / ``make deployments-reconcile`` against
PostgreSQL (ADR-0014 s4 / s8).

The simulated CMS here is the DISPOSABLE database ``ken_cms_sim_test`` on the project server
(override: ``KEN_TEST_CMS_SIM_DATABASE_URL``, see ``tests.support.cms_sim_db``; created with
``ensure_database`` and emptied by every test); ``ken_cms_sim`` itself is never used. The
application side is the usual ``*_test`` database (``migrated``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.inventory import SourceType
from ken_core.providers.cms.factory import build_html_engine
from ken_core.providers.cms.simulated import (
    SimulatedCMSProvider,
    ensure_database,
    stable_page_id,
)
from ken_services.deployment import cli as deploy_cli
from ken_services.deployment import cms_sim
from ken_services.deployment.compose import build_deployment_service
from ken_services.deployment.sim_site import SimulatedSite, serve_simulated_site
from tests.conftest import make_settings
from tests.integration import recommendation_support as rs
from tests.integration.conftest import ingest, make_page
from tests.support.cms_sim_db import cms_sim_test_database_url

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SM = async_sessionmaker[AsyncSession]
SIM_URL = cms_sim_test_database_url()
FULL_DOC_PATH = "/reports/full-document"


@pytest.fixture
async def sim() -> AsyncIterator[SimulatedCMSProvider]:
    await ensure_database(SIM_URL)
    provider = SimulatedCMSProvider(database_url=SIM_URL, html_engine=build_html_engine())
    await provider.ensure_schema()
    await provider.reset()
    try:
        yield provider
    finally:
        await provider.reset()
        await provider.aclose()


def _full_document_record() -> dict[str, Any]:
    paragraphs = rs.CORPUS[rs.EV][1]
    body = (
        '<!DOCTYPE html><html lang="en"><head><title>Full</title></head><body>'
        "<header><nav><a href='/'>Home</a></nav></header>"
        + rs.body_html("Full document report", paragraphs)
        + "<footer>(c) site</footer></body></html>"
    )
    return {**rs.record(FULL_DOC_PATH, title="Full document report"), "body_html": body}


async def _seed_inventory(sessionmaker: SM, directory: Path) -> tuple[Path, list[dict[str, Any]]]:
    """Three exported pages, one of them a full document, plus one CMS-sourced page."""
    records = [rs.record(rs.EV), rs.record(rs.LI), _full_document_record()]
    detail = await rs.import_records(sessionmaker, directory, records, name="export.jsonl")
    assert detail.counters.pages_created == 3
    await ingest(
        sessionmaker,
        make_page(
            "/cms-page",
            body="Managed in the CMS",
            external_id="cms-42",
            source_type=SourceType.CMS,
        ),
    )
    return directory / "export.jsonl", records


async def _inventory(sessionmaker: SM) -> dict[str, Any]:
    rows = await rs.rows(
        sessionmaker,
        "SELECT p.id, p.canonical_url, v.body_html, v.title, v.source_type FROM pages p "
        "JOIN page_versions v ON v.page_id = p.id AND v.version = p.current_version",
    )
    return {row.canonical_url: row for row in rows}


async def test_seed_from_the_inventory_with_the_original_export(
    sessionmaker: SM, sim: SimulatedCMSProvider, tmp_path: Path
) -> None:
    export, records = await _seed_inventory(sessionmaker, tmp_path)
    inventory = await _inventory(sessionmaker)
    bodies = await cms_sim.load_export_bodies(export, max_bytes=10_000_000)
    assert len(bodies) == 3

    summary = await cms_sim.seed_simulated_cms(
        sessionmaker=sessionmaker, provider=sim, export_bodies=bodies
    )

    assert summary.inventory_pages == 4
    assert summary.seeded == 4 and summary.already_present == 0
    assert summary.from_export == 3 and summary.skipped_conflict == 0
    pages = {p.url: p for p in await sim.list_pages()}
    assert set(pages) == set(inventory)
    for record in records:
        page = pages[record["url"]]
        assert page.page_id == stable_page_id(record["url"])
        assert page.body_html == record["body_html"]  # the ORIGINAL, not the stored root
        assert page.canonical_url == page.url
        assert page.title == inventory[record["url"]].title
    cms_page = pages[rs.SITE + "/cms-page"]
    assert cms_page.page_id == "cms-42"  # external_id of a CMS-sourced page
    assert cms_page.body_html == inventory[rs.SITE + "/cms-page"].body_html  # no export match
    assert inventory[rs.SITE + "/cms-page"].source_type == "cms"


async def test_seed_agrees_with_the_deployment_service_page_resolution(
    sessionmaker: SM, sim: SimulatedCMSProvider, tmp_path: Path
) -> None:
    await _seed_inventory(sessionmaker, tmp_path)
    await cms_sim.seed_simulated_cms(sessionmaker=sessionmaker, provider=sim)
    service = build_deployment_service(
        sessionmaker=sessionmaker,
        settings=make_settings(cms_provider="simulated", cms_sim_database_url=SIM_URL),
        cms=sim,
    )
    inventory = await _inventory(sessionmaker)
    stored = {p.url: p.page_id for p in await sim.list_pages()}
    async with sessionmaker() as session:
        for url, row in inventory.items():
            resolved = await service._cms_page_id(session, row.id, url)
            assert resolved == stored[url], url
            assert (await sim.get_page(page_id=resolved)).url == url


async def test_reseed_keeps_existing_pages_and_reset_starts_over(
    sessionmaker: SM, sim: SimulatedCMSProvider, tmp_path: Path
) -> None:
    await _seed_inventory(sessionmaker, tmp_path)
    first = await cms_sim.seed_simulated_cms(sessionmaker=sessionmaker, provider=sim)
    assert first.seeded == 4
    edited = await sim.edit_page(page_id="cms-42", body_html="<p>edited in the CMS</p>")

    again = await cms_sim.seed_simulated_cms(sessionmaker=sessionmaker, provider=sim)
    assert again.seeded == 0 and again.already_present == 4
    kept = await sim.get_page(page_id="cms-42")
    assert kept.body_html == "<p>edited in the CMS</p>" and kept.version == edited.version

    fresh = await cms_sim.seed_simulated_cms(sessionmaker=sessionmaker, provider=sim, reset=True)
    assert fresh.reset is True and fresh.seeded == 4
    assert (await sim.get_page(page_id="cms-42")).body_html != "<p>edited in the CMS</p>"


async def test_gone_pages_are_not_seeded(
    sessionmaker: SM, sim: SimulatedCMSProvider, tmp_path: Path
) -> None:
    await _seed_inventory(sessionmaker, tmp_path)
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("UPDATE pages SET gone_at = now() WHERE canonical_url = :u"),
            {"u": rs.SITE + rs.LI},
        )
    summary = await cms_sim.seed_simulated_cms(sessionmaker=sessionmaker, provider=sim)
    assert summary.inventory_pages == 3 and summary.seeded == 3
    assert await sim.resolve_page_id(url=rs.SITE + rs.LI) is None


async def test_seed_cli(
    sessionmaker: SM,
    sim: SimulatedCMSProvider,
    tmp_path: Path,
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    export, _ = await _seed_inventory(sessionmaker, tmp_path)
    settings = make_settings(database_url=test_database_url, cms_sim_database_url=SIM_URL)
    monkeypatch.setattr(cms_sim, "get_settings", lambda: settings)

    code = await asyncio.to_thread(cms_sim.main, ["seed", "--export", str(export), "--reset"])

    out = capsys.readouterr()
    assert code == cms_sim.EXIT_OK, out.err
    assert json.loads(out.out.strip().splitlines()[-1]) == {
        "command": "cms-sim-seed",
        "database_created": False,
        "reset": True,
        "inventory_pages": 4,
        "seeded": 4,
        "already_present": 0,
        "from_export": 3,
        "skipped_conflict": 0,
    }
    assert "postgresql" not in out.out + out.err
    assert len(await sim.list_pages()) == 4


async def test_serve_the_seeded_site(
    sessionmaker: SM, sim: SimulatedCMSProvider, tmp_path: Path
) -> None:
    export, records = await _seed_inventory(sessionmaker, tmp_path)
    bodies = await cms_sim.load_export_bodies(export, max_bytes=10_000_000)
    await cms_sim.seed_simulated_cms(sessionmaker=sessionmaker, provider=sim, export_bodies=bodies)

    async with serve_simulated_site(sim) as base_url:
        host, port = base_url.removeprefix("http://").rsplit(":", 1)
        assert host == "127.0.0.1" and int(port) not in (0, 8765)
        async with httpx.AsyncClient(base_url=base_url, trust_env=False) as client:
            full = await client.get(FULL_DOC_PATH)
            assert full.status_code == 200
            assert full.headers["content-type"].startswith("text/html")
            assert full.text == records[2]["body_html"]  # a full document: verbatim
            fragment = await client.get("/cms-page")
            assert fragment.status_code == 200
            assert '<link rel="canonical" href="https://site.test/cms-page">' in fragment.text
            assert "Managed in the CMS" in fragment.text
            assert (await client.get(rs.EV)).status_code == 200
            assert (await client.get("/reports/unknown")).status_code == 404
            robots = await client.get("/robots.txt")
            assert robots.status_code == 200 and "Allow: /" in robots.text

            # A CMS write is visible on the next fetch (HTTP verification reads this).
            await sim.edit_page(page_id="cms-42", body_html="<p>new body</p>")
            assert "new body" in (await client.get("/cms-page")).text


async def test_serve_cli_prints_the_verified_url(
    sessionmaker: SM,
    sim: SimulatedCMSProvider,
    tmp_path: Path,
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _seed_inventory(sessionmaker, tmp_path)
    await cms_sim.seed_simulated_cms(sessionmaker=sessionmaker, provider=sim)
    settings = make_settings(database_url=test_database_url, cms_sim_database_url=SIM_URL)
    fetched: dict[str, int] = {}

    async def fetch_then_return(self: SimulatedSite) -> None:  # instead of "until Ctrl-C"
        async with httpx.AsyncClient(trust_env=False) as client:
            fetched["status"] = (await client.get(self.base_url + "/cms-page")).status_code

    monkeypatch.setattr(cms_sim, "get_settings", lambda: settings)
    monkeypatch.setattr(SimulatedSite, "wait", fetch_then_return)

    code = await asyncio.to_thread(cms_sim.main, ["serve"])

    out = capsys.readouterr()
    assert code == cms_sim.EXIT_OK, out.err
    line = json.loads(out.out.strip().splitlines()[-1])
    assert line["command"] == "cms-sim-serve" and line["verified"] is True
    assert line["pages"] == 4
    assert line["base_url"].startswith("http://127.0.0.1:")
    assert int(line["base_url"].rsplit(":", 1)[1]) not in (0, 8765)
    assert fetched["status"] == 200
    assert "postgresql" not in out.out + out.err


async def test_reconcile_cli_against_the_real_service(
    sessionmaker: SM,
    sim: SimulatedCMSProvider,
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI's wiring end to end (settings -> engine -> provider -> composed service), with
    the simulated provider (``mock`` is refused: unit-tested in test_deployment_ops_cli)."""
    settings = make_settings(
        database_url=test_database_url, cms_provider="simulated", cms_sim_database_url=SIM_URL
    )
    monkeypatch.setattr(deploy_cli, "get_settings", lambda: settings)

    code = await asyncio.to_thread(deploy_cli.main, ["reconcile", "--older-than-s", "0"])

    out = capsys.readouterr()
    assert code == deploy_cli.EXIT_OK, out.err
    data = json.loads(out.out.strip().splitlines()[-1])
    assert data["command"] == "reconcile" and data["cms_provider"] == "simulated"
    assert data["examined"] == 0 and data["still_unknown"] == 0
    assert "postgresql" not in out.out + out.err
