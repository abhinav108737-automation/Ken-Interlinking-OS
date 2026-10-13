"""The read-only /inventory API over a fake InventoryReader (no database)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ken_api.deps import get_inventory_reader
from ken_contracts.errors import ErrorResponse, ListResponse, PageInfo
from ken_contracts.inventory import (
    CanonicalStatus,
    ExistingLinkView,
    IngestionIssueView,
    IngestionRunDetail,
    IngestionRunSummary,
    InventoryNotFoundError,
    InventoryReader,
    IssueCode,
    IssueSeverity,
    LinkResolution,
    LinkType,
    PageDetail,
    PageSummary,
    PageVersionDetail,
    PageVersionSummary,
    PublicationState,
    RunCounters,
    RunStatus,
    SourceType,
)
from ken_core.db.engine import create_engine
from ken_core.db.session import create_sessionmaker
from tests.conftest import make_app, make_settings

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
HASH = sha256(b"x").hexdigest()
PAGE_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
UNKNOWN = uuid.UUID("99999999-9999-4999-8999-999999999999")


def _summary() -> dict[str, Any]:
    return {
        "id": PAGE_ID,
        "canonical_url": "https://site.test/a",
        "publication_state": PublicationState.PUBLISHED,
        "current_version": 2,
        "content_hash": HASH,
        "word_count": 10,
        "internal_link_count": 1,
        "external_link_count": 0,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "last_changed_at": NOW,
    }


def _version_summary(version: int) -> dict[str, Any]:
    return {
        "page_id": PAGE_ID,
        "version": version,
        "content_hash": HASH,
        "hash_version": 1,
        "run_id": RUN_ID,
        "source_type": SourceType.FILE_EXPORT,
        "source_id": "https://site.test/a",
        "url": "https://site.test/a",
        "publication_state": PublicationState.PUBLISHED,
        "word_count": 10,
        "internal_link_count": 1,
        "external_link_count": 0,
        "fetched_at": NOW,
        "created_at": NOW,
    }


def _version_detail(version: int) -> PageVersionDetail:
    return PageVersionDetail(
        **_version_summary(version),
        requested_url="https://site.test/a",
        redirect_chain=[],
        canonical_url="https://site.test/a",
        canonical_status=CanonicalStatus.SELF,
        robots_directives=[],
        metadata={},
        headings=[],
        body_text="text",
        body_html="<p>text</p>",
    )


def _link() -> ExistingLinkView:
    return ExistingLinkView(
        page_id=PAGE_ID,
        page_version=2,
        source_url="https://site.test/a",
        ordinal=0,
        href="/b",
        resolved_url="https://site.test/b",
        link_type=LinkType.INTERNAL,
        resolution=LinkResolution.RESOLVED,
        target_page_id=uuid.uuid4(),
        anchor_text="b",
        anchor_source="text",
        rel=[],
        block_id="b0",
        block_kind="p",
        occurrence_index=0,
        context_text="see b",
    )


def _run_summary() -> dict[str, Any]:
    return {
        "id": RUN_ID,
        "source_type": SourceType.FILE_EXPORT,
        "source_name": "file_export",
        "status": RunStatus.SUCCEEDED,
        "started_at": NOW,
        "completed_at": NOW,
        "counters": RunCounters(pages_fetched=1, pages_created=1),
    }


def _page[T](items: list[T], limit: int, offset: int) -> ListResponse[T]:
    return ListResponse[T](items=items, page=PageInfo(total=len(items), limit=limit, offset=offset))


class FakeInventoryReader:
    """Satisfies ``InventoryReader``; records the keyword arguments of every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    async def list_pages(
        self,
        *,
        limit: int,
        offset: int,
        search: str | None = None,
        publication_state: PublicationState | None = None,
    ) -> ListResponse[PageSummary]:
        self._record(
            "list_pages",
            limit=limit,
            offset=offset,
            search=search,
            publication_state=publication_state,
        )
        return _page([PageSummary(**_summary())], limit, offset)

    async def get_page(self, *, page_id: uuid.UUID) -> PageDetail:
        if page_id != PAGE_ID:
            raise InventoryNotFoundError("page")
        return PageDetail(**_summary(), current=_version_detail(2))

    async def list_page_versions(
        self, *, page_id: uuid.UUID, limit: int, offset: int
    ) -> ListResponse[PageVersionSummary]:
        if page_id != PAGE_ID:
            raise InventoryNotFoundError("page")
        return _page([PageVersionSummary(**_version_summary(v)) for v in (2, 1)], limit, offset)

    async def get_page_version(self, *, page_id: uuid.UUID, version: int) -> PageVersionDetail:
        if page_id != PAGE_ID:
            raise InventoryNotFoundError("page")
        if version not in (1, 2):
            raise InventoryNotFoundError("page_version")
        return _version_detail(version)

    async def list_page_links(
        self,
        *,
        page_id: uuid.UUID,
        limit: int,
        offset: int,
        version: int | None = None,
        link_type: LinkType | None = None,
    ) -> ListResponse[ExistingLinkView]:
        self._record("list_page_links", version=version, link_type=link_type)
        if page_id != PAGE_ID:
            raise InventoryNotFoundError("page")
        return _page([_link()], limit, offset)

    async def list_links(
        self,
        *,
        limit: int,
        offset: int,
        link_type: LinkType | None = None,
        resolution: LinkResolution | None = None,
        target_page_id: uuid.UUID | None = None,
    ) -> ListResponse[ExistingLinkView]:
        self._record(
            "list_links",
            link_type=link_type,
            resolution=resolution,
            target_page_id=target_page_id,
        )
        return _page([_link()], limit, offset)

    async def list_runs(
        self,
        *,
        limit: int,
        offset: int,
        source_type: SourceType | None = None,
        status: RunStatus | None = None,
    ) -> ListResponse[IngestionRunSummary]:
        self._record("list_runs", source_type=source_type, status=status)
        return _page([IngestionRunSummary(**_run_summary())], limit, offset)

    async def get_run(self, *, run_id: uuid.UUID) -> IngestionRunDetail:
        if run_id != RUN_ID:
            raise InventoryNotFoundError("run")
        return IngestionRunDetail(
            **_run_summary(), config_snapshot={"path": "x.jsonl"}, issue_counts={"timeout": 1}
        )

    async def list_run_issues(
        self,
        *,
        run_id: uuid.UUID,
        limit: int,
        offset: int,
        severity: IssueSeverity | None = None,
    ) -> ListResponse[IngestionIssueView]:
        self._record("list_run_issues", severity=severity)
        if run_id != RUN_ID:
            raise InventoryNotFoundError("run")
        issue = IngestionIssueView(
            id=1,
            run_id=RUN_ID,
            code=IssueCode.TIMEOUT,
            severity=IssueSeverity.WARNING,
            url="https://site.test/slow",
            message="request timed out",
            occurred_at=NOW,
        )
        return _page([issue], limit, offset)


@pytest.fixture
def reader() -> FakeInventoryReader:
    return FakeInventoryReader()


@pytest.fixture
def client(reader: FakeInventoryReader) -> TestClient:
    app = make_app()
    app.dependency_overrides[get_inventory_reader] = lambda: reader
    return TestClient(app, raise_server_exceptions=False)


def test_fake_reader_satisfies_the_protocol(reader: FakeInventoryReader) -> None:
    assert isinstance(reader, InventoryReader)


# --------------------------------------------------------------------------- #
# Every route: 200 and the contract shape
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("path", "model"),
    [
        ("/inventory/pages", ListResponse[PageSummary]),
        (f"/inventory/pages/{PAGE_ID}", PageDetail),
        (f"/inventory/pages/{PAGE_ID}/versions", ListResponse[PageVersionSummary]),
        (f"/inventory/pages/{PAGE_ID}/versions/1", PageVersionDetail),
        (f"/inventory/pages/{PAGE_ID}/links", ListResponse[ExistingLinkView]),
        ("/inventory/links", ListResponse[ExistingLinkView]),
        ("/inventory/runs", ListResponse[IngestionRunSummary]),
        (f"/inventory/runs/{RUN_ID}", IngestionRunDetail),
        (f"/inventory/runs/{RUN_ID}/issues", ListResponse[IngestionIssueView]),
    ],
)
def test_every_route_returns_its_contract_model(client: TestClient, path: str, model: Any) -> None:
    response = client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    if "page" in body:
        # PageInfo.has_more is a computed (output-only) field; extra="forbid"
        # refuses it on input, so a response body does not round-trip as-is.
        assert body["page"].pop("has_more") is False
    model.model_validate(body)


def test_list_defaults_and_paging(client: TestClient) -> None:
    body = client.get("/inventory/pages").json()
    assert body["page"] == {"total": 1, "limit": 50, "offset": 0, "has_more": False}
    body = client.get("/inventory/pages?limit=200&offset=5").json()
    assert (body["page"]["limit"], body["page"]["offset"]) == (200, 5)


def test_query_parameters_reach_the_reader(client: TestClient, reader: FakeInventoryReader) -> None:
    target = uuid.uuid4()
    client.get("/inventory/pages?q=india&publication_state=draft")
    client.get(f"/inventory/pages/{PAGE_ID}/links?version=1&link_type=external")
    client.get(f"/inventory/links?resolution=broken&link_type=internal&target_page_id={target}")
    client.get("/inventory/runs?source_type=cms&status=failed")
    client.get(f"/inventory/runs/{RUN_ID}/issues?severity=error")
    calls = dict(reader.calls)
    assert calls["list_pages"]["search"] == "india"
    assert calls["list_pages"]["publication_state"] is PublicationState.DRAFT
    assert calls["list_page_links"] == {"version": 1, "link_type": LinkType.EXTERNAL}
    assert calls["list_links"] == {
        "link_type": LinkType.INTERNAL,
        "resolution": LinkResolution.BROKEN,
        "target_page_id": target,
    }
    assert calls["list_runs"] == {"source_type": SourceType.CMS, "status": RunStatus.FAILED}
    assert calls["list_run_issues"] == {"severity": IssueSeverity.ERROR}


def test_body_html_is_returned_as_data(client: TestClient) -> None:
    body = client.get(f"/inventory/pages/{PAGE_ID}").json()
    assert body["current"]["body_html"] == "<p>text</p>"
    assert (
        client.get(f"/inventory/pages/{PAGE_ID}")
        .headers["content-type"]
        .startswith("application/json")
    )


# --------------------------------------------------------------------------- #
# 404 / 422 / 405
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("path", "code"),
    [
        (f"/inventory/pages/{UNKNOWN}", "page_not_found"),
        (f"/inventory/pages/{UNKNOWN}/versions", "page_not_found"),
        (f"/inventory/pages/{UNKNOWN}/versions/1", "page_not_found"),
        (f"/inventory/pages/{UNKNOWN}/links", "page_not_found"),
        (f"/inventory/pages/{PAGE_ID}/versions/7", "not_found"),
        (f"/inventory/runs/{UNKNOWN}", "not_found"),
        (f"/inventory/runs/{UNKNOWN}/issues", "not_found"),
    ],
)
def test_unknown_resources_are_404_without_echoing_the_id(
    client: TestClient, path: str, code: str
) -> None:
    response = client.get(path)
    assert response.status_code == 404
    body = ErrorResponse.model_validate(response.json())
    assert body.code.value == code
    assert str(UNKNOWN) not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/inventory/pages/not-a-uuid",
        "/inventory/runs/not-a-uuid",
        "/inventory/pages?limit=0",
        "/inventory/pages?limit=201",
        "/inventory/pages?offset=-1",
        "/inventory/pages?publication_state=secret",
        f"/inventory/pages/{PAGE_ID}/versions/0",
        "/inventory/links?resolution=maybe",
        "/inventory/links?link_type=weird",
        "/inventory/links?target_page_id=nope",
        "/inventory/runs?status=paused",
        f"/inventory/runs/{RUN_ID}/issues?severity=fatal",
    ],
)
def test_invalid_parameters_are_422_error_responses(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 422
    body = ErrorResponse.model_validate(response.json())
    assert body.code.value == "validation_failed"
    assert body.details


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize(
    "path",
    [
        "/inventory/pages",
        f"/inventory/pages/{PAGE_ID}",
        f"/inventory/pages/{PAGE_ID}/versions/1",
        "/inventory/links",
        "/inventory/runs",
        f"/inventory/runs/{RUN_ID}",
    ],
)
def test_there_are_no_mutation_routes(client: TestClient, method: str, path: str) -> None:
    response = client.request(method.upper(), path)
    assert response.status_code == 405
    ErrorResponse.model_validate(response.json())


# --------------------------------------------------------------------------- #
# The real dependency resolves the SQL reader by name
# --------------------------------------------------------------------------- #
async def test_default_dependency_builds_the_sql_reader() -> None:
    from ken_services.inventory.reader import SqlInventoryReader

    engine = create_engine(make_settings())  # creating an engine performs no I/O
    try:
        reader = get_inventory_reader(create_sessionmaker(engine))
        assert isinstance(reader, SqlInventoryReader)
        assert isinstance(reader, InventoryReader)
    finally:
        await engine.dispose()
