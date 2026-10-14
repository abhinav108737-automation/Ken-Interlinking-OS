"""Integration fixtures. Every test here runs against the ``*_test`` database only.

The skip-when-unreachable and ``_test``-name guard live in tests/conftest.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, TypeVar

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from ken_contracts.inventory import (
    CanonicalStatus,
    ExtractedLink,
    IngestionRunDetail,
    IssueCode,
    IssueSeverity,
    LinkType,
    NormalizedPage,
    PublicationState,
    SiteScope,
    SourceDocument,
    SourceIssue,
    SourceSummary,
    SourceType,
)
from ken_core.db.engine import create_engine as create_async_app_engine
from ken_core.db.session import create_sessionmaker
from tests.conftest import make_settings

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

TRUNCATE_ALL = (
    "TRUNCATE link_candidates, link_plan_transitions, link_plans, opportunities, "
    "page_relationships, page_protections, page_embeddings, page_analyses, job_runs, "
    "existing_links, known_urls, page_versions, pages, ingestion_issues, "
    "ingestion_runs, events RESTART IDENTITY CASCADE"
)


@pytest.fixture(scope="session")
def alembic_ini_path() -> str:
    return str(ALEMBIC_INI)


@pytest.fixture
def alembic_config(test_database_url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    # configparser interpolation: a literal % in the URL must be doubled.
    config.set_main_option("sqlalchemy.url", test_database_url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    return config


T = TypeVar("T")


class SyncDb:
    """Run synchronous inspection code against the test DB over asyncpg.

    asyncpg is async-only, so there is no synchronous SQLAlchemy engine. Each
    ``run`` opens a short-lived async engine inside ``asyncio.run`` and hands
    ``fn`` a sync-facing Connection via ``run_sync`` -- which is what
    ``inspect()``, ``MigrationContext`` and ``compare_metadata`` need.

    Tests that use this stay SYNCHRONOUS on purpose: they also call Alembic
    commands, and the async migrations/env.py runs its own ``asyncio.run``,
    which would fail inside an already-running event loop.
    """

    def __init__(self, url: str) -> None:
        self._url = url

    def run(self, fn: Callable[[Connection], T]) -> T:
        async def _go() -> T:
            engine = create_async_engine(self._url, poolclass=NullPool)
            try:
                async with engine.begin() as connection:
                    return await connection.run_sync(fn)
            finally:
                await engine.dispose()

        return asyncio.run(_go())


@pytest.fixture
def sync_db(test_database_url: str) -> SyncDb:
    return SyncDb(test_database_url)


@pytest.fixture
def migrated(alembic_config: Config, sync_db: SyncDb) -> Iterator[None]:
    """Test database at head, with every application table empty.

    TRUNCATE, not DELETE: the immutability triggers on page_versions,
    existing_links, page_analyses, page_embeddings, link_plans,
    link_plan_transitions and link_candidates are row triggers, which TRUNCATE
    does not fire.
    """
    command.upgrade(alembic_config, "head")
    sync_db.run(lambda connection: connection.execute(text(TRUNCATE_ALL)))
    yield


@pytest.fixture
async def async_engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_app_engine(make_settings(database_url=test_database_url))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def sessionmaker(async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(async_engine)


# --------------------------------------------------------------------------- #
# Content inventory: scripted source + fake normaliser (no real parsing)
# --------------------------------------------------------------------------- #
# Ingestion and reader tests must not depend on the real normaliser (a
# separate workstream): pages are built directly as NormalizedPage objects and
# the fake normaliser hands back the page scripted for each document.
SITE = "https://site.test"
SCOPE = SiteScope(allowed_hosts=frozenset({"site.test"}))
T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


class TickClock:
    """Deterministic, strictly increasing clock (1 s per call)."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def url(path: str) -> str:
    return f"{SITE}{path}"


def make_link(
    ordinal: int,
    target: str | None,
    *,
    link_type: LinkType = LinkType.INTERNAL,
    anchor: str = "anchor",
    href: str | None = None,
) -> ExtractedLink:
    return ExtractedLink(
        ordinal=ordinal,
        href=href if href is not None else (target or "mailto:x@site.test"),
        resolved_url=target,
        link_type=link_type,
        anchor_text=anchor,
        anchor_source="text",
        rel=(),
        block_id=f"b{ordinal}",
        block_kind="p",
        occurrence_index=0,
        context_text=f"context {ordinal}",
        context_hash=sha256(f"context {ordinal}".encode()).hexdigest(),
    )


def make_page(
    path: str,
    *,
    body: str = "body",
    title: str | None = "Title",
    canonical: str | None = None,
    status: CanonicalStatus | None = None,
    external_id: str | None = None,
    links: tuple[ExtractedLink, ...] = (),
    requested: str | None = None,
    redirect_chain: tuple[str, ...] = (),
    publication_state: PublicationState = PublicationState.PUBLISHED,
    page_type: str | None = None,
    source_type: SourceType = SourceType.FILE_EXPORT,
    metadata: dict[str, str] | None = None,
) -> NormalizedPage:
    """A NormalizedPage served at ``path``; ``canonical`` (a path) makes it an alias."""
    own = url(path)
    identity = url(canonical) if canonical is not None else own
    if status is None:
        status = CanonicalStatus.DECLARED if identity != own else CanonicalStatus.SELF
    digest = sha256(
        "|".join(
            [title or "", identity, body, *(f"{link.href}>{link.anchor_text}" for link in links)]
        ).encode()
    ).hexdigest()
    return NormalizedPage(
        source_type=source_type,
        source_id=own,
        external_id=external_id,
        requested_url=requested if requested is not None else own,
        url=own,
        redirect_chain=redirect_chain,
        http_status=200,
        fetched_at=T0,
        canonical_url=identity,
        declared_canonical_url=identity if status is not CanonicalStatus.MISSING else None,
        canonical_status=status,
        title=title,
        meta_description=None,
        language="en",
        publication_state=publication_state,
        page_type=page_type,
        metadata=metadata or {},
        body_html=f"<p>{body}</p>",
        body_text=body,
        word_count=len(body.split()),
        links=links,
        content_hash=digest,
    )


def make_issue(
    code: IssueCode,
    target: str | None,
    *,
    severity: IssueSeverity = IssueSeverity.WARNING,
    http_status: int | None = None,
    related_url: str | None = None,
    message: str = "scripted issue",
) -> SourceIssue:
    return SourceIssue(
        code=code,
        severity=severity,
        url=target,
        related_url=related_url,
        http_status=http_status,
        message=message,
        occurred_at=T0,
    )


class Boom:
    """A scripted failure: the normaliser (by default) or the source raises ``exc``."""

    def __init__(self, exc: BaseException, *, in_source: bool = False, path: str = "/boom") -> None:
        self.exc = exc
        self.in_source = in_source
        self.path = path


class FakeNormalizer:
    """Returns the page scripted for a document (keyed by the document's html marker)."""

    def __init__(self) -> None:
        self.script: dict[str, NormalizedPage | BaseException] = {}
        self.calls = 0

    def __call__(self, doc: SourceDocument, /, *, scope: SiteScope) -> NormalizedPage:
        self.calls += 1
        assert scope == SCOPE
        result = self.script[doc.html]
        if isinstance(result, BaseException):
            raise result
        return result


class FakeSource:
    """A scripted ``ContentSourceProvider``."""

    name = "fake-source"
    source_type = SourceType.FILE_EXPORT

    def __init__(
        self,
        items: list[Any],
        normalizer: FakeNormalizer,
        *,
        scope_error: BaseException | None = None,
        snapshot: dict[str, Any] | None = None,
        discovered: int | None = None,
        skipped: int = 0,
    ) -> None:
        self.items = items
        self.normalizer = normalizer
        self.scope_error = scope_error
        self.snapshot = snapshot if snapshot is not None else {"path": "export.jsonl", "n": 1}
        self.discovered = discovered
        self.skipped = skipped
        self.closed = 0
        self.iterated = False

    def config_snapshot(self) -> dict[str, Any]:
        return self.snapshot

    async def site_scope(self) -> SiteScope:
        if self.scope_error is not None:
            raise self.scope_error
        return SCOPE

    async def iter_records(self) -> AsyncIterator[SourceDocument | SourceIssue]:
        self.iterated = True
        for index, item in enumerate(self.items):
            if isinstance(item, SourceIssue):
                yield item
                continue
            if isinstance(item, Boom) and item.in_source:
                raise item.exc
            marker = f"doc-{index}"
            if isinstance(item, Boom):
                self.normalizer.script[marker] = item.exc
                final = url(item.path)
            else:
                self.normalizer.script[marker] = item
                final = item.url
            yield SourceDocument(
                source_type=self.source_type,
                source_id=final,
                requested_url=final,
                final_url=final,
                http_status=200,
                fetched_at=T0,
                html=marker,
            )

    async def summary(self) -> SourceSummary:
        count = sum(1 for item in self.items if not isinstance(item, SourceIssue))
        return SourceSummary(
            discovered=self.discovered if self.discovered is not None else count,
            skipped=self.skipped,
        )

    async def aclose(self) -> None:
        self.closed += 1


async def ingest(
    sessionmaker: async_sessionmaker[AsyncSession],
    *items: Any,
    clock: Callable[[], datetime] | None = None,
    **kwargs: Any,
) -> IngestionRunDetail:
    """Run one ingestion of a scripted source; asserts the counter invariant."""
    from ken_services.inventory.ingest import run_ingestion

    normalizer = FakeNormalizer()
    source_kwargs = {
        key: kwargs.pop(key)
        for key in ("scope_error", "snapshot", "discovered", "skipped")
        if key in kwargs
    }
    source = FakeSource(list(items), normalizer, **source_kwargs)
    detail = await run_ingestion(
        source=source,
        sessionmaker=sessionmaker,
        normalizer=normalizer,
        clock=clock or TickClock(),
        **kwargs,
    )
    assert source.closed == 1
    c = detail.counters
    assert c.pages_fetched == (
        c.pages_created + c.pages_changed + c.pages_unchanged + c.pages_duplicate + c.pages_failed
    )
    return detail
