"""``--provenance`` is stamped on every record of a JSONL import and validated."""

from __future__ import annotations

from pathlib import Path

import pytest

from ken_contracts.inventory import SourceDocument
from ken_services.inventory import cli
from ken_services.inventory.sources.file_export import FileExportContentSource
from tests.conftest import make_settings

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / "tests" / "fixtures" / "demo" / "research_site.jsonl"


async def test_every_record_carries_the_label() -> None:
    source = FileExportContentSource(BASE, max_bytes=50_000_000, provenance="synthetic_base")
    await source.site_scope()
    docs = [r async for r in source.iter_records() if isinstance(r, SourceDocument)]
    await source.aclose()
    assert len(docs) == 65  # 64 pages; one URL appears twice and is merged at ingestion
    assert all(d.assertions.metadata["provenance"] == "synthetic_base" for d in docs)
    assert source.config_snapshot()["provenance"] == "synthetic_base"


async def test_without_a_label_nothing_is_stamped() -> None:
    source = FileExportContentSource(BASE, max_bytes=50_000_000)
    await source.site_scope()
    docs = [r async for r in source.iter_records() if isinstance(r, SourceDocument)]
    await source.aclose()
    assert all("provenance" not in d.assertions.metadata for d in docs)


def test_an_unknown_label_is_a_usage_error() -> None:
    with pytest.raises(cli.UsageError):
        cli.build_file_source(BASE, make_settings(), "totally_real")


def test_the_html_folder_command_is_wired(tmp_path: Path) -> None:
    parsed = cli._parser().parse_args(
        [
            "import-html-dir",
            str(tmp_path),
            "--site-url",
            "https://x.example.test/",
            "--provenance",
            "authorized_import",
        ]
    )
    source = cli._build_source(parsed, make_settings())
    assert source.name == "html_directory"
    with pytest.raises(cli.UsageError):
        cli.build_html_dir_source(tmp_path / "missing", "https://x.example.test/", None)
