"""Content inventory repository: write-side SQL primitives (ADR-0009).

The caller owns the transaction: nothing here commits, so a page version, its
links, its ``known_urls`` rows, the run counters and the event describing the
change are written atomically by ``ken_services.inventory.ingest``.

There is deliberately no function that updates or deletes a ``page_versions``
or ``existing_links`` row; the migration 0002 trigger would refuse it anyway.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.inventory import (
    LinkType,
    NormalizedPage,
    RunCounters,
    RunStatus,
    SourceIssue,
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

RESOLVED_OUTCOMES: Final = frozenset({UrlOutcome.PAGE.value, UrlOutcome.REDIRECT.value})


@dataclass(frozen=True, slots=True)
class PageState:
    """The locked ``pages`` row fields ingestion decides on."""

    id: uuid.UUID
    canonical_url: str
    external_id: str | None
    current_version: int
    content_hash: str
    gone_at: datetime | None


def _state(row: Page | None) -> PageState | None:
    if row is None:
        return None
    return PageState(
        id=row.id,
        canonical_url=row.canonical_url,
        external_id=row.external_id,
        current_version=row.current_version,
        content_hash=row.content_hash,
        gone_at=row.gone_at,
    )


# --------------------------------------------------------------------------- #
# Runs and issues
# --------------------------------------------------------------------------- #
async def insert_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    source_type: str,
    source_name: str,
    config_snapshot: Mapping[str, Any],
    started_at: datetime,
) -> None:
    await session.execute(
        insert(IngestionRun).values(
            id=run_id,
            source_type=source_type,
            source_name=source_name,
            status=RunStatus.RUNNING.value,
            started_at=started_at,
            config_snapshot=dict(config_snapshot),
            created_at=started_at,
            updated_at=started_at,
        )
    )


async def update_run_counters(
    session: AsyncSession, *, run_id: uuid.UUID, counters: RunCounters, now: datetime
) -> None:
    await session.execute(
        update(IngestionRun)
        .where(IngestionRun.id == run_id)
        .values(**counters.model_dump(), updated_at=now)
    )


async def finish_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    status: RunStatus,
    counters: RunCounters,
    completed_at: datetime,
    failure_reason: str | None,
) -> None:
    await session.execute(
        update(IngestionRun)
        .where(IngestionRun.id == run_id)
        .values(
            **counters.model_dump(),
            status=status.value,
            completed_at=completed_at,
            failure_reason=failure_reason,
            updated_at=completed_at,
        )
    )


async def insert_issue(session: AsyncSession, *, run_id: uuid.UUID, issue: SourceIssue) -> None:
    await session.execute(
        insert(IngestionIssue).values(
            run_id=run_id,
            code=issue.code.value,
            severity=issue.severity.value,
            url=issue.url,
            related_url=issue.related_url,
            http_status=issue.http_status,
            message=issue.message,
            occurred_at=issue.occurred_at,
        )
    )


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
async def lock_page_by_external_id(session: AsyncSession, external_id: str) -> PageState | None:
    row = await session.scalar(
        select(Page).where(Page.external_id == external_id).with_for_update()
    )
    return _state(row)


async def lock_page_by_canonical_url(session: AsyncSession, url: str) -> PageState | None:
    row = await session.scalar(select(Page).where(Page.canonical_url == url).with_for_update())
    return _state(row)


async def lock_live_page_by_canonical_url(session: AsyncSession, url: str) -> PageState | None:
    """The page whose identity URL is ``url`` and which is not already gone."""
    row = await session.scalar(
        select(Page).where(Page.canonical_url == url, Page.gone_at.is_(None)).with_for_update()
    )
    return _state(row)


async def version_canonical_status(
    session: AsyncSession, *, page_id: uuid.UUID, version: int
) -> str | None:
    result = await session.scalar(
        select(PageVersion.canonical_status).where(
            PageVersion.page_id == page_id, PageVersion.version == version
        )
    )
    return None if result is None else str(result)


async def page_id_for_url(session: AsyncSession, url: str) -> uuid.UUID | None:
    """The page ``url`` currently leads to: via ``known_urls``, else as an identity URL."""
    known = await session.scalar(
        select(KnownUrl.page_id).where(KnownUrl.url == url, KnownUrl.outcome.in_(RESOLVED_OUTCOMES))
    )
    if known is not None:
        return known
    return await session.scalar(select(Page.id).where(Page.canonical_url == url))


def _link_counts(page: NormalizedPage) -> tuple[int, int]:
    internal = sum(1 for link in page.links if link.link_type is LinkType.INTERNAL)
    external = sum(1 for link in page.links if link.link_type is LinkType.EXTERNAL)
    return internal, external


def _page_fields(page: NormalizedPage, *, version: int) -> dict[str, Any]:
    internal, external = _link_counts(page)
    return {
        "current_version": version,
        "content_hash": page.content_hash,
        "title": page.title,
        "publication_state": page.publication_state.value,
        "page_type": page.page_type,
        "language": page.language,
        "word_count": page.word_count,
        "internal_link_count": internal,
        "external_link_count": external,
    }


async def insert_page(
    session: AsyncSession,
    *,
    page_id: uuid.UUID,
    canonical_url: str,
    external_id: str | None,
    page: NormalizedPage,
    now: datetime,
) -> None:
    """A new page pointing at version 1 (the deferred FK is checked at commit)."""
    await session.execute(
        insert(Page).values(
            id=page_id,
            canonical_url=canonical_url,
            external_id=external_id,
            **_page_fields(page, version=1),
            first_seen_at=now,
            last_seen_at=now,
            last_changed_at=now,
            gone_at=None,
            created_at=now,
            updated_at=now,
        )
    )


async def set_page_identity(
    session: AsyncSession,
    *,
    page_id: uuid.UUID,
    canonical_url: str,
    external_id: str | None,
    now: datetime,
) -> None:
    await session.execute(
        update(Page)
        .where(Page.id == page_id)
        .values(canonical_url=canonical_url, external_id=external_id, updated_at=now)
    )


async def set_page_current(
    session: AsyncSession,
    *,
    page_id: uuid.UUID,
    page: NormalizedPage,
    version: int,
    now: datetime,
) -> None:
    """Point the page at a newly written version; it is live again if it was gone."""
    await session.execute(
        update(Page)
        .where(Page.id == page_id)
        .values(
            **_page_fields(page, version=version),
            last_seen_at=now,
            last_changed_at=now,
            gone_at=None,
            updated_at=now,
        )
    )


async def touch_page(session: AsyncSession, *, page_id: uuid.UUID, now: datetime) -> None:
    """Seen again with unchanged content: advance last_seen_at, clear gone_at."""
    await session.execute(
        update(Page)
        .where(Page.id == page_id)
        .values(last_seen_at=now, gone_at=None, updated_at=now)
    )


async def mark_page_gone(session: AsyncSession, *, page_id: uuid.UUID, now: datetime) -> None:
    await session.execute(
        update(Page).where(Page.id == page_id).values(gone_at=now, updated_at=now)
    )


# --------------------------------------------------------------------------- #
# Immutable versions and links (insert only)
# --------------------------------------------------------------------------- #
async def insert_version(
    session: AsyncSession,
    *,
    page_id: uuid.UUID,
    version: int,
    page: NormalizedPage,
    run_id: uuid.UUID,
    now: datetime,
) -> None:
    internal, external = _link_counts(page)
    await session.execute(
        insert(PageVersion).values(
            id=uuid.uuid4(),
            page_id=page_id,
            version=version,
            content_hash=page.content_hash,
            hash_version=page.hash_version,
            run_id=run_id,
            source_type=page.source_type.value,
            source_id=page.source_id,
            requested_url=page.requested_url,
            url=page.url,
            redirect_chain=list(page.redirect_chain),
            http_status=page.http_status,
            canonical_url=page.canonical_url,
            declared_canonical_url=page.declared_canonical_url,
            canonical_status=page.canonical_status.value,
            title=page.title,
            meta_description=page.meta_description,
            language=page.language,
            publication_state=page.publication_state.value,
            page_type=page.page_type,
            published_at=page.published_at,
            modified_at=page.modified_at,
            robots_directives=list(page.robots_directives),
            page_metadata=dict(page.metadata),
            headings=[heading.model_dump(mode="json") for heading in page.headings],
            body_html=page.body_html,
            body_text=page.body_text,
            word_count=page.word_count,
            internal_link_count=internal,
            external_link_count=external,
            fetched_at=page.fetched_at,
            created_at=now,
        )
    )
    if page.links:
        await session.execute(
            insert(ExistingLink),
            [
                {
                    "page_id": page_id,
                    "page_version": version,
                    "ordinal": link.ordinal,
                    "href": link.href,
                    "resolved_url": link.resolved_url,
                    "link_type": link.link_type.value,
                    "anchor_text": link.anchor_text,
                    "anchor_source": link.anchor_source,
                    "rel": list(link.rel),
                    "block_id": link.block_id,
                    "block_kind": link.block_kind,
                    "occurrence_index": link.occurrence_index,
                    "context_text": link.context_text,
                    "context_hash": link.context_hash,
                    "created_at": now,
                }
                for link in page.links
            ],
        )


# --------------------------------------------------------------------------- #
# Known URLs
# --------------------------------------------------------------------------- #
async def upsert_known_url(
    session: AsyncSession,
    *,
    url: str,
    outcome: UrlOutcome,
    page_id: uuid.UUID | None,
    http_status: int | None,
    run_id: uuid.UUID,
    now: datetime,
) -> None:
    """Record the latest observation of ``url``.

    A failure outcome passes ``page_id=None`` and KEEPS the stored page (the
    last page this URL led to), so a transient failure never drops a mapping.
    """
    statement = insert(KnownUrl).values(
        url=url,
        page_id=page_id,
        outcome=outcome.value,
        http_status=http_status,
        last_run_id=run_id,
        first_seen_at=now,
        last_seen_at=now,
        updated_at=now,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[KnownUrl.url],
        set_={
            "page_id": func.coalesce(statement.excluded.page_id, KnownUrl.page_id),
            "outcome": statement.excluded.outcome,
            "http_status": statement.excluded.http_status,
            "last_run_id": statement.excluded.last_run_id,
            "last_seen_at": statement.excluded.last_seen_at,
            "updated_at": statement.excluded.updated_at,
        },
    )
    await session.execute(statement)


async def insert_known_url_if_absent(
    session: AsyncSession,
    *,
    url: str,
    page_id: uuid.UUID,
    run_id: uuid.UUID,
    now: datetime,
) -> None:
    """Map an identity URL that was declared but not itself observed.

    An existing row is a real observation of that URL (possibly a 404) and is
    left untouched.
    """
    await session.execute(
        insert(KnownUrl)
        .values(
            url=url,
            page_id=page_id,
            outcome=UrlOutcome.PAGE.value,
            http_status=None,
            last_run_id=run_id,
            first_seen_at=now,
            last_seen_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[KnownUrl.url])
    )


async def upsert_known_urls(
    session: AsyncSession,
    *,
    observations: Sequence[tuple[str, UrlOutcome, int | None]],
    page_id: uuid.UUID,
    run_id: uuid.UUID,
    now: datetime,
) -> None:
    for url, outcome, http_status in observations:
        await upsert_known_url(
            session,
            url=url,
            outcome=outcome,
            page_id=page_id,
            http_status=http_status,
            run_id=run_id,
            now=now,
        )
