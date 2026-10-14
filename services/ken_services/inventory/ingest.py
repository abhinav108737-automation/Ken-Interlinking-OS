"""Ingestion: one run of one ``ContentSourceProvider`` into the inventory (ADR-0009).

Run lifecycle::

    advisory lock (refuse a concurrent run) -> ingestion_runs row (running)
    -> site_scope() -> iter_records():
           SourceIssue    -> ingestion_issues, known_urls, page.deleted on 404/410
           SourceDocument -> normalise -> persist (authoritative now, aliases buffered)
    -> buffered aliases -> deferred duplicate-URL redirects
    -> summary() -> succeeded | completed_with_errors, crawl.completed

Every document is persisted in its OWN transaction, together with its
``known_urls`` rows, the run counters and its event, so a failure on one
document rolls back exactly that document and the run continues.

Canonical precedence (ADR-0009 section 9): a document whose canonical status is
not ``declared`` is AUTHORITATIVE for its identity URL. ``declared`` (alias)
documents are buffered and persisted only after the source is exhausted, so
within one run an identity's own document always wins over its aliases. Across
runs, an alias writes no version once the identity's current version came from
an authoritative document.

Nothing here logs HTML, credentials or exception text; failure reasons and
issue messages carry fixed text plus an exception CLASS name only.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Protocol, cast

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from ken_contracts.events import EventEnvelope, EventName
from ken_contracts.intelligence import ProtectionSource
from ken_contracts.inventory import (
    EMITTED_BY_INGEST,
    MAX_URL_LENGTH,
    SUBJECT_INGESTION_RUN,
    SUBJECT_PAGE,
    CanonicalStatus,
    ContentSourceProvider,
    CrawlCompletedPayload,
    IngestionRunDetail,
    IssueCode,
    IssueSeverity,
    NormalizedPage,
    PageDeletedPayload,
    PageEventPayload,
    RunCounters,
    RunStatus,
    SiteScope,
    SourceDocument,
    SourceIssue,
    SourceSummary,
    SourceType,
    UrlOutcome,
)
from ken_core.db.repositories import intelligence as protection_repo
from ken_core.db.repositories import inventory as repo
from ken_core.db.repositories.events import append_event
from ken_core.logging import get_logger
from ken_services.inventory.urls import InvalidUrlError

CMS_PROTECTED_KEY: Final = "cms_protected"
"""Metadata key a CMS-derived record uses to say the CMS protects the page."""
CMS_PROTECTION_REASON: Final = "The CMS marks this page protected."

logger = get_logger("ken_services.inventory.ingest")

INGEST_LOCK_NAME: Final = "ken.inventory.ingest"
INGEST_LOCK_KEY: Final = -3337962714556857529
"""``pg_advisory_lock`` key: the first 8 bytes of sha256(INGEST_LOCK_NAME), signed.

Fixed (not computed at import) so it is greppable in ``pg_locks``; a unit test
checks it still matches the derivation."""

DEFAULT_NORMALIZER_MODULE: Final = "ken_services.inventory.normalize"
DEFAULT_MAX_STORED_ISSUES: Final = 10_000

SKIPPED_ISSUE_CODES: Final = frozenset(
    {
        IssueCode.ROBOTS_DISALLOWED,
        IssueCode.LIMIT_REACHED,
        IssueCode.NON_HTML,
        IssueCode.OUT_OF_SCOPE,
        IssueCode.DUPLICATE_URL,
    }
)
"""Discovered-but-not-fetched: counted by the source's summary, never as pages."""

_ISSUE_OUTCOMES: Final[dict[IssueCode, UrlOutcome]] = {
    IssueCode.HTTP_NOT_FOUND: UrlOutcome.NOT_FOUND,
    IssueCode.HTTP_GONE: UrlOutcome.GONE,
    IssueCode.HTTP_ERROR: UrlOutcome.HTTP_ERROR,
    IssueCode.TIMEOUT: UrlOutcome.FETCH_ERROR,
    IssueCode.NETWORK_ERROR: UrlOutcome.FETCH_ERROR,
    IssueCode.ROBOTS_DISALLOWED: UrlOutcome.BLOCKED_BY_ROBOTS,
    IssueCode.NON_HTML: UrlOutcome.NON_HTML,
}
_GONE_STATUS: Final[dict[IssueCode, int]] = {
    IssueCode.HTTP_NOT_FOUND: 404,
    IssueCode.HTTP_GONE: 410,
}
_MAX_ISSUE_URL: Final = MAX_URL_LENGTH * 2  # SourceIssue.url max_length

FAILURE_UNEXPECTED: Final = "ingestion aborted by an unexpected error"
FAILURE_INTERRUPTED: Final = "ingestion interrupted"


class IngestionAlreadyRunningError(RuntimeError):
    """Another ingestion run holds the inventory advisory lock. No run row was created."""


class Normalizer(Protocol):
    """``SourceDocument -> NormalizedPage``; pure and deterministic (ADR-0009 section 2)."""

    def __call__(self, doc: SourceDocument, /, *, scope: SiteScope) -> NormalizedPage: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def _default_normalizer() -> Normalizer:
    module: Any = importlib.import_module(DEFAULT_NORMALIZER_MODULE)
    return cast(Normalizer, module.normalize_document)


def _storable_url(url: str | None) -> str | None:
    """A URL that may become a ``known_urls`` key (btree-indexable, valid text)."""
    if not url or "\x00" in url or len(url.encode("utf-8")) > MAX_URL_LENGTH:
        return None
    return url


def _issue_url(url: str | None) -> str | None:
    return url if url is not None and len(url) <= _MAX_ISSUE_URL else None


class _Outcome(StrEnum):
    CREATED = "created"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


@dataclass(slots=True)
class _Tally:
    """Run accounting plus issue-storage state; copied per transaction, kept on commit."""

    pages_discovered: int = 0
    pages_fetched: int = 0
    pages_created: int = 0
    pages_changed: int = 0
    pages_unchanged: int = 0
    pages_duplicate: int = 0
    pages_failed: int = 0
    pages_skipped: int = 0
    pages_gone: int = 0
    error_count: int = 0
    warning_count: int = 0
    stored_issues: int = 0
    issue_counts: Counter[str] = field(default_factory=Counter)

    def copy(self) -> _Tally:
        clone = _Tally(**{f.name: getattr(self, f.name) for f in fields(self)})
        clone.issue_counts = Counter(self.issue_counts)
        return clone

    def counters(self) -> RunCounters:
        return RunCounters(
            pages_discovered=self.pages_discovered,
            pages_fetched=self.pages_fetched,
            pages_created=self.pages_created,
            pages_changed=self.pages_changed,
            pages_unchanged=self.pages_unchanged,
            pages_duplicate=self.pages_duplicate,
            pages_failed=self.pages_failed,
            pages_skipped=self.pages_skipped,
            pages_gone=self.pages_gone,
            error_count=self.error_count,
            warning_count=self.warning_count,
        )


type _Work = Callable[[AsyncSession, _Tally, datetime], Awaitable[Any]]


class _Run:
    """State of one run. Every write goes through :meth:`_transaction`."""

    def __init__(
        self,
        *,
        source: ContentSourceProvider,
        sessionmaker: async_sessionmaker[AsyncSession],
        normalizer: Normalizer,
        clock: Callable[[], datetime],
        max_stored_issues: int,
        config_snapshot: dict[str, Any],
    ) -> None:
        self.run_id = uuid.uuid4()
        self.source = source
        self.sessionmaker = sessionmaker
        self.normalizer = normalizer
        self.clock = clock
        self.max_stored_issues = max_stored_issues
        self.config_snapshot = config_snapshot
        self.source_type = source.source_type
        self.source_name = source.name
        self.started_at = clock()
        self.tally = _Tally()
        self.claimed: set[uuid.UUID] = set()
        self.aliases: list[NormalizedPage] = []
        self.redirects: list[tuple[str, str]] = []
        self.summary: SourceSummary | None = None

    # ------------------------------------------------------------ plumbing
    async def _transaction(self, work: _Work) -> Any:
        """Run ``work`` and persist the run counters atomically with it.

        The in-memory tally changes only if the transaction commits, so the
        counters in memory and in the database never diverge.
        """
        tentative = self.tally.copy()
        now = self.clock()
        async with self.sessionmaker() as session, session.begin():
            result = await work(session, tentative, now)
            await repo.update_run_counters(
                session, run_id=self.run_id, counters=tentative.counters(), now=now
            )
        self.tally = tentative
        return result

    async def _emit(
        self,
        session: AsyncSession,
        *,
        name: EventName,
        subject_type: str,
        subject_id: uuid.UUID,
        payload: BaseModel,
        now: datetime,
    ) -> None:
        await append_event(
            session,
            EventEnvelope(
                event_name=name,
                occurred_at=now,
                emitted_by=EMITTED_BY_INGEST,
                subject_type=subject_type,
                subject_id=str(subject_id),
                payload=payload.model_dump(mode="json"),
                correlation_id=self.run_id,
            ),
        )

    async def _record_issue(self, session: AsyncSession, tally: _Tally, issue: SourceIssue) -> None:
        if issue.severity is IssueSeverity.ERROR:
            tally.error_count += 1
        elif issue.severity is IssueSeverity.WARNING:
            tally.warning_count += 1
        if tally.stored_issues < self.max_stored_issues:
            await repo.insert_issue(session, run_id=self.run_id, issue=issue)
            tally.stored_issues += 1
            tally.issue_counts[issue.code.value] += 1

    def _issue(
        self,
        code: IssueCode,
        severity: IssueSeverity,
        message: str,
        *,
        url: str | None = None,
        related_url: str | None = None,
    ) -> SourceIssue:
        return SourceIssue(
            code=code,
            severity=severity,
            url=_issue_url(url),
            related_url=_issue_url(related_url),
            message=message,
            occurred_at=self.clock(),
        )

    # ----------------------------------------------------------- lifecycle
    async def start(self) -> None:
        async with self.sessionmaker() as session, session.begin():
            await repo.insert_run(
                session,
                run_id=self.run_id,
                source_type=self.source_type.value,
                source_name=self.source_name,
                config_snapshot=self.config_snapshot,
                started_at=self.started_at,
            )
        logger.info(
            "ingest_run_started",
            run_id=str(self.run_id),
            source_type=self.source_type.value,
            source_name=self.source_name,
        )

    async def execute(self) -> None:
        scope = await self.source.site_scope()
        records = self.source.iter_records()
        try:
            async for record in records:
                if isinstance(record, SourceDocument):
                    await self._on_document(record, scope)
                elif isinstance(record, SourceIssue):
                    await self._on_issue(record)
                else:
                    raise TypeError("source yielded a record of an unknown type")
        finally:
            await _close_iterator(records)
        # Aliases only now: every authoritative document of this run has been seen.
        for page in self.aliases:
            await self._persist(page, authoritative=False)
        for url, related_url in self.redirects:
            await self._transaction(self._redirect_work(url, related_url))
        self.summary = await self.source.summary()

    async def finish(self, status: RunStatus, failure_reason: str | None) -> IngestionRunDetail:
        tentative = self.tally.copy()
        if self.summary is not None:
            tentative.pages_discovered = self.summary.discovered
            tentative.pages_skipped = self.summary.skipped
        completed_at = max(self.clock(), self.started_at)
        counters = tentative.counters()
        async with self.sessionmaker() as session, session.begin():
            await repo.finish_run(
                session,
                run_id=self.run_id,
                status=status,
                counters=counters,
                completed_at=completed_at,
                failure_reason=failure_reason,
            )
            await self._emit(
                session,
                name=EventName.CRAWL_COMPLETED,
                subject_type=SUBJECT_INGESTION_RUN,
                subject_id=self.run_id,
                payload=CrawlCompletedPayload(
                    run_id=self.run_id,
                    source_type=self.source_type,
                    source_name=self.source_name,
                    status=status,
                    started_at=self.started_at,
                    completed_at=completed_at,
                    counters=counters,
                ),
                now=completed_at,
            )
        self.tally = tentative
        logger.info(
            "ingest_run_finished",
            run_id=str(self.run_id),
            status=status.value,
            **counters.model_dump(),
        )
        return IngestionRunDetail(
            id=self.run_id,
            source_type=self.source_type,
            source_name=self.source_name,
            status=status,
            started_at=self.started_at,
            completed_at=completed_at,
            counters=counters,
            config_snapshot=self.config_snapshot,
            failure_reason=failure_reason,
            issue_counts=dict(sorted(tentative.issue_counts.items())),
        )

    async def finish_failed(self, reason: str) -> IngestionRunDetail:
        if self.summary is None:
            try:
                self.summary = await self.source.summary()
            except Exception as exc:  # best effort: the run has already failed
                logger.warning("ingest_summary_unavailable", error_class=type(exc).__name__)
        return await self.finish(RunStatus.FAILED, reason)

    # -------------------------------------------------------------- issues
    async def _on_issue(self, issue: SourceIssue) -> None:
        await self._transaction(lambda s, t, now: self._apply_issue(s, t, issue, now))
        if issue.code is IssueCode.DUPLICATE_URL:
            url, related = _storable_url(issue.url), _storable_url(issue.related_url)
            # A repeated line in an export names the SAME url: that is not a
            # redirect, and recording it as one would overwrite the URL's "page"
            # outcome with a self-redirect (Phase 1 QA L3).
            if url is not None and related is not None and url != related:
                self.redirects.append((url, related))

    async def _apply_issue(
        self, session: AsyncSession, tally: _Tally, issue: SourceIssue, now: datetime
    ) -> None:
        await self._record_issue(session, tally, issue)
        outcome = _ISSUE_OUTCOMES.get(issue.code)
        url = _storable_url(issue.url)
        if outcome is None or url is None:
            return
        gone_status = _GONE_STATUS.get(issue.code)
        await repo.upsert_known_url(
            session,
            url=url,
            outcome=outcome,
            page_id=None,  # keeps the last known page (implication rule)
            http_status=issue.http_status or gone_status,
            run_id=self.run_id,
            now=now,
        )
        if gone_status is None:
            return
        page = await repo.lock_live_page_by_canonical_url(session, url)
        if page is None:
            return
        await repo.mark_page_gone(session, page_id=page.id, now=now)
        tally.pages_gone += 1
        await self._emit(
            session,
            name=EventName.PAGE_DELETED,
            subject_type=SUBJECT_PAGE,
            subject_id=page.id,
            payload=PageDeletedPayload(
                page_id=page.id,
                canonical_url=page.canonical_url,
                http_status=404 if gone_status == 404 else 410,
                last_version=page.current_version,
                run_id=self.run_id,
            ),
            now=now,
        )

    def _redirect_work(self, url: str, related_url: str) -> _Work:
        async def work(session: AsyncSession, _tally: _Tally, now: datetime) -> None:
            await self._apply_redirect(session, url, related_url, now)

        return work

    async def _apply_redirect(
        self, session: AsyncSession, url: str, related_url: str, now: datetime
    ) -> None:
        target = await repo.page_id_for_url(session, related_url)
        if target is None:
            return
        await repo.upsert_known_url(
            session,
            url=url,
            outcome=UrlOutcome.REDIRECT,
            page_id=target,
            http_status=None,
            run_id=self.run_id,
            now=now,
        )

    # ----------------------------------------------------------- documents
    async def _on_document(self, doc: SourceDocument, scope: SiteScope) -> None:
        try:
            page: object = await asyncio.to_thread(self.normalizer, doc, scope=scope)
            if not isinstance(page, NormalizedPage):
                raise TypeError("normaliser returned an unexpected type")
        except Exception as exc:
            code = _normalisation_issue_code(exc)
            issue = self._issue(
                code,
                IssueSeverity.ERROR,
                f"document could not be normalised ({type(exc).__name__})",
                url=doc.final_url,
            )
            logger.warning(
                "ingest_document_not_normalised",
                run_id=str(self.run_id),
                code=code.value,
                error_class=type(exc).__name__,
            )
            await self._transaction(lambda s, t, _now: self._failed(s, t, issue))
            return
        if page.canonical_status is CanonicalStatus.DECLARED:
            self.aliases.append(page)
        else:
            await self._persist(page, authoritative=True)

    async def _failed(self, session: AsyncSession, tally: _Tally, issue: SourceIssue) -> None:
        tally.pages_fetched += 1
        tally.pages_failed += 1
        await self._record_issue(session, tally, issue)

    async def _persist(self, page: NormalizedPage, *, authoritative: bool) -> None:
        try:
            outcome, page_id = await self._transaction(
                lambda s, t, now: self._apply_document(s, t, page, authoritative, now)
            )
        except SQLAlchemyError as exc:
            logger.warning(
                "ingest_document_not_persisted",
                run_id=str(self.run_id),
                error_class=type(exc).__name__,
            )
            issue = self._issue(
                IssueCode.PERSIST_ERROR,
                IssueSeverity.ERROR,
                f"document could not be stored ({type(exc).__name__})",
                url=page.url,
            )
            await self._transaction(lambda s, t, _now: self._failed(s, t, issue))
            return
        if outcome in {_Outcome.CREATED, _Outcome.CHANGED, _Outcome.UNCHANGED}:
            self.claimed.add(page_id)

    async def _apply_document(
        self,
        session: AsyncSession,
        tally: _Tally,
        page: NormalizedPage,
        authoritative: bool,
        now: datetime,
    ) -> tuple[_Outcome, uuid.UUID | None]:
        tally.pages_fetched += 1
        identity = page.canonical_url
        # An alias's external id names the alias's own record, not the identity it declares.
        external_id = page.external_id if authoritative else None

        state: repo.PageState | None = None
        new_external_id: str | None = None
        rename = False
        if external_id is not None:
            state = await repo.lock_page_by_external_id(session, external_id)
            if state is not None and state.canonical_url != identity:
                owner = await repo.lock_page_by_canonical_url(session, identity)
                if owner is not None and owner.id != state.id:
                    await self._conflict(session, tally, page)
                    return _Outcome.CONFLICT, None
                rename = True
        if state is None:
            state = await repo.lock_page_by_canonical_url(session, identity)
            if state is not None and external_id is not None:
                if state.external_id is None:
                    new_external_id = external_id
                elif state.external_id != external_id:
                    await self._conflict(session, tally, page)
                    return _Outcome.CONFLICT, None

        if state is None:
            page_id = uuid.uuid4()
            await repo.insert_page(
                session,
                page_id=page_id,
                canonical_url=identity,
                external_id=external_id,
                page=page,
                now=now,
            )
            await repo.insert_version(
                session, page_id=page_id, version=1, page=page, run_id=self.run_id, now=now
            )
            await self._emit_page_event(session, EventName.PAGE_CREATED, page_id, page, 1, None)
            await self._observe(session, page, page_id, now)
            await self._sync_cms_protection(session, page, page_id, now)
            tally.pages_created += 1
            return _Outcome.CREATED, page_id

        if await self._is_duplicate(session, state, authoritative):
            await self._observe(session, page, state.id, now)
            await self._record_issue(
                session,
                tally,
                self._issue(
                    IssueCode.DUPLICATE_CANONICAL,
                    IssueSeverity.INFO,
                    "another document is authoritative for this canonical URL",
                    url=page.url,
                    related_url=identity,
                ),
            )
            tally.pages_duplicate += 1
            return _Outcome.DUPLICATE, state.id

        if rename or new_external_id is not None:
            await repo.set_page_identity(
                session,
                page_id=state.id,
                canonical_url=identity,
                external_id=new_external_id or state.external_id,
                now=now,
            )
        if state.content_hash == page.content_hash:
            await repo.touch_page(session, page_id=state.id, now=now)
            await self._observe(session, page, state.id, now)
            await self._sync_cms_protection(session, page, state.id, now)
            tally.pages_unchanged += 1
            return _Outcome.UNCHANGED, state.id

        version = state.current_version + 1
        await repo.insert_version(
            session, page_id=state.id, version=version, page=page, run_id=self.run_id, now=now
        )
        await repo.set_page_current(session, page_id=state.id, page=page, version=version, now=now)
        await self._emit_page_event(session, EventName.PAGE_UPDATED, state.id, page, version, state)
        await self._observe(session, page, state.id, now)
        await self._sync_cms_protection(session, page, state.id, now)
        tally.pages_changed += 1
        return _Outcome.CHANGED, state.id

    async def _sync_cms_protection(
        self, session: AsyncSession, page: NormalizedPage, page_id: uuid.UUID, now: datetime
    ) -> None:
        """Mirror the CMS's own protection flag on EVERY document, changed or not.

        Protection must never lag (ADR-0010 section 5). Metadata is not part of
        the content hash, so a CMS switching ``protected`` on for unchanged
        content creates no new version -- and Phase 2's policy rules, which run
        on analysis, would not see it until the content changed. Only
        CMS-derived records (a CMS read or a CMS export) can carry the flag; a
        public-web crawl never creates or clears a CMS protection.
        """
        if page.source_type not in (SourceType.CMS, SourceType.FILE_EXPORT):
            return
        flagged = page.metadata.get(CMS_PROTECTED_KEY) == "true"
        cms_rows = [
            row
            for row in await protection_repo.active_protections(session, page_id=page_id)
            if row.source == ProtectionSource.CMS.value
        ]
        if flagged and not cms_rows:
            await protection_repo.insert_protection(
                session,
                page_id=page_id,
                source=ProtectionSource.CMS.value,
                rule_id=None,
                reason=CMS_PROTECTION_REASON,
                created_by=EMITTED_BY_INGEST,
                created_at=now,
            )
        elif not flagged:
            for row in cms_rows:
                await protection_repo.deactivate_protection(
                    session, protection_id=row.id, deactivated_by=EMITTED_BY_INGEST, now=now
                )

    async def _is_duplicate(
        self, session: AsyncSession, state: repo.PageState, authoritative: bool
    ) -> bool:
        if state.id in self.claimed:
            return True  # first document for this identity in this run wins
        if authoritative:
            return False
        current_status = await repo.version_canonical_status(
            session, page_id=state.id, version=state.current_version
        )
        return current_status != CanonicalStatus.DECLARED.value

    async def _conflict(self, session: AsyncSession, tally: _Tally, page: NormalizedPage) -> None:
        tally.pages_failed += 1
        await self._record_issue(
            session,
            tally,
            self._issue(
                IssueCode.IDENTITY_CONFLICT,
                IssueSeverity.ERROR,
                "external id and canonical URL identify two different pages",
                url=page.url,
                related_url=page.canonical_url,
            ),
        )

    async def _emit_page_event(
        self,
        session: AsyncSession,
        name: EventName,
        page_id: uuid.UUID,
        page: NormalizedPage,
        version: int,
        previous: repo.PageState | None,
    ) -> None:
        await self._emit(
            session,
            name=name,
            subject_type=SUBJECT_PAGE,
            subject_id=page_id,
            payload=PageEventPayload(
                page_id=page_id,
                canonical_url=page.canonical_url,
                version=version,
                content_hash=page.content_hash,
                previous_version=None if previous is None else previous.current_version,
                previous_content_hash=None if previous is None else previous.content_hash,
                source_type=page.source_type,
                run_id=self.run_id,
            ),
            now=self.clock(),
        )

    async def _observe(
        self, session: AsyncSession, page: NormalizedPage, page_id: uuid.UUID, now: datetime
    ) -> None:
        """Map every URL this document was reached through to its page."""
        observations: dict[str, tuple[UrlOutcome, int | None]] = {}
        for hop in page.redirect_chain:
            observations[hop] = (UrlOutcome.REDIRECT, None)
        if page.requested_url != page.url:
            observations.setdefault(page.requested_url, (UrlOutcome.REDIRECT, None))
        observations[page.url] = (UrlOutcome.PAGE, page.http_status)
        await repo.upsert_known_urls(
            session,
            observations=[
                (url, outcome, status)
                for raw, (outcome, status) in observations.items()
                if (url := _storable_url(raw)) is not None
            ],
            page_id=page_id,
            run_id=self.run_id,
            now=now,
        )
        identity = _storable_url(page.canonical_url)
        if identity is not None and identity not in observations:
            await repo.insert_known_url_if_absent(
                session, url=identity, page_id=page_id, run_id=self.run_id, now=now
            )


def _normalisation_issue_code(exc: BaseException) -> IssueCode:
    """``invalid_url`` when the document's own URL was refused, else ``parse_error``."""
    if isinstance(exc, InvalidUrlError):
        return IssueCode.INVALID_URL
    try:
        refused: Any = getattr(
            importlib.import_module(DEFAULT_NORMALIZER_MODULE), "NormalizationError", None
        )
    except ImportError:
        refused = None
    if isinstance(refused, type) and isinstance(exc, refused):
        return IssueCode.INVALID_URL
    code = getattr(exc, "code", None)
    if code == IssueCode.INVALID_URL.value:
        return IssueCode.INVALID_URL
    return IssueCode.PARSE_ERROR


async def _close_iterator(records: AsyncIterator[object]) -> None:
    aclose = getattr(records, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception as exc:  # closing must never mask the run's own outcome
        logger.warning("ingest_source_iterator_close_failed", error_class=type(exc).__name__)


def _engine_of(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncEngine:
    bind = sessionmaker.kw.get("bind")
    if not isinstance(bind, AsyncEngine):
        raise TypeError("the sessionmaker must be bound to an AsyncEngine")
    return bind


async def _acquire_lock(engine: AsyncEngine) -> AsyncConnection | None:
    """A dedicated connection holding the session-level ingest lock, or None if taken."""
    connection = await engine.connect()
    try:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": INGEST_LOCK_KEY}
        )
        # The lock is session-level: it outlives this transaction, which is
        # ended so the connection does not sit "idle in transaction" for the run.
        await connection.commit()
    except BaseException:
        await connection.close()
        raise
    if not acquired:
        await connection.close()
        return None
    return connection


async def _release_lock(connection: AsyncConnection) -> None:
    try:
        await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": INGEST_LOCK_KEY})
        await connection.commit()
    except Exception as exc:
        # Never return a connection that may still hold the lock to the pool.
        logger.warning("ingest_lock_release_failed", error_class=type(exc).__name__)
        await connection.invalidate()
    finally:
        await connection.close()


async def _close_source(source: ContentSourceProvider) -> None:
    try:
        await source.aclose()
    except Exception as exc:
        logger.warning("ingest_source_close_failed", error_class=type(exc).__name__)


def _snapshot(source: ContentSourceProvider) -> dict[str, Any]:
    snapshot = source.config_snapshot()
    try:
        # Round-trip: what is stored is exactly what JSON can represent.
        result: dict[str, Any] = json.loads(json.dumps(snapshot, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("the source's config_snapshot is not JSON-serialisable") from exc
    if not isinstance(result, dict):
        raise ValueError("the source's config_snapshot must be a JSON object")
    return result


async def run_ingestion(
    *,
    source: ContentSourceProvider,
    sessionmaker: async_sessionmaker[AsyncSession],
    normalizer: Normalizer | None = None,
    clock: Callable[[], datetime] = utc_now,
    max_stored_issues: int = DEFAULT_MAX_STORED_ISSUES,
) -> IngestionRunDetail:
    """Run one ingestion of ``source`` and return the finished run.

    A source or unexpected failure gives a ``failed`` run (returned, not raised).
    Raises :class:`IngestionAlreadyRunningError` (no run row is created) when
    another run holds the lock; re-raises cancellation / KeyboardInterrupt after
    marking the run failed. The source is always closed.
    """
    try:
        if max_stored_issues < 0:
            raise ValueError("max_stored_issues must be >= 0")
        resolved = normalizer if normalizer is not None else _default_normalizer()
        snapshot = _snapshot(source)
        lock = await _acquire_lock(_engine_of(sessionmaker))
        if lock is None:
            logger.warning("ingest_already_running", source_type=source.source_type.value)
            raise IngestionAlreadyRunningError("another ingestion run is in progress")
        try:
            run = _Run(
                source=source,
                sessionmaker=sessionmaker,
                normalizer=resolved,
                clock=clock,
                max_stored_issues=max_stored_issues,
                config_snapshot=snapshot,
            )
            await run.start()
            return await _drive(run)
        finally:
            await _release_lock(lock)
    finally:
        await _close_source(source)


async def _drive(run: _Run) -> IngestionRunDetail:
    try:
        await run.execute()
        status = (
            RunStatus.SUCCEEDED if run.tally.error_count == 0 else RunStatus.COMPLETED_WITH_ERRORS
        )
        return await run.finish(status, None)
    except (asyncio.CancelledError, KeyboardInterrupt):
        try:
            await run.finish_failed(FAILURE_INTERRUPTED)
        except Exception as exc:
            logger.error("ingest_run_not_finalised", error_class=type(exc).__name__)
        raise
    except Exception as exc:
        logger.error("ingest_run_failed", run_id=str(run.run_id), error_class=type(exc).__name__)
        return await run.finish_failed(f"{FAILURE_UNEXPECTED} ({type(exc).__name__})")
