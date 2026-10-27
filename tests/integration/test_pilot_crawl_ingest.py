"""Pilot crawl -> ingestion into a database, against a mock of the site (no network)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ken_services.inventory import pilot
from ken_services.inventory.ingest import run_ingestion
from ken_services.inventory.sources.public_web import PublicWebContentSource
from tests.integration.recommendation_support import SM
from tests.unit.inventory.test_pilot_config import CONTACT, PILOT25, _fixture_site

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]


async def test_the_pilot_fixture_crawl_stores_only_the_approved_host(sessionmaker: SM) -> None:
    config = pilot.load_pilot(PILOT25)
    site = _fixture_site()
    fake = site.time
    assert fake is not None
    source = PublicWebContentSource(
        config.crawl_config(CONTACT).model_copy(update={"render": "http"}),
        transport=site.transport(),
        sleep=fake.sleep,
        monotonic=fake.monotonic,
    )
    detail = await run_ingestion(source=source, sessionmaker=sessionmaker)
    assert detail.status.value in {"succeeded", "completed_with_errors"}
    async with sessionmaker() as session:
        host_rows = await session.execute(
            text("SELECT DISTINCT split_part(canonical_url, '/', 3) FROM pages ORDER BY 1")
        )
        hosts: list[str] = [str(row[0]) for row in host_rows]
        count_row = await session.execute(text("SELECT count(*) FROM pages"))
        count: int = int(count_row.scalar_one())
        for table in ("deployments", "publish_attempts", "link_plans"):
            scalar = await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            assert scalar.scalar_one() == 0, f"{table} must stay empty: the pilot inventories only"
    assert hosts == ["www.kenresearch.com"]
    assert 13 <= count <= 25
