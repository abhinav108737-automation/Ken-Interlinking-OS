"""SQL implementation of :class:`ken_contracts.inventory.InventoryReader` (read-only).

The API resolves this module by name (``ken_api.deps.INVENTORY_READER``) and
builds ``SqlInventoryReader(sessionmaker)`` per request. Nothing here writes.

Link resolution is computed AT READ TIME (ADR-0009 section 5) by joining
``existing_links.resolved_url`` to ``known_urls``:

* a non-internal link is ``not_applicable``;
* the URL's latest outcome is ``not_found``/``gone`` -> ``broken``;
* the URL has led to a page (``known_urls.page_id``, kept across later
  transient failures -- ADR-0009 section 9) -> ``target_page_id``; ``broken``
  if that page is gone, else ``resolved``. A later robots block or timeout on
  the URL therefore does not make an established link flap to ``unresolved``;
* anything else, or no row -> ``unresolved``.

The resolution is a SQL expression, so filtering by it happens before paging
and ``PageInfo.total`` is exact.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import ColumnElement, Select, and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from ken_contracts.errors import ListResponse, PageInfo
from ken_contracts.inventory import (
    CanonicalStatus,
    ExistingLinkView,
    Heading,
    IngestionIssueView,
    IngestionRunDetail,
    IngestionRunSummary,
    InventoryNotFoundError,
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
    UrlOutcome,
)
from ken_core.db.models.inventory import (
    ExistingLink,
    IngestionIssue,
    IngestionRun,
    KnownUrl,
    Page,
    PageVersion,
)

MAX_LIMIT: Final = 200
_RESOLVED_OUTCOMES: Final = (UrlOutcome.PAGE.value, UrlOutcome.REDIRECT.value)
_BROKEN_OUTCOMES: Final = (UrlOutcome.NOT_FOUND.value, UrlOutcome.GONE.value)
_COUNTER_FIELDS: Final = tuple(RunCounters.model_fields)


def _check_paging(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        raise ValueError("offset must be >= 0")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------- #
# Row -> contract model
# --------------------------------------------------------------------------- #
def _page_fields(row: Page) -> dict[str, Any]:
    return {
        "id": row.id,
        "canonical_url": row.canonical_url,
        "external_id": row.external_id,
        "title": row.title,
        "publication_state": PublicationState(row.publication_state),
        "page_type": row.page_type,
        "language": row.language,
        "current_version": row.current_version,
        "content_hash": row.content_hash,
        "word_count": row.word_count,
        "internal_link_count": row.internal_link_count,
        "external_link_count": row.external_link_count,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "last_changed_at": row.last_changed_at,
        "gone_at": row.gone_at,
    }


def _version_fields(row: PageVersion) -> dict[str, Any]:
    return {
        "page_id": row.page_id,
        "version": row.version,
        "content_hash": row.content_hash,
        "hash_version": row.hash_version,
        "run_id": row.run_id,
        "source_type": SourceType(row.source_type),
        "source_id": row.source_id,
        "url": row.url,
        "http_status": row.http_status,
        "title": row.title,
        "publication_state": PublicationState(row.publication_state),
        "word_count": row.word_count,
        "internal_link_count": row.internal_link_count,
        "external_link_count": row.external_link_count,
        "fetched_at": row.fetched_at,
        "created_at": row.created_at,
    }


def _version_detail(row: PageVersion) -> PageVersionDetail:
    return PageVersionDetail(
        **_version_fields(row),
        requested_url=row.requested_url,
        redirect_chain=list(row.redirect_chain),
        canonical_url=row.canonical_url,
        declared_canonical_url=row.declared_canonical_url,
        canonical_status=CanonicalStatus(row.canonical_status),
        meta_description=row.meta_description,
        language=row.language,
        page_type=row.page_type,
        published_at=row.published_at,
        modified_at=row.modified_at,
        robots_directives=list(row.robots_directives),
        metadata=dict(row.page_metadata),
        headings=[Heading.model_validate(item) for item in row.headings],
        body_text=row.body_text,
        body_html=row.body_html,
    )


def _counters(row: IngestionRun) -> RunCounters:
    return RunCounters(**{name: getattr(row, name) for name in _COUNTER_FIELDS})


def _run_fields(row: IngestionRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_type": SourceType(row.source_type),
        "source_name": row.source_name,
        "status": RunStatus(row.status),
        "started_at": row.started_at,
        "completed_at": row.completed_at,
        "counters": _counters(row),
    }


# --------------------------------------------------------------------------- #
# Link query with read-time resolution
# --------------------------------------------------------------------------- #
_known = aliased(KnownUrl, name="known")
_target = aliased(Page, name="target")
_source = aliased(Page, name="source_page")

_is_internal: Final = ExistingLink.link_type == LinkType.INTERNAL.value
# "Has led to a page": the last page this URL mapped to, whatever its latest
# (possibly transient) outcome. A definitive 404/410 is checked first.
_leads_to_page: Final = _target.id.is_not(None)

_resolution: Final[ColumnElement[str]] = case(
    (~_is_internal, literal(LinkResolution.NOT_APPLICABLE.value)),
    (_known.outcome.in_(_BROKEN_OUTCOMES), literal(LinkResolution.BROKEN.value)),
    (
        and_(_leads_to_page, _target.gone_at.is_not(None)),
        literal(LinkResolution.BROKEN.value),
    ),
    (_leads_to_page, literal(LinkResolution.RESOLVED.value)),
    else_=literal(LinkResolution.UNRESOLVED.value),
)
_target_page_id: Final[ColumnElement[uuid.UUID | None]] = case(
    (and_(_is_internal, _leads_to_page), _target.id), else_=None
)


def _links_select() -> Select[*tuple[Any, ...]]:
    return (
        select(
            ExistingLink,
            _source.canonical_url.label("source_url"),
            _resolution.label("resolution"),
            _target_page_id.label("target_page_id"),
        )
        .join(_source, _source.id == ExistingLink.page_id)
        .outerjoin(_known, and_(_is_internal, _known.url == ExistingLink.resolved_url))
        .outerjoin(_target, _target.id == _known.page_id)
    )


def _link_view(row: Any) -> ExistingLinkView:
    link: ExistingLink = row[0]
    return ExistingLinkView(
        page_id=link.page_id,
        page_version=link.page_version,
        source_url=row.source_url,
        ordinal=link.ordinal,
        href=link.href,
        resolved_url=link.resolved_url,
        link_type=LinkType(link.link_type),
        resolution=LinkResolution(row.resolution),
        target_page_id=row.target_page_id,
        anchor_text=link.anchor_text,
        anchor_source=link.anchor_source,  # Literal re-checked by pydantic
        rel=list(link.rel),
        block_id=link.block_id,
        block_kind=link.block_kind,
        occurrence_index=link.occurrence_index,
        context_text=link.context_text,
    )


async def _total(session: AsyncSession, statement: Select[*tuple[Any, ...]]) -> int:
    count = await session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery())
    )
    return int(count or 0)


class SqlInventoryReader:
    """``InventoryReader`` over PostgreSQL. One short read session per call."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    # ----------------------------------------------------------------- pages
    async def list_pages(
        self,
        *,
        limit: int,
        offset: int,
        search: str | None = None,
        publication_state: PublicationState | None = None,
    ) -> ListResponse[PageSummary]:
        _check_paging(limit, offset)
        statement = select(Page)
        if search:
            pattern = f"%{_escape_like(search)}%"
            statement = statement.where(
                or_(
                    Page.title.ilike(pattern, escape="\\"),
                    Page.canonical_url.ilike(pattern, escape="\\"),
                )
            )
        if publication_state is not None:
            statement = statement.where(
                Page.publication_state == PublicationState(publication_state).value
            )
        async with self._sessionmaker() as session:
            total = await _total(session, statement)
            rows = (
                await session.scalars(
                    statement.order_by(Page.last_changed_at.desc(), Page.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        return ListResponse[PageSummary](
            items=[PageSummary(**_page_fields(row)) for row in rows],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    async def get_page(self, *, page_id: uuid.UUID) -> PageDetail:
        async with self._sessionmaker() as session:
            row = await session.execute(
                select(Page, PageVersion)
                .join(
                    PageVersion,
                    and_(
                        PageVersion.page_id == Page.id,
                        PageVersion.version == Page.current_version,
                    ),
                )
                .where(Page.id == page_id)
            )
            found = row.one_or_none()
        if found is None:
            raise InventoryNotFoundError("page")
        page, version = found
        return PageDetail(**_page_fields(page), current=_version_detail(version))

    async def _require_page(self, session: AsyncSession, page_id: uuid.UUID) -> int:
        """The page's current version, or InventoryNotFoundError("page")."""
        current = await session.scalar(select(Page.current_version).where(Page.id == page_id))
        if current is None:
            raise InventoryNotFoundError("page")
        return int(current)

    async def list_page_versions(
        self, *, page_id: uuid.UUID, limit: int, offset: int
    ) -> ListResponse[PageVersionSummary]:
        _check_paging(limit, offset)
        statement = select(PageVersion).where(PageVersion.page_id == page_id)
        async with self._sessionmaker() as session:
            await self._require_page(session, page_id)
            total = await _total(session, statement)
            rows = (
                await session.scalars(
                    statement.order_by(PageVersion.version.desc()).limit(limit).offset(offset)
                )
            ).all()
        return ListResponse[PageVersionSummary](
            items=[PageVersionSummary(**_version_fields(row)) for row in rows],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    async def get_page_version(self, *, page_id: uuid.UUID, version: int) -> PageVersionDetail:
        async with self._sessionmaker() as session:
            await self._require_page(session, page_id)
            row = await session.scalar(
                select(PageVersion).where(
                    PageVersion.page_id == page_id, PageVersion.version == version
                )
            )
        if row is None:
            raise InventoryNotFoundError("page_version")
        return _version_detail(row)

    # ----------------------------------------------------------------- links
    async def list_page_links(
        self,
        *,
        page_id: uuid.UUID,
        limit: int,
        offset: int,
        version: int | None = None,
        link_type: LinkType | None = None,
    ) -> ListResponse[ExistingLinkView]:
        _check_paging(limit, offset)
        async with self._sessionmaker() as session:
            current = await self._require_page(session, page_id)
            if version is None:
                version = current
            else:
                exists = await session.scalar(
                    select(PageVersion.id).where(
                        PageVersion.page_id == page_id, PageVersion.version == version
                    )
                )
                if exists is None:
                    raise InventoryNotFoundError("page_version")
            statement = _links_select().where(
                ExistingLink.page_id == page_id, ExistingLink.page_version == version
            )
            if link_type is not None:
                statement = statement.where(ExistingLink.link_type == LinkType(link_type).value)
            total = await _total(session, statement)
            rows = (
                await session.execute(
                    statement.order_by(ExistingLink.ordinal, ExistingLink.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        return ListResponse[ExistingLinkView](
            items=[_link_view(row) for row in rows],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    async def list_links(
        self,
        *,
        limit: int,
        offset: int,
        link_type: LinkType | None = None,
        resolution: LinkResolution | None = None,
        target_page_id: uuid.UUID | None = None,
    ) -> ListResponse[ExistingLinkView]:
        _check_paging(limit, offset)
        # Current versions only.
        statement = _links_select().where(ExistingLink.page_version == _source.current_version)
        if link_type is not None:
            statement = statement.where(ExistingLink.link_type == LinkType(link_type).value)
        if resolution is not None:
            statement = statement.where(_resolution == LinkResolution(resolution).value)
        if target_page_id is not None:
            statement = statement.where(_target_page_id == target_page_id)
        async with self._sessionmaker() as session:
            total = await _total(session, statement)
            rows = (
                await session.execute(
                    statement.order_by(_source.canonical_url, ExistingLink.ordinal, ExistingLink.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        return ListResponse[ExistingLinkView](
            items=[_link_view(row) for row in rows],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    # ------------------------------------------------------------------ runs
    async def list_runs(
        self,
        *,
        limit: int,
        offset: int,
        source_type: SourceType | None = None,
        status: RunStatus | None = None,
    ) -> ListResponse[IngestionRunSummary]:
        _check_paging(limit, offset)
        statement = select(IngestionRun)
        if source_type is not None:
            statement = statement.where(IngestionRun.source_type == SourceType(source_type).value)
        if status is not None:
            statement = statement.where(IngestionRun.status == RunStatus(status).value)
        async with self._sessionmaker() as session:
            total = await _total(session, statement)
            rows = (
                await session.scalars(
                    statement.order_by(IngestionRun.started_at.desc(), IngestionRun.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        return ListResponse[IngestionRunSummary](
            items=[IngestionRunSummary(**_run_fields(row)) for row in rows],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    async def get_run(self, *, run_id: uuid.UUID) -> IngestionRunDetail:
        async with self._sessionmaker() as session:
            row = await session.scalar(select(IngestionRun).where(IngestionRun.id == run_id))
            if row is None:
                raise InventoryNotFoundError("run")
            counts = (
                await session.execute(
                    select(IngestionIssue.code, func.count())
                    .where(IngestionIssue.run_id == run_id)
                    .group_by(IngestionIssue.code)
                    .order_by(IngestionIssue.code)
                )
            ).all()
        return IngestionRunDetail(
            **_run_fields(row),
            config_snapshot=dict(row.config_snapshot),
            failure_reason=row.failure_reason,
            issue_counts={str(code): int(count) for code, count in counts},
        )

    async def list_run_issues(
        self,
        *,
        run_id: uuid.UUID,
        limit: int,
        offset: int,
        severity: IssueSeverity | None = None,
    ) -> ListResponse[IngestionIssueView]:
        _check_paging(limit, offset)
        statement = select(IngestionIssue).where(IngestionIssue.run_id == run_id)
        if severity is not None:
            statement = statement.where(IngestionIssue.severity == IssueSeverity(severity).value)
        async with self._sessionmaker() as session:
            exists = await session.scalar(select(IngestionRun.id).where(IngestionRun.id == run_id))
            if exists is None:
                raise InventoryNotFoundError("run")
            total = await _total(session, statement)
            rows = (
                await session.scalars(
                    statement.order_by(IngestionIssue.id).limit(limit).offset(offset)
                )
            ).all()
        return ListResponse[IngestionIssueView](
            items=[
                IngestionIssueView(
                    id=row.id,
                    run_id=row.run_id,
                    code=IssueCode(row.code),
                    severity=IssueSeverity(row.severity),
                    url=row.url,
                    related_url=row.related_url,
                    http_status=row.http_status,
                    message=row.message,
                    occurred_at=row.occurred_at,
                )
                for row in rows
            ],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )
