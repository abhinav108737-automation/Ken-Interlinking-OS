"""Local HTML-folder import: URL mapping, safety limits, provenance. No network involved."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ken_contracts.inventory import IssueCode, SourceDocument, SourceIssue
from ken_services.inventory.sources.html_dir import HtmlDirectoryContentSource

SITE = "https://docs.example.test/"


def _write(
    root: Path, name: str, html: str = "<html><head><title>t</title></head><body>x</body></html>"
) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")


async def _run(source: HtmlDirectoryContentSource) -> list[SourceDocument | SourceIssue]:
    try:
        await source.site_scope()
        return [r async for r in source.iter_records()]
    finally:
        await source.aclose()


async def test_paths_map_to_urls_deterministically(tmp_path: Path) -> None:
    _write(tmp_path, "index.html")
    _write(tmp_path, "reports/ev.html")
    _write(tmp_path, "reports/index.htm")
    _write(tmp_path, "notes.txt", "ignored")
    records = await _run(
        HtmlDirectoryContentSource(tmp_path, site_url=SITE, provenance="authorized_import")
    )
    docs = [r for r in records if isinstance(r, SourceDocument)]
    assert [d.final_url for d in docs] == [
        "https://docs.example.test/",
        "https://docs.example.test/reports/ev",
        "https://docs.example.test/reports/",
    ]
    assert all(d.assertions.metadata["provenance"] == "authorized_import" for d in docs)
    assert {d.assertions.metadata["source_file"] for d in docs} == {
        "index.html",
        "reports/ev.html",
        "reports/index.htm",
    }


async def test_a_duplicate_url_is_an_issue_not_a_failure(tmp_path: Path) -> None:
    _write(tmp_path, "a.html")
    _write(tmp_path, "a.htm")
    records = await _run(HtmlDirectoryContentSource(tmp_path, site_url=SITE))
    assert len([r for r in records if isinstance(r, SourceDocument)]) == 1
    assert [r.code for r in records if isinstance(r, SourceIssue)] == [IssueCode.DUPLICATE_URL]


async def test_a_symlink_leaving_the_folder_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    inside = tmp_path / "inside"
    _write(outside, "secret.html")
    inside.mkdir()
    os.symlink(outside / "secret.html", inside / "link.html")
    records = await _run(HtmlDirectoryContentSource(inside, site_url=SITE))
    assert not [r for r in records if isinstance(r, SourceDocument)]
    issue = next(r for r in records if isinstance(r, SourceIssue))
    assert issue.code is IssueCode.OUT_OF_SCOPE


async def test_oversize_and_non_utf8_files_are_issues(tmp_path: Path) -> None:
    _write(tmp_path, "big.html", "x" * 5000)
    (tmp_path / "bad.html").write_bytes(b"\xff\xfe\x00bad")
    _write(tmp_path, "ok.html")
    source = HtmlDirectoryContentSource(tmp_path, site_url=SITE, max_file_bytes=1000)
    records = await _run(source)
    codes = sorted(r.code.value for r in records if isinstance(r, SourceIssue))
    assert codes == ["invalid_record", "too_large"] or codes == ["too_large", "too_large"]
    assert [r.final_url for r in records if isinstance(r, SourceDocument)] == [
        "https://docs.example.test/ok"
    ]


async def test_the_file_limit_is_reported(tmp_path: Path) -> None:
    for i in range(5):
        _write(tmp_path, f"p{i}.html")
    records = await _run(HtmlDirectoryContentSource(tmp_path, site_url=SITE, max_files=3))
    assert len([r for r in records if isinstance(r, SourceDocument)]) == 3
    assert [r.code for r in records if isinstance(r, SourceIssue)] == [IssueCode.LIMIT_REACHED]


async def test_a_missing_folder_fails_before_any_record(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="folder"):
        await HtmlDirectoryContentSource(tmp_path / "nope", site_url=SITE).site_scope()


def test_the_snapshot_names_the_folder_not_its_path(tmp_path: Path) -> None:
    snapshot = HtmlDirectoryContentSource(
        tmp_path, site_url=SITE, provenance="authorized_import"
    ).config_snapshot()
    assert snapshot["path"] == tmp_path.name and str(tmp_path.parent) not in str(snapshot)
    assert snapshot["provenance"] == "authorized_import"


def test_an_unknown_provenance_label_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="provenance"):
        HtmlDirectoryContentSource(tmp_path, site_url=SITE, provenance="real_ken_content")
