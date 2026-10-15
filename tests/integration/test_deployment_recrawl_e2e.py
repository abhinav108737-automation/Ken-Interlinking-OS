"""The local loop end to end (ADR-0014 "Consequences"): a site served from the SIMULATED CMS
on a loopback port -> imported, analysed, recommended -> approved by a reviewer -> deployed by
an operator into the simulated CMS (its OWN database) -> verified over real HTTP against the
served site -> RE-CRAWLED by the Phase 1 crawler, after which the deployed link is an
EXISTING link of the source page's new version, and the engine treats the pair as already
linked.

The simulated CMS uses the disposable ``ken_cms_sim_test`` database (override with
``KEN_TEST_CMS_SIM_DATABASE_URL``, which must name a ``ken_cms_sim*_test`` database). The
server binds 127.0.0.1 port 0 and verifies its own listener before use (never 8765).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Final

import pytest

from ken_contracts.auth import Role
from ken_contracts.deployment import DeploymentStatus
from ken_contracts.inventory import CrawlConfig
from ken_contracts.recommendations import LinkPlanStatus
from ken_contracts.review import ApproveRequest
from ken_core.providers.cms.factory import build_html_engine
from ken_core.providers.cms.simulated import SimulatedCMSProvider, ensure_database
from ken_services.deployment.cms_sim import seed_simulated_cms
from ken_services.deployment.http_probe import HttpProbe
from ken_services.deployment.mutation import HtmlMutator
from ken_services.deployment.service import DatabaseDeploymentService
from ken_services.deployment.sim_site import serve_simulated_site
from ken_services.deployment.verification import HtmlLinkVerifier
from ken_services.inventory.ingest import run_ingestion
from ken_services.inventory.sources.file_export import FileExportContentSource
from ken_services.inventory.sources.public_web import PublicWebContentSource
from tests.integration.deployment_support import deploy_request, plan_status
from tests.integration.recommendation_support import (
    CHARGING,
    CORPUS,
    EV,
    LI,
    SETTINGS,
    SM,
    analyse,
    body_html,
    recommend,
    rows,
)
from tests.integration.review_support import principal, queue, review_service
from tests.support.cms_sim_db import cms_sim_test_database_url

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SIM_URL: Final = cms_sim_test_database_url()
FORBIDDEN_PORT: Final = 8765


def _export(directory: Path, base: str) -> Path:
    host = base.removeprefix("http://")
    header = {
        "format": "ken-inventory-export",
        "version": 1,
        "site_url": base + "/",
        "allowed_hosts": [host.split(":")[0]],
        "exported_at": "2026-10-15T00:00:00Z",
        "generator": "deployment-e2e 1",
    }
    records = [
        {
            "url": base + path,
            "title": title,
            "body_html": body_html(title, paragraphs),
            "publication_state": "published",
            "language": "en",
        }
        for path, (title, paragraphs) in CORPUS.items()
    ]
    path = directory / "site.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in [header, *records]) + "\n", "utf-8")
    return path


async def _sim(*, reset: bool) -> SimulatedCMSProvider:
    await ensure_database(SIM_URL)
    provider = SimulatedCMSProvider(database_url=SIM_URL, html_engine=build_html_engine())
    await provider.ensure_schema()
    if reset:
        await provider.reset()
    return provider


async def _links_from(sessionmaker: SM, source_url: str) -> list[Any]:
    return await rows(
        sessionmaker,
        "SELECT l.resolved_url AS target_url, l.anchor_text, k.page_id AS target_page_id "
        "FROM existing_links l JOIN pages p ON p.id = l.page_id "
        "AND l.page_version = p.current_version LEFT JOIN known_urls k ON k.url = l.resolved_url "
        "WHERE p.canonical_url = :u ORDER BY l.resolved_url",
        u=source_url,
    )


async def test_deploy_to_the_simulated_cms_then_recrawl_finds_the_link(
    sessionmaker: SM, tmp_path: Path
) -> None:
    await _sim(reset=True)
    site_provider = await _sim(reset=False)  # the served site reads the same database
    async with serve_simulated_site(site_provider) as base:
        port = int(base.rsplit(":", 1)[1])
        assert base.startswith("http://127.0.0.1:") and port not in (0, FORBIDDEN_PORT)

        # The inventory: the corpus at the site's own URLs, analysed and recommended.
        source = FileExportContentSource(_export(tmp_path, base), max_bytes=10_000_000)
        imported = await run_ingestion(source=source, sessionmaker=sessionmaker)
        assert imported.counters.pages_created == len(CORPUS)
        assert (await analyse(sessionmaker)).counters["errors"] == 0
        assert (await recommend(sessionmaker)).counters["errors"] == 0
        plans = {
            (r.source_url.removeprefix(base), r.target_url.removeprefix(base)): r
            for r in await rows(sessionmaker, "SELECT * FROM link_plans WHERE status = 'proposed'")
        }
        chosen = [plans[(EV, CHARGING)], plans[(EV, LI)]]

        # The simulated CMS is seeded from the inventory: the site now serves those pages.
        cms = await _sim(reset=False)
        summary = await seed_simulated_cms(sessionmaker=sessionmaker, provider=cms)
        assert summary.seeded == len(CORPUS)

        # A reviewer approves; an operator deploys with HTTP verification against the site.
        reviewer = await principal(sessionmaker, "rita", role=Role.REVIEWER)
        operator = await principal(sessionmaker, "otto", role=Role.OPERATOR_ADMIN)
        for plan in chosen:
            await queue(sessionmaker, plan.id)
            await review_service(sessionmaker).approve(
                plan_id=plan.id,
                request=ApproveRequest(
                    operation_id=uuid.uuid4(), expected_status=LinkPlanStatus.AWAITING_APPROVAL
                ),
                principal=reviewer,
            )
        settings = SETTINGS.model_copy(update={"deploy_verify_base_url": base})
        probe = HttpProbe(base_url=base, timeout_s=5.0, user_agent="KenDeployE2E/1.0")
        service = DatabaseDeploymentService(
            sessionmaker=sessionmaker,
            settings=settings,
            cms=cms,
            mutator=HtmlMutator(),
            verifier=HtmlLinkVerifier(),
            probe=probe,
        )
        try:
            detail = await service.deploy(
                request=deploy_request([p.id for p in chosen]), principal=operator
            )
        finally:
            await cms.aclose()
        assert detail.status is DeploymentStatus.VERIFIED, detail
        run = detail.verifications[-1]
        assert run.http_checked and run.passed
        for plan in chosen:
            assert await plan_status(sessionmaker, plan.id) is LinkPlanStatus.VERIFIED

        # Before the re-crawl the inventory does not know the links...
        assert await _links_from(sessionmaker, base + EV) == []
        # ...the Phase 1 crawler re-reads the served source page and follows one hop.
        config = CrawlConfig(
            base_url=base + EV,
            user_agent="KenDeployE2E/1.0",
            delay_s=0.0,
            concurrency=1,
            max_pages=20,
            max_depth=1,
            timeout_s=5.0,
            max_bytes=1_000_000,
            max_redirects=2,
        )
        crawled = await run_ingestion(
            source=PublicWebContentSource(config), sessionmaker=sessionmaker
        )
        assert crawled.counters.pages_failed == 0
        assert crawled.counters.pages_changed >= 1  # the source page gained two links

    links = await _links_from(sessionmaker, base + EV)
    by_target = {link.target_url: link for link in links}
    for plan in chosen:
        link = by_target[plan.target_url]
        assert link.anchor_text == plan.anchor_text
        assert link.target_page_id == plan.target_page_id  # resolved to the target page
    # The engine now sees the pair as already linked (and the verified plans occupy it).
    assert (await analyse(sessionmaker)).counters["errors"] == 0
    again = await recommend(sessionmaker, source_page_ids=[chosen[0].source_page_id])
    assert again.counters["errors"] == 0
    proposed = await rows(
        sessionmaker,
        "SELECT target_page_id FROM link_plans WHERE source_page_id = :s AND status = 'proposed'",
        s=chosen[0].source_page_id,
    )
    assert not {r.target_page_id for r in proposed} & {p.target_page_id for p in chosen}
