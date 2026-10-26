"""An imported HTML folder and a labelled JSONL export reach the database with their provenance."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from ken_services.inventory.ingest import run_ingestion
from ken_services.inventory.sources.file_export import FileExportContentSource
from ken_services.inventory.sources.html_dir import HtmlDirectoryContentSource
from tests.integration.recommendation_support import SM

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]
ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / "tests" / "fixtures" / "offline_demo" / "synthetic_extension.jsonl"


async def _provenance(sessionmaker: SM) -> dict[str, int]:
    async with sessionmaker() as session:
        rows = await session.execute(
            text(
                "SELECT coalesce(v.metadata->>'provenance', 'unlabelled'), count(*) "
                "FROM pages p JOIN page_versions v "
                "ON v.page_id = p.id AND v.version = p.current_version "
                "GROUP BY 1"
            )
        )
        return {str(r[0]): int(r[1]) for r in rows}


async def test_labelled_jsonl_and_an_html_folder_keep_their_provenance(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ext = FileExportContentSource(EXTENSION, max_bytes=50_000_000, provenance="synthetic_extension")
    assert (await run_ingestion(source=ext, sessionmaker=sessionmaker)).status.value == "succeeded"

    folder = tmp_path / "authorised"
    (folder / "reports").mkdir(parents=True)
    page = "<html><head><title>{t}</title></head><body><h1>{t}</h1><p>{p}</p></body></html>"
    (folder / "index.html").write_text(page.format(t="Home", p="Welcome " * 40))
    (folder / "reports" / "ev.html").write_text(page.format(t="EV", p="Electric vehicles " * 40))
    html = HtmlDirectoryContentSource(
        folder, site_url="https://research.example.com/", provenance="authorized_import"
    )
    assert (await run_ingestion(source=html, sessionmaker=sessionmaker)).status.value == "succeeded"

    counts = await _provenance(sessionmaker)
    assert counts == {"synthetic_extension": 11, "authorized_import": 2}
    assert "unlabelled" not in counts
