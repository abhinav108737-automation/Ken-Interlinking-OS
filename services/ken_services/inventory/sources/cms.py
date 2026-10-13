"""``CMSContentSource``: an inventory source over a READ-ONLY ``CMSInventoryReader``.

ADR-0009 section 1. The source only ever lists and reads: it holds a
:class:`~ken_contracts.inventory.CMSInventoryReader`, which by construction has
no method that can mutate content (it is deliberately not a ``CMSProvider``).

There is no real CMS reader in Phase 1: the real read API and least-privilege
read credentials are an ASK of the user. :class:`MockCMSInventoryReader` serves
the deterministic Phase 0 seed pages for development and tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import ValidationError

from ken_contracts.cms import CMSError, CMSNotFoundError, CMSPage
from ken_contracts.inventory import (
    CMSInventoryReader,
    CMSPageRef,
    CMSPageRefPage,
    IssueCode,
    IssueSeverity,
    PublicationState,
    SiteScope,
    SourceAssertions,
    SourceDocument,
    SourceIssue,
    SourceSummary,
    SourceType,
)
from ken_core.providers.cms.seed import seed_pages
from ken_services.inventory.urls import (
    InvalidUrlError,
    build_scope,
    in_scope,
    normalize_netloc,
    try_normalize_url,
)

__all__ = ["CMSContentSource", "MockCMSInventoryReader"]

MAX_PAGE_SIZE: Final = 1000


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_or_none(value: datetime | None) -> datetime | None:
    """A naive timestamp's zone is unknown; it is dropped rather than guessed."""
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value


def _metadata(values: dict[str, Any]) -> dict[str, str]:
    """Scalar CMS metadata as strings; nested or null values are not carried."""
    out: dict[str, str] = {}
    for key, value in values.items():
        if isinstance(value, bool):
            out[str(key)] = "true" if value else "false"
        elif isinstance(value, str | int | float):
            out[str(key)] = str(value)
    return out


class CMSContentSource:
    """:class:`~ken_contracts.inventory.ContentSourceProvider` over a CMS read API.

    Listing errors propagate (the source cannot continue and the run fails);
    a per-page read error becomes a :class:`SourceIssue`. The source takes
    ownership of ``reader`` and closes it in :meth:`aclose`.
    """

    source_type: SourceType = SourceType.CMS

    def __init__(
        self,
        reader: CMSInventoryReader,
        *,
        site_url: str,
        allowed_hosts: Sequence[str] = (),
        page_size: int = 100,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        self._reader = reader
        self.name: str = f"cms:{reader.name}"
        self._site_url = site_url
        self._allowed_hosts = tuple(allowed_hosts)
        self._page_size = page_size
        self._clock = clock
        self._scope: SiteScope | None = None
        self._discovered = 0
        self._skipped = 0
        self._closed = False

    def config_snapshot(self) -> dict[str, Any]:
        """Secret-free: nothing from the reader beyond its name; URLs normalised."""
        hosts: set[str] = set()
        for host in self._allowed_hosts:
            try:
                hosts.add(normalize_netloc(host))
            except InvalidUrlError:
                continue
        return {
            "reader": self._reader.name,
            # try_normalize_url refuses credentials, so none can reach the snapshot.
            "site_url": try_normalize_url(self._site_url),
            "allowed_hosts": sorted(hosts),
            "page_size": self._page_size,
        }

    async def site_scope(self) -> SiteScope:
        if self._scope is None:
            self._scope = build_scope(self._site_url, self._allowed_hosts)
        return self._scope

    def _issue(
        self,
        code: IssueCode,
        severity: IssueSeverity,
        message: str,
        *,
        url: str | None = None,
    ) -> SourceIssue:
        return SourceIssue(
            code=code, severity=severity, url=url, message=message, occurred_at=self._clock()
        )

    async def _read(self, ref: CMSPageRef, scope: SiteScope) -> SourceDocument | SourceIssue:
        ref_url = try_normalize_url(ref.url)
        try:
            page = await self._reader.get_page(page_id=ref.page_id)
        except CMSNotFoundError:
            return self._issue(
                IssueCode.HTTP_NOT_FOUND,
                IssueSeverity.WARNING,
                "CMS page listed but no longer found",
                url=ref_url,
            )
        except CMSError as exc:
            # The class name only: exception text may carry CMS internals.
            return self._issue(
                IssueCode.NETWORK_ERROR,
                IssueSeverity.ERROR,
                f"CMS page read failed: {type(exc).__name__}",
                url=ref_url,
            )

        url = try_normalize_url(page.url)
        if url is None:
            return self._issue(
                IssueCode.INVALID_URL,
                IssueSeverity.ERROR,
                "CMS page url is not a valid http(s) URL",
            )
        if not in_scope(url, scope):
            self._skipped += 1
            return self._issue(
                IssueCode.OUT_OF_SCOPE,
                IssueSeverity.WARNING,
                "CMS page url is outside the site scope",
                url=url,
            )
        try:
            return SourceDocument(
                source_type=SourceType.CMS,
                source_id=page.page_id,
                requested_url=url,
                final_url=url,
                http_status=None,
                fetched_at=self._clock(),
                html=page.body_html,
                assertions=SourceAssertions(
                    external_id=page.page_id,
                    title=page.title,
                    canonical_url=page.canonical_url,
                    publication_state=(
                        PublicationState.PUBLISHED
                        if page.published_at is not None
                        else PublicationState.UNKNOWN
                    ),
                    published_at=_aware_or_none(page.published_at),
                    modified_at=_aware_or_none(page.updated_at),
                    metadata=_metadata(page.metadata),
                ),
            )
        except ValidationError as exc:
            types = ", ".join(sorted({str(error["type"]) for error in exc.errors()}))
            return self._issue(
                IssueCode.INVALID_RECORD,
                IssueSeverity.ERROR,
                f"CMS page is not a valid source document: {types}"[:500],
                url=url,
            )

    async def iter_records(self) -> AsyncIterator[SourceDocument | SourceIssue]:
        scope = await self.site_scope()
        cursor: str | None = None
        used: set[str | None] = {None}
        while True:
            listing = await self._reader.list_pages(cursor=cursor, limit=self._page_size)
            for ref in listing.items:
                self._discovered += 1
                yield await self._read(ref, scope)
            next_cursor = listing.next_cursor
            if next_cursor is None:
                return
            if next_cursor in used:
                yield self._issue(
                    IssueCode.INVALID_RECORD,
                    IssueSeverity.ERROR,
                    "CMS listing returned a cursor it had already returned; listing stopped",
                )
                return
            used.add(next_cursor)
            cursor = next_cursor

    async def summary(self) -> SourceSummary:
        return SourceSummary(discovered=self._discovered, skipped=self._skipped)

    async def aclose(self) -> None:
        """Idempotent. Closes the reader this source owns."""
        if self._closed:
            return
        self._closed = True
        await self._reader.aclose()


class MockCMSInventoryReader:
    """Deterministic, READ-ONLY :class:`CMSInventoryReader` over fixed pages.

    Defaults to the Phase 0 seed pages. Pages are kept in the order given;
    cursors are opaque offsets. Every read returns a deep copy, so a caller can
    never change what the reader serves. There is no mutation method.
    """

    name: str = "mock"

    def __init__(self, pages: Sequence[CMSPage] | None = None) -> None:
        source = seed_pages() if pages is None else pages
        self._pages: tuple[CMSPage, ...] = tuple(page.model_copy(deep=True) for page in source)
        self._by_id: dict[str, CMSPage] = {page.page_id: page for page in self._pages}

    async def list_pages(self, *, cursor: str | None = None, limit: int = 100) -> CMSPageRefPage:
        if limit < 1:
            raise ValueError("limit must be positive")
        start = 0
        if cursor is not None:
            if not cursor.isdigit() or int(cursor) > len(self._pages):
                raise CMSError("invalid cursor")
            start = int(cursor)
        chunk = self._pages[start : start + limit]
        end = start + len(chunk)
        return CMSPageRefPage(
            items=tuple(
                CMSPageRef(page_id=page.page_id, url=page.url, version=page.version)
                for page in chunk
            ),
            next_cursor=str(end) if end < len(self._pages) else None,
        )

    async def get_page(self, *, page_id: str) -> CMSPage:
        page = self._by_id.get(page_id)
        if page is None:
            raise CMSNotFoundError("page not found")
        return page.model_copy(deep=True)

    async def aclose(self) -> None:
        """Nothing to release; idempotent."""
