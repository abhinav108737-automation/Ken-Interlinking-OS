"""FileExportContentSource (ken_services.inventory.sources.file_export)."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ken_contracts.inventory import (
    ContentSourceProvider,
    IssueCode,
    IssueSeverity,
    PublicationState,
    SourceDocument,
    SourceIssue,
    SourceType,
)
from ken_services.inventory.normalize import normalize_document
from ken_services.inventory.sources.file_export import ExportFormatError, FileExportContentSource

NOW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
HEADER: dict[str, Any] = {
    "format": "ken-inventory-export",
    "version": 1,
    "site_url": "https://example.com",
    "allowed_hosts": ["www.example.com"],
}


def _write(path: Path, header: dict[str, Any] | str | None, *lines: dict[str, Any] | str) -> Path:
    out: list[str] = []
    if header is not None:
        out.append(header if isinstance(header, str) else json.dumps(header))
    out.extend(line if isinstance(line, str) else json.dumps(line) for line in lines)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def _source(path: Path, max_bytes: int = 1_000_000) -> FileExportContentSource:
    return FileExportContentSource(path, max_bytes=max_bytes, clock=lambda: NOW)


async def _collect(source: FileExportContentSource) -> list[SourceDocument | SourceIssue]:
    await source.site_scope()
    return [record async for record in source.iter_records()]


def test_implements_protocol(tmp_path: Path) -> None:
    source = _source(tmp_path / "x.jsonl")
    assert isinstance(source, ContentSourceProvider)
    assert source.name == "file_export"
    assert source.source_type is SourceType.FILE_EXPORT


async def test_valid_file(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "export.jsonl",
        HEADER,
        {
            "url": "https://Example.com/a?utm_source=x",
            "body_html": "<h1>A</h1><p>Hello <a href='/b'>b</a></p>",
            "external_id": "cms-a",
            "page_type": "report",
            "title": "Asserted A",
            "canonical_url": "https://example.com/a",
            "meta_description": "Desc",
            "publication_state": "draft",
            "language": "en",
            "published_at": "2026-01-01T00:00:00Z",
            "modified_at": "2026-02-01T00:00:00Z",
            "metadata": {"k": "v"},
        },
        "",
        "   ",
        {"url": "https://www.example.com/b", "body_html": "<p>B</p>"},
    )
    source = _source(path)
    scope = await source.site_scope()
    assert scope.allowed_hosts == frozenset({"example.com", "www.example.com"})
    records = [record async for record in source.iter_records()]
    assert all(isinstance(record, SourceDocument) for record in records)
    docs = [record for record in records if isinstance(record, SourceDocument)]
    first, second = docs
    assert first.source_type is SourceType.FILE_EXPORT
    assert first.source_id == "cms-a"
    assert first.requested_url == first.final_url == "https://example.com/a"
    assert first.http_status is None
    assert first.fetched_at == NOW
    assert first.assertions.external_id == "cms-a"
    assert first.assertions.page_type == "report"
    assert first.assertions.title == "Asserted A"
    assert first.assertions.publication_state is PublicationState.DRAFT
    assert first.assertions.metadata == {"k": "v"}
    assert first.assertions.published_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert second.source_id == "https://www.example.com/b"  # no external id -> url
    assert second.assertions.external_id is None

    summary = await source.summary()
    assert (summary.discovered, summary.skipped) == (2, 0)

    page = normalize_document(first, scope=scope)
    assert page.title == "Asserted A"
    assert page.external_id == "cms-a"
    assert page.publication_state is PublicationState.DRAFT
    await source.aclose()


async def test_invalid_lines_become_issues(tmp_path: Path) -> None:
    secret = "hunter2-secret-value"
    path = _write(
        tmp_path / "export.jsonl",
        HEADER,
        "{not json",
        json.dumps([1, 2]),
        {"url": "https://example.com/missing-body"},
        {"url": "https://example.com/x", "body_html": "<p>x</p>", "unknown": secret},
        {"url": f"https://admin:{secret}@example.com/", "body_html": "<p>x</p>"},
        {"url": "ftp://example.com/f", "body_html": "<p>x</p>"},
        {"url": "https://example.com/ok", "body_html": "<p>ok</p>"},
    )
    records = await _collect(_source(path))
    issues = [record for record in records if isinstance(record, SourceIssue)]
    docs = [record for record in records if isinstance(record, SourceDocument)]
    assert [doc.final_url for doc in docs] == ["https://example.com/ok"]
    assert [(issue.code, issue.severity) for issue in issues] == [
        (IssueCode.INVALID_RECORD, IssueSeverity.ERROR),
        (IssueCode.INVALID_RECORD, IssueSeverity.ERROR),
        (IssueCode.INVALID_RECORD, IssueSeverity.ERROR),
        (IssueCode.INVALID_RECORD, IssueSeverity.ERROR),
        (IssueCode.INVALID_URL, IssueSeverity.ERROR),
        (IssueCode.INVALID_URL, IssueSeverity.ERROR),
    ]
    assert issues[0].message == "line 2: invalid JSON"
    assert issues[1].message == "line 3: model_type"
    assert issues[2].message == "line 4: missing"
    assert issues[3].message == "line 5: extra_forbidden"
    assert issues[4].message.startswith("line 6: ")
    for issue in issues:
        assert secret not in issue.message
        assert issue.url is None or secret not in issue.url
        assert issue.occurred_at == NOW


async def test_out_of_scope_and_duplicates(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "export.jsonl",
        HEADER,
        {"url": "https://example.com/a", "body_html": "<p>1</p>"},
        {"url": "https://other.com/a", "body_html": "<p>2</p>"},
        {"url": "https://EXAMPLE.com/a#frag", "body_html": "<p>3</p>"},
        {"url": "https://blog.example.com/", "body_html": "<p>4</p>"},
        "{broken",
    )
    source = _source(path)
    records = await _collect(source)
    kinds = [record.code if isinstance(record, SourceIssue) else "doc" for record in records]
    assert kinds == [
        "doc",
        IssueCode.OUT_OF_SCOPE,
        IssueCode.DUPLICATE_URL,
        IssueCode.OUT_OF_SCOPE,
        IssueCode.INVALID_RECORD,
    ]
    out_of_scope = records[1]
    duplicate = records[2]
    assert isinstance(out_of_scope, SourceIssue)
    assert out_of_scope.severity is IssueSeverity.WARNING
    assert out_of_scope.url == "https://other.com/a"
    assert isinstance(duplicate, SourceIssue)
    assert duplicate.severity is IssueSeverity.WARNING
    assert duplicate.url == duplicate.related_url == "https://example.com/a"
    assert "line 2" in duplicate.message
    summary = await source.summary()
    assert (summary.discovered, summary.skipped) == (5, 3)


async def test_iteration_without_explicit_site_scope(tmp_path: Path) -> None:
    path = _write(tmp_path / "e.jsonl", HEADER, {"url": "https://example.com/", "body_html": ""})
    source = _source(path)
    records = [record async for record in source.iter_records()]
    assert len(records) == 1


async def test_header_with_bom_and_crlf(tmp_path: Path) -> None:
    path = tmp_path / "e.jsonl"
    body = json.dumps({"url": "https://example.com/", "body_html": "<p>x</p>"})
    path.write_bytes(("﻿" + json.dumps(HEADER) + "\r\n" + body + "\r\n").encode("utf-8"))
    records = await _collect(_source(path))
    assert [type(record) for record in records] == [SourceDocument]


async def test_missing_file(tmp_path: Path) -> None:
    source = _source(tmp_path / "missing.jsonl")
    with pytest.raises(ExportFormatError, match="not found"):
        await source.site_scope()


async def test_directory_is_not_a_file(tmp_path: Path) -> None:
    with pytest.raises(ExportFormatError):
        await _source(tmp_path).site_scope()


async def test_oversize_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "e.jsonl", HEADER, {"url": "https://example.com/", "body_html": "x"})
    with pytest.raises(ExportFormatError, match="larger than"):
        await _source(path, max_bytes=50).site_scope()


@pytest.mark.parametrize(
    "header",
    [
        "",
        "not json",
        json.dumps({**HEADER, "format": "something-else"}),
        json.dumps({**HEADER, "version": 2}),
        json.dumps({**HEADER, "unexpected": True}),
        json.dumps({k: v for k, v in HEADER.items() if k != "site_url"}),
        json.dumps({**HEADER, "site_url": "ftp://example.com"}),
        json.dumps({**HEADER, "site_url": "https://user:pw@example.com"}),
        json.dumps({**HEADER, "allowed_hosts": ["user:pw@www.example.com"]}),
        json.dumps({**HEADER, "allowed_hosts": ["bad host name"]}),
    ],
)
async def test_bad_header(tmp_path: Path, header: str) -> None:
    path = _write(tmp_path / "e.jsonl", header, {"url": "https://example.com/", "body_html": ""})
    with pytest.raises(ExportFormatError) as excinfo:
        await _source(path).site_scope()
    assert "pw@" not in str(excinfo.value)


async def test_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "e.jsonl"
    path.write_bytes(b"")
    with pytest.raises(ExportFormatError):
        await _source(path).site_scope()


async def test_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "e.jsonl"
    path.write_bytes(
        json.dumps(HEADER).encode()
        + b"\n"
        + b'{"url": "https://example.com/", "body_html": "\xff"}\n'
    )
    with pytest.raises(ExportFormatError, match="UTF-8"):
        await _source(path).site_scope()


async def test_header_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "e.jsonl"
    path.write_bytes(b"\xff\xfe" + json.dumps(HEADER).encode() + b"\n")
    with pytest.raises(ExportFormatError, match="UTF-8"):
        await _source(path).site_scope()


async def test_config_snapshot_has_no_absolute_path(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "export.jsonl",
        {**HEADER, "allowed_hosts": ["WWW.example.com:443"]},
    )
    source = _source(path, max_bytes=12345)
    snapshot = source.config_snapshot()
    assert snapshot == {
        "path": "export.jsonl",
        "format_version": 1,
        "site_url": "https://example.com/",
        "allowed_hosts": ["www.example.com"],
        "max_bytes": 12345,
    }
    assert str(tmp_path) not in json.dumps(snapshot)
    await source.site_scope()
    assert source.config_snapshot() == snapshot


def test_config_snapshot_never_carries_credentials(tmp_path: Path) -> None:
    path = _write(tmp_path / "e.jsonl", {**HEADER, "site_url": "https://user:pw@example.com"})
    snapshot = _source(path).config_snapshot()
    assert snapshot["site_url"] is None
    assert "pw" not in json.dumps(snapshot)


def test_config_snapshot_when_file_missing(tmp_path: Path) -> None:
    snapshot = _source(tmp_path / "gone.jsonl").config_snapshot()
    assert snapshot["path"] == "gone.jsonl"
    assert snapshot["site_url"] is None
    assert snapshot["allowed_hosts"] == []


async def test_aclose_is_idempotent_and_closes_abandoned_iteration(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "e.jsonl",
        HEADER,
        {"url": "https://example.com/1", "body_html": ""},
        {"url": "https://example.com/2", "body_html": ""},
    )
    source = _source(path)
    await source.site_scope()
    iterator = source.iter_records()
    assert isinstance(iterator, AsyncGenerator)
    first = await iterator.__anext__()
    assert isinstance(first, SourceDocument)
    await source.aclose()
    await source.aclose()
    await iterator.aclose()


def test_rejects_non_positive_max_bytes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        FileExportContentSource(tmp_path / "e.jsonl", max_bytes=0)


async def test_deterministic_order(tmp_path: Path) -> None:
    lines = [{"url": f"https://example.com/{n}", "body_html": f"<p>{n}</p>"} for n in range(20)]
    path = _write(tmp_path / "e.jsonl", HEADER, *lines)
    first = await _collect(_source(path))
    second = await _collect(_source(path))
    assert first == second
