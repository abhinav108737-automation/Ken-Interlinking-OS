"""CMSContentSource and MockCMSInventoryReader (ken_services.inventory.sources.cms)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ken_contracts.cms import CMSError, CMSNotFoundError, CMSPage
from ken_contracts.inventory import (
    CMSInventoryReader,
    CMSPageRef,
    CMSPageRefPage,
    ContentSourceProvider,
    IssueCode,
    IssueSeverity,
    PublicationState,
    SourceDocument,
    SourceIssue,
    SourceType,
)
from ken_core.providers.cms.seed import SEED_PAGE_IDS, seed_pages
from ken_services.inventory.normalize import normalize_document
from ken_services.inventory.sources.cms import CMSContentSource, MockCMSInventoryReader

NOW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
SITE = "https://example.com"


def _page(n: int, **overrides: object) -> CMSPage:
    values: dict[str, object] = {
        "page_id": f"p{n}",
        "url": f"https://example.com/p{n}",
        "title": f"Page {n}",
        "body_html": f"<p>Body {n}</p>",
        "version": "v1",
    }
    values.update(overrides)
    return CMSPage.model_validate(values)


def _source(reader: CMSInventoryReader, **kwargs: object) -> CMSContentSource:
    return CMSContentSource(reader, site_url=SITE, clock=lambda: NOW, **kwargs)  # type: ignore[arg-type]


async def _collect(source: CMSContentSource) -> list[SourceDocument | SourceIssue]:
    await source.site_scope()
    return [record async for record in source.iter_records()]


class RecordingReader(MockCMSInventoryReader):
    """Mock reader that records calls and can fail chosen reads."""

    def __init__(self, pages: list[CMSPage], failures: dict[str, Exception] | None = None) -> None:
        super().__init__(pages)
        self.failures = failures or {}
        self.list_calls: list[tuple[str | None, int]] = []
        self.closed = 0

    async def list_pages(self, *, cursor: str | None = None, limit: int = 100) -> CMSPageRefPage:
        self.list_calls.append((cursor, limit))
        return await super().list_pages(cursor=cursor, limit=limit)

    async def get_page(self, *, page_id: str) -> CMSPage:
        if page_id in self.failures:
            raise self.failures[page_id]
        return await super().get_page(page_id=page_id)

    async def aclose(self) -> None:
        self.closed += 1


class LoopingReader:
    """Returns the same cursor forever."""

    name = "looping"

    def __init__(self) -> None:
        self.calls = 0

    async def list_pages(self, *, cursor: str | None = None, limit: int = 100) -> CMSPageRefPage:
        self.calls += 1
        ref = CMSPageRef(page_id="p1", url="https://example.com/p1", version="v1")
        return CMSPageRefPage(items=(ref,), next_cursor="same")

    async def get_page(self, *, page_id: str) -> CMSPage:
        return _page(1)

    async def aclose(self) -> None:
        return None


class SecretReader(MockCMSInventoryReader):
    name = "secret-reader"

    def __init__(self) -> None:
        super().__init__([])
        self.api_token = "tok-SUPERSECRET"
        self.base_url = "https://svc:pw@cms.internal/api"


class BrokenListReader(MockCMSInventoryReader):
    async def list_pages(self, *, cursor: str | None = None, limit: int = 100) -> CMSPageRefPage:
        raise CMSError("listing unavailable")


# --------------------------------------------------------------------------- #
# Source
# --------------------------------------------------------------------------- #
def test_implements_protocol() -> None:
    source = _source(MockCMSInventoryReader())
    assert isinstance(source, ContentSourceProvider)
    assert source.name == "cms:mock"
    assert source.source_type is SourceType.CMS


async def test_reads_seed_pages() -> None:
    source = _source(MockCMSInventoryReader())
    scope = await source.site_scope()
    records = [record async for record in source.iter_records()]
    docs = [record for record in records if isinstance(record, SourceDocument)]
    assert [doc.source_id for doc in docs] == list(SEED_PAGE_IDS)
    seed = {page.page_id: page for page in seed_pages()}
    for doc in docs:
        page = seed[doc.source_id]
        assert doc.source_type is SourceType.CMS
        assert doc.requested_url == doc.final_url == page.url
        assert doc.html == page.body_html
        assert doc.http_status is None
        assert doc.fetched_at == NOW
        assert doc.assertions.external_id == page.page_id
        assert doc.assertions.title == page.title
        assert doc.assertions.canonical_url == page.canonical_url
        assert doc.assertions.publication_state is PublicationState.PUBLISHED
        assert doc.assertions.published_at == page.published_at
        assert doc.assertions.modified_at == page.updated_at
        assert doc.assertions.metadata == {k: str(v) for k, v in page.metadata.items()}
        normalized = normalize_document(doc, scope=scope)
        assert normalized.external_id == page.page_id
        assert normalized.title == page.title
    summary = await source.summary()
    assert (summary.discovered, summary.skipped) == (3, 0)


async def test_paging_follows_cursors() -> None:
    reader = RecordingReader([_page(n) for n in range(5)])
    source = _source(reader, page_size=2)
    records = await _collect(source)
    assert [r.source_id for r in records if isinstance(r, SourceDocument)] == [
        "p0",
        "p1",
        "p2",
        "p3",
        "p4",
    ]
    assert reader.list_calls == [(None, 2), ("2", 2), ("4", 2)]


async def test_not_found_and_other_errors_become_issues() -> None:
    reader = RecordingReader(
        [_page(1), _page(2), _page(3)],
        failures={
            "p1": CMSNotFoundError("gone: secret detail"),
            "p2": CMSError("backend said: password=xyz"),
        },
    )
    records = await _collect(_source(reader))
    first, second, third = records
    assert isinstance(first, SourceIssue)
    assert (first.code, first.severity) == (IssueCode.HTTP_NOT_FOUND, IssueSeverity.WARNING)
    assert first.url == "https://example.com/p1"
    assert isinstance(second, SourceIssue)
    assert (second.code, second.severity) == (IssueCode.NETWORK_ERROR, IssueSeverity.ERROR)
    assert "CMSError" in second.message
    assert "password" not in second.message
    assert "secret" not in first.message
    assert isinstance(third, SourceDocument)


async def test_list_errors_propagate() -> None:
    source = _source(BrokenListReader())
    await source.site_scope()
    with pytest.raises(CMSError):
        _ = [record async for record in source.iter_records()]


async def test_repeating_cursor_stops_with_error_issue() -> None:
    reader = LoopingReader()
    records = await _collect(_source(reader))
    assert reader.calls == 2
    last = records[-1]
    assert isinstance(last, SourceIssue)
    assert last.severity is IssueSeverity.ERROR
    assert "cursor" in last.message
    assert sum(isinstance(r, SourceDocument) for r in records) == 2


async def test_out_of_scope_and_invalid_urls() -> None:
    reader = MockCMSInventoryReader(
        [
            _page(1, url="https://elsewhere.com/p1"),
            _page(2, url="https://user:pw@example.com/p2"),
            _page(3),
        ]
    )
    source = _source(reader)
    records = await _collect(source)
    first, second, third = records
    assert isinstance(first, SourceIssue)
    assert (first.code, first.severity) == (IssueCode.OUT_OF_SCOPE, IssueSeverity.WARNING)
    assert isinstance(second, SourceIssue)
    assert second.code is IssueCode.INVALID_URL
    assert second.url is None
    assert isinstance(third, SourceDocument)
    summary = await source.summary()
    assert (summary.discovered, summary.skipped) == (3, 1)


async def test_unpublished_naive_dates_and_metadata() -> None:
    page = _page(
        1,
        published_at=None,
        updated_at=datetime(2026, 1, 1, 12, 0),  # naive: zone unknown, dropped
        metadata={
            "s": "x",
            "i": 3,
            "f": 1.5,
            "b": True,
            "n": None,
            "l": [1, 2],
            "d": {"a": 1},
        },
    )
    records = await _collect(_source(MockCMSInventoryReader([page])))
    doc = records[0]
    assert isinstance(doc, SourceDocument)
    assert doc.assertions.publication_state is PublicationState.UNKNOWN
    assert doc.assertions.published_at is None
    assert doc.assertions.modified_at is None
    assert doc.assertions.metadata == {"s": "x", "i": "3", "f": "1.5", "b": "true"}


async def test_invalid_source_document_becomes_issue() -> None:
    page = _page(1, page_id="x" * 600)  # external_id longer than the contract allows
    records = await _collect(_source(MockCMSInventoryReader([page])))
    issue = records[0]
    assert isinstance(issue, SourceIssue)
    assert issue.code is IssueCode.INVALID_RECORD
    assert "x" * 50 not in issue.message


def test_config_snapshot_is_secret_free() -> None:
    reader = SecretReader()
    source = CMSContentSource(
        reader,
        site_url="https://example.com/",
        allowed_hosts=["WWW.example.com:443"],
        page_size=50,
    )
    snapshot = source.config_snapshot()
    assert snapshot == {
        "reader": "secret-reader",
        "site_url": "https://example.com/",
        "allowed_hosts": ["www.example.com"],
        "page_size": 50,
    }
    dumped = json.dumps(snapshot)
    assert "SUPERSECRET" not in dumped
    assert "cms.internal" not in dumped
    assert "pw" not in dumped


def test_config_snapshot_drops_credentialed_site_url() -> None:
    source = CMSContentSource(MockCMSInventoryReader(), site_url="https://u:pw@example.com/")
    assert source.config_snapshot()["site_url"] is None


@pytest.mark.parametrize("page_size", [0, -1, 1001])
def test_page_size_bounds(page_size: int) -> None:
    with pytest.raises(ValueError, match="page_size"):
        CMSContentSource(MockCMSInventoryReader(), site_url=SITE, page_size=page_size)


async def test_aclose_closes_reader_once() -> None:
    reader = RecordingReader([])
    source = _source(reader)
    await source.aclose()
    await source.aclose()
    assert reader.closed == 1


# --------------------------------------------------------------------------- #
# Mock reader
# --------------------------------------------------------------------------- #
def test_mock_reader_satisfies_protocol() -> None:
    reader = MockCMSInventoryReader()
    assert isinstance(reader, CMSInventoryReader)
    assert reader.name == "mock"


def test_mock_reader_is_read_only() -> None:
    public = {name for name in dir(MockCMSInventoryReader) if not name.startswith("_")}
    assert public == {"name", "list_pages", "get_page", "aclose"}


async def test_mock_reader_paging_is_deterministic() -> None:
    reader = MockCMSInventoryReader([_page(n) for n in range(3)])
    first = await reader.list_pages(limit=2)
    assert [ref.page_id for ref in first.items] == ["p0", "p1"]
    assert first.next_cursor is not None
    second = await reader.list_pages(cursor=first.next_cursor, limit=2)
    assert [ref.page_id for ref in second.items] == ["p2"]
    assert second.next_cursor is None
    assert await reader.list_pages(limit=2) == first


async def test_mock_reader_errors() -> None:
    reader = MockCMSInventoryReader()
    with pytest.raises(CMSNotFoundError):
        await reader.get_page(page_id="no-such-page")
    with pytest.raises(CMSError):
        await reader.list_pages(cursor="not-a-cursor")
    with pytest.raises(CMSError):
        await reader.list_pages(cursor="999")
    with pytest.raises(ValueError, match="limit"):
        await reader.list_pages(limit=0)


async def test_mock_reader_returns_copies() -> None:
    pages = [_page(1)]
    reader = MockCMSInventoryReader(pages)
    pages[0].title = "mutated by caller"
    served = await reader.get_page(page_id="p1")
    assert served.title == "Page 1"
    served.title = "mutated after read"
    assert (await reader.get_page(page_id="p1")).title == "Page 1"
    await reader.aclose()
    await reader.aclose()


async def test_empty_reader() -> None:
    source = _source(MockCMSInventoryReader([]))
    assert await _collect(source) == []
    summary = await source.summary()
    assert (summary.discovered, summary.skipped) == (0, 0)
