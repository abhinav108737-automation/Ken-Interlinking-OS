"""Ingestion service against the ``*_test`` database (ADR-0009 sections 3-6, 9).

A scripted source and a fake normaliser stand in for the real ones, so these
tests exercise persistence, accounting, precedence and events only.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ken_contracts.events import EventName
from ken_contracts.inventory import (
    EMITTED_BY_INGEST,
    SUBJECT_INGESTION_RUN,
    SUBJECT_PAGE,
    CanonicalStatus,
    CrawlCompletedPayload,
    IssueCode,
    IssueSeverity,
    LinkType,
    PageDeletedPayload,
    PageEventPayload,
    RunStatus,
    SourceType,
)
from ken_core.db.models.event import Event
from ken_core.db.models.inventory import (
    ExistingLink,
    IngestionIssue,
    IngestionRun,
    KnownUrl,
    Page,
    PageVersion,
)
from ken_services.inventory.ingest import (
    INGEST_LOCK_KEY,
    IngestionAlreadyRunningError,
    run_ingestion,
)
from ken_services.inventory.urls import InvalidUrlError
from tests.integration.conftest import (
    Boom,
    FakeNormalizer,
    FakeSource,
    TickClock,
    ingest,
    make_issue,
    make_link,
    make_page,
    url,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SM = async_sessionmaker[AsyncSession]


# --------------------------------------------------------------------------- #
# Query helpers
# --------------------------------------------------------------------------- #
async def _all(sm: SM, statement: Any) -> list[Any]:
    async with sm() as session:
        return list((await session.scalars(statement)).all())


async def _page(sm: SM, path: str) -> Page:
    pages = await _all(sm, select(Page).where(Page.canonical_url == url(path)))
    assert len(pages) == 1, f"expected one page at {path}"
    page: Page = pages[0]
    return page


async def _pages(sm: SM) -> list[Page]:
    return await _all(sm, select(Page).order_by(Page.canonical_url))


async def _versions(sm: SM, page_id: uuid.UUID) -> list[PageVersion]:
    return await _all(
        sm, select(PageVersion).where(PageVersion.page_id == page_id).order_by(PageVersion.version)
    )


async def _links(sm: SM, page_id: uuid.UUID, version: int) -> list[ExistingLink]:
    return await _all(
        sm,
        select(ExistingLink)
        .where(ExistingLink.page_id == page_id, ExistingLink.page_version == version)
        .order_by(ExistingLink.ordinal),
    )


async def _events(sm: SM, name: EventName | None = None) -> list[Event]:
    statement = select(Event).order_by(Event.id)
    if name is not None:
        statement = statement.where(Event.event_name == name.value)
    return await _all(sm, statement)


async def _known(sm: SM, path: str) -> KnownUrl | None:
    rows = await _all(sm, select(KnownUrl).where(KnownUrl.url == url(path)))
    return rows[0] if rows else None


async def _runs(sm: SM) -> list[IngestionRun]:
    return await _all(sm, select(IngestionRun).order_by(IngestionRun.started_at))


async def _issues(sm: SM, run_id: uuid.UUID) -> list[IngestionIssue]:
    return await _all(
        sm,
        select(IngestionIssue).where(IngestionIssue.run_id == run_id).order_by(IngestionIssue.id),
    )


def _codes(issues: Sequence[IngestionIssue]) -> list[str]:
    return [issue.code for issue in issues]


# --------------------------------------------------------------------------- #
# New, unchanged, changed
# --------------------------------------------------------------------------- #
async def test_new_page_writes_version_links_event_and_known_urls(sessionmaker: SM) -> None:
    page = make_page(
        "/a",
        links=(
            make_link(0, url("/b")),
            make_link(1, "https://elsewhere.test/", link_type=LinkType.EXTERNAL),
        ),
        requested=url("/old"),
        redirect_chain=(url("/old"),),
        page_type="report",
    )
    detail = await ingest(sessionmaker, page)

    assert detail.status is RunStatus.SUCCEEDED
    assert detail.counters.pages_created == 1
    assert detail.counters.pages_fetched == 1
    assert detail.counters.pages_discovered == 1

    row = await _page(sessionmaker, "/a")
    assert row.current_version == 1
    assert row.content_hash == page.content_hash
    assert (row.internal_link_count, row.external_link_count) == (1, 1)
    assert row.page_type == "report"
    assert row.gone_at is None

    [version] = await _versions(sessionmaker, row.id)
    assert version.version == 1
    assert version.run_id == detail.id
    assert version.canonical_status == CanonicalStatus.SELF.value
    assert version.redirect_chain == [url("/old")]
    assert version.page_type == "report"
    links = await _links(sessionmaker, row.id, 1)
    assert [(link.ordinal, link.resolved_url) for link in links] == [
        (0, url("/b")),
        (1, "https://elsewhere.test/"),
    ]

    [created] = await _events(sessionmaker, EventName.PAGE_CREATED)
    assert created.payload == PageEventPayload(
        page_id=row.id,
        canonical_url=url("/a"),
        version=1,
        content_hash=page.content_hash,
        source_type=SourceType.FILE_EXPORT,
        run_id=detail.id,
    ).model_dump(mode="json")

    final = await _known(sessionmaker, "/a")
    hop = await _known(sessionmaker, "/old")
    assert final is not None and hop is not None
    assert (final.outcome, final.page_id, final.http_status) == ("page", row.id, 200)
    assert (hop.outcome, hop.page_id) == ("redirect", row.id)
    assert final.last_run_id == detail.id


async def test_unchanged_reingest_writes_nothing_but_advances_last_seen(sessionmaker: SM) -> None:
    page = make_page("/a", links=(make_link(0, url("/b")),))
    await ingest(sessionmaker, page)
    first = await _page(sessionmaker, "/a")

    clock = TickClock(start=first.last_seen_at)
    detail = await ingest(sessionmaker, page, clock=clock)

    assert detail.counters.pages_unchanged == 1
    assert detail.counters.pages_created == detail.counters.pages_changed == 0
    second = await _page(sessionmaker, "/a")
    assert second.id == first.id
    assert second.current_version == 1
    assert second.last_seen_at > first.last_seen_at
    assert second.last_changed_at == first.last_changed_at
    assert len(await _versions(sessionmaker, first.id)) == 1
    assert len(await _links(sessionmaker, first.id, 1)) == 1
    page_events = [e for e in await _events(sessionmaker) if e.subject_type == SUBJECT_PAGE]
    assert [e.event_name for e in page_events] == ["page.created"]


async def test_changed_content_writes_version_2_and_keeps_version_1(sessionmaker: SM) -> None:
    v1 = make_page("/a", body="one", links=(make_link(0, url("/old-target")),))
    v2 = make_page("/a", body="two", links=(make_link(0, url("/new-target")),))
    await ingest(sessionmaker, v1)
    detail = await ingest(sessionmaker, v2)

    assert detail.counters.pages_changed == 1
    row = await _page(sessionmaker, "/a")
    assert row.current_version == 2
    assert row.content_hash == v2.content_hash
    assert [v.version for v in await _versions(sessionmaker, row.id)] == [1, 2]
    assert [link.resolved_url for link in await _links(sessionmaker, row.id, 1)] == [
        url("/old-target")
    ]
    assert [link.resolved_url for link in await _links(sessionmaker, row.id, 2)] == [
        url("/new-target")
    ]

    [updated] = await _events(sessionmaker, EventName.PAGE_UPDATED)
    payload = PageEventPayload.model_validate(updated.payload)
    assert (payload.version, payload.previous_version) == (2, 1)
    assert payload.previous_content_hash == v1.content_hash
    assert payload.content_hash == v2.content_hash
    assert payload.run_id == detail.id


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
async def test_external_id_keeps_identity_when_url_changes(sessionmaker: SM) -> None:
    await ingest(sessionmaker, make_page("/a", external_id="cms-1"))
    original = await _page(sessionmaker, "/a")

    detail = await ingest(sessionmaker, make_page("/a-renamed", external_id="cms-1"))

    assert detail.counters.pages_changed == 1
    moved = await _page(sessionmaker, "/a-renamed")
    assert moved.id == original.id
    assert moved.external_id == "cms-1"
    assert moved.current_version == 2
    assert len(await _pages(sessionmaker)) == 1
    old = await _known(sessionmaker, "/a")
    assert old is not None and old.page_id == original.id


async def test_external_id_is_attached_to_a_page_found_by_url(sessionmaker: SM) -> None:
    await ingest(sessionmaker, make_page("/a"))
    detail = await ingest(sessionmaker, make_page("/a", external_id="cms-7"))
    assert detail.counters.pages_unchanged == 1
    assert (await _page(sessionmaker, "/a")).external_id == "cms-7"


async def test_identity_conflict_is_rejected_not_merged(sessionmaker: SM) -> None:
    await ingest(
        sessionmaker,
        make_page("/a", external_id="cms-1"),
        make_page("/b", external_id="cms-2"),
    )
    before = {p.canonical_url: (p.id, p.current_version) for p in await _pages(sessionmaker)}

    # cms-1 is page /a, but URL /b belongs to another page.
    detail = await ingest(
        sessionmaker,
        make_page("/b", external_id="cms-1", body="moved"),
        # URL /a is cms-1's page, but this document claims a different external id.
        make_page("/a", external_id="cms-9", body="other"),
    )

    assert detail.status is RunStatus.COMPLETED_WITH_ERRORS
    assert detail.counters.pages_failed == 2
    assert detail.counters.error_count == 2
    assert detail.issue_counts == {"identity_conflict": 2}
    after = {p.canonical_url: (p.id, p.current_version) for p in await _pages(sessionmaker)}
    assert after == before


# --------------------------------------------------------------------------- #
# Canonical precedence (ADR-0009 section 9)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("alias_first", [True, False])
async def test_authoritative_document_beats_alias_within_one_run(
    sessionmaker: SM, alias_first: bool
) -> None:
    own = make_page("/x", body="the real page")
    alias = make_page("/y", canonical="/x", body="an alias with other text")
    items = [alias, own] if alias_first else [own, alias]

    detail = await ingest(sessionmaker, *items)

    assert detail.status is RunStatus.SUCCEEDED  # duplicate_canonical is INFO
    assert (detail.counters.pages_created, detail.counters.pages_duplicate) == (1, 1)
    assert detail.issue_counts == {"duplicate_canonical": 1}
    row = await _page(sessionmaker, "/x")
    [version] = await _versions(sessionmaker, row.id)
    assert version.body_text == "the real page"
    assert version.canonical_status == "self"
    alias_url = await _known(sessionmaker, "/y")
    assert alias_url is not None
    assert (alias_url.outcome, alias_url.page_id) == ("page", row.id)
    [issue] = await _issues(sessionmaker, detail.id)
    assert (issue.severity, issue.url, issue.related_url) == ("info", url("/y"), url("/x"))


async def test_alias_and_authority_across_runs_do_not_flip_flop(sessionmaker: SM) -> None:
    own = make_page("/x", body="the real page")
    alias = make_page("/y", canonical="/x", body="alias text")

    # Run 1: only the alias is seen. It may create identity /x.
    first = await ingest(sessionmaker, alias)
    assert first.counters.pages_created == 1
    row = await _page(sessionmaker, "/x")
    assert (await _versions(sessionmaker, row.id))[0].canonical_status == "declared"
    assert (await _known(sessionmaker, "/x")) is not None  # identity URL mapped

    # Run 2: the authority arrives and takes over.
    second = await ingest(sessionmaker, alias, own)
    assert (second.counters.pages_changed, second.counters.pages_duplicate) == (1, 1)

    # Runs 3 and 4: stable. No version is written for either document.
    third = await ingest(sessionmaker, alias, own)
    assert (third.counters.pages_unchanged, third.counters.pages_duplicate) == (1, 1)
    fourth = await ingest(sessionmaker, make_page("/y", canonical="/x", body="alias v2"))
    assert fourth.counters.pages_duplicate == 1

    versions = await _versions(sessionmaker, row.id)
    assert [(v.version, v.body_text) for v in versions] == [
        (1, "alias text"),
        (2, "the real page"),
    ]
    assert len(await _events(sessionmaker, EventName.PAGE_UPDATED)) == 1


async def test_first_of_two_aliases_wins_and_stays_stable(sessionmaker: SM) -> None:
    a = make_page("/y1", canonical="/x", body="alias one")
    b = make_page("/y2", canonical="/x", body="alias two")
    first = await ingest(sessionmaker, a, b)
    second = await ingest(sessionmaker, a, b)
    assert (first.counters.pages_created, first.counters.pages_duplicate) == (1, 1)
    assert (second.counters.pages_unchanged, second.counters.pages_duplicate) == (1, 1)
    row = await _page(sessionmaker, "/x")
    assert [v.body_text for v in await _versions(sessionmaker, row.id)] == ["alias one"]


async def test_second_authoritative_document_for_one_identity_is_a_duplicate(
    sessionmaker: SM,
) -> None:
    detail = await ingest(
        sessionmaker, make_page("/x", body="first"), make_page("/x", body="second")
    )
    assert (detail.counters.pages_created, detail.counters.pages_duplicate) == (1, 1)
    row = await _page(sessionmaker, "/x")
    assert [v.body_text for v in await _versions(sessionmaker, row.id)] == ["first"]


# --------------------------------------------------------------------------- #
# Per-document failures are isolated
# --------------------------------------------------------------------------- #
async def test_normaliser_failure_is_isolated_and_safe(sessionmaker: SM) -> None:
    detail = await ingest(
        sessionmaker,
        make_page("/a"),
        Boom(RuntimeError("password=hunter2 secret detail"), path="/broken"),
        Boom(InvalidUrlError("bad url"), path="/bad-url"),
        make_page("/b"),
    )

    assert detail.status is RunStatus.COMPLETED_WITH_ERRORS
    assert (detail.counters.pages_created, detail.counters.pages_failed) == (2, 2)
    assert detail.counters.pages_fetched == 4
    issues = await _issues(sessionmaker, detail.id)
    assert _codes(issues) == ["parse_error", "invalid_url"]
    assert issues[0].url == url("/broken")
    assert "hunter2" not in issues[0].message
    assert "RuntimeError" in issues[0].message
    assert len(await _pages(sessionmaker)) == 2


async def test_database_failure_on_one_document_is_isolated(sessionmaker: SM) -> None:
    # PostgreSQL text cannot hold NUL: the insert fails for this document only.
    detail = await ingest(
        sessionmaker,
        make_page("/a"),
        make_page("/b", title="bad\x00title"),
        make_page("/c"),
    )

    assert detail.status is RunStatus.COMPLETED_WITH_ERRORS
    assert (detail.counters.pages_created, detail.counters.pages_failed) == (2, 1)
    [issue] = await _issues(sessionmaker, detail.id)
    assert issue.code == "persist_error"
    assert issue.url == url("/b")
    assert [p.canonical_url for p in await _pages(sessionmaker)] == [url("/a"), url("/c")]
    # The failed document left nothing behind.
    assert await _known(sessionmaker, "/b") is None
    assert len(await _events(sessionmaker, EventName.PAGE_CREATED)) == 2


# --------------------------------------------------------------------------- #
# Issues, known URLs and gone pages
# --------------------------------------------------------------------------- #
async def test_issues_are_stored_counted_and_recorded_as_known_urls(sessionmaker: SM) -> None:
    error = IssueSeverity.ERROR
    info = IssueSeverity.INFO
    detail = await ingest(
        sessionmaker,
        make_page("/a"),
        make_issue(IssueCode.HTTP_NOT_FOUND, url("/missing"), severity=error, http_status=404),
        make_issue(IssueCode.HTTP_GONE, url("/removed"), severity=error, http_status=410),
        make_issue(IssueCode.HTTP_ERROR, url("/err"), severity=error, http_status=503),
        make_issue(IssueCode.TIMEOUT, url("/slow")),
        make_issue(IssueCode.NETWORK_ERROR, url("/net")),
        make_issue(IssueCode.ROBOTS_DISALLOWED, url("/private"), severity=info),
        make_issue(IssueCode.NON_HTML, url("/file.pdf"), severity=info),
        make_issue(IssueCode.LIMIT_REACHED, url("/later"), severity=info),
        make_issue(IssueCode.DUPLICATE_URL, url("/a?x"), severity=info, related_url=url("/a")),
        skipped=3,
        discovered=9,
    )

    counters = detail.counters
    assert detail.status is RunStatus.COMPLETED_WITH_ERRORS
    assert (counters.error_count, counters.warning_count) == (3, 2)
    assert (counters.pages_fetched, counters.pages_created) == (1, 1)
    assert (counters.pages_discovered, counters.pages_skipped) == (9, 3)
    assert sum(detail.issue_counts.values()) == 9
    assert len(await _issues(sessionmaker, detail.id)) == 9

    expected = {
        "/missing": ("not_found", 404),
        "/removed": ("gone", 410),
        "/err": ("http_error", 503),
        "/slow": ("fetch_error", None),
        "/net": ("fetch_error", None),
        "/private": ("blocked_by_robots", None),
        "/file.pdf": ("non_html", None),
    }
    for path, (outcome, status) in expected.items():
        row = await _known(sessionmaker, path)
        assert row is not None, path
        assert (row.outcome, row.http_status, row.page_id) == (outcome, status, None)
    assert await _known(sessionmaker, "/later") is None
    duplicate = await _known(sessionmaker, "/a?x")
    page = await _page(sessionmaker, "/a")
    assert duplicate is not None
    assert (duplicate.outcome, duplicate.page_id) == ("redirect", page.id)


async def test_transient_failure_keeps_the_last_known_page(sessionmaker: SM) -> None:
    await ingest(sessionmaker, make_page("/a"))
    page = await _page(sessionmaker, "/a")
    await ingest(sessionmaker, make_issue(IssueCode.TIMEOUT, url("/a")))
    row = await _known(sessionmaker, "/a")
    assert row is not None
    assert (row.outcome, row.page_id) == ("fetch_error", page.id)
    assert (await _page(sessionmaker, "/a")).gone_at is None


async def test_stored_issues_are_capped_but_counters_stay_exact(sessionmaker: SM) -> None:
    issues = [make_issue(IssueCode.TIMEOUT, url(f"/t{i}")) for i in range(5)]
    detail = await ingest(sessionmaker, *issues, max_stored_issues=2)
    assert detail.counters.warning_count == 5
    assert detail.issue_counts == {"timeout": 2}
    assert len(await _issues(sessionmaker, detail.id)) == 2
    # The cap limits storage only: every URL is still recorded.
    for i in range(5):
        assert await _known(sessionmaker, f"/t{i}") is not None


async def test_404_marks_page_gone_once_and_reappearance_clears_it(sessionmaker: SM) -> None:
    page = make_page("/a")
    await ingest(sessionmaker, page)
    row = await _page(sessionmaker, "/a")

    def not_found() -> Any:
        return make_issue(
            IssueCode.HTTP_NOT_FOUND, url("/a"), severity=IssueSeverity.ERROR, http_status=404
        )

    gone = await ingest(sessionmaker, not_found())
    again = await ingest(sessionmaker, not_found())
    assert (gone.counters.pages_gone, again.counters.pages_gone) == (1, 0)
    marked = await _page(sessionmaker, "/a")
    assert marked.gone_at is not None
    [deleted] = await _events(sessionmaker, EventName.PAGE_DELETED)
    assert deleted.payload == PageDeletedPayload(
        page_id=row.id,
        canonical_url=url("/a"),
        http_status=404,
        last_version=1,
        run_id=gone.id,
    ).model_dump(mode="json")
    known = await _known(sessionmaker, "/a")
    assert known is not None and (known.outcome, known.page_id) == ("not_found", row.id)

    back = await ingest(sessionmaker, page)
    assert back.counters.pages_unchanged == 1
    assert (await _page(sessionmaker, "/a")).gone_at is None
    assert await _events(sessionmaker, EventName.PAGE_UPDATED) == []

    await ingest(
        sessionmaker,
        make_issue(IssueCode.HTTP_GONE, url("/a"), severity=IssueSeverity.ERROR, http_status=410),
    )
    deleted_events = await _events(sessionmaker, EventName.PAGE_DELETED)
    assert [e.payload["http_status"] for e in deleted_events] == [404, 410]


# --------------------------------------------------------------------------- #
# Run lifecycle and events
# --------------------------------------------------------------------------- #
async def test_crawl_completed_is_emitted_for_every_outcome(sessionmaker: SM) -> None:
    ok = await ingest(sessionmaker, make_page("/a"))
    errors = await ingest(sessionmaker, Boom(RuntimeError("x")))
    failed = await ingest(sessionmaker, scope_error=RuntimeError("x"))

    events = await _events(sessionmaker, EventName.CRAWL_COMPLETED)
    assert len(events) == 3
    for event, detail in zip(events, (ok, errors, failed), strict=True):
        payload = CrawlCompletedPayload.model_validate(event.payload)
        assert payload.run_id == detail.id
        assert payload.status is detail.status
        assert payload.counters == detail.counters
        assert payload.completed_at == detail.completed_at
        assert event.subject_type == SUBJECT_INGESTION_RUN
        assert event.subject_id == str(detail.id)
    assert [ok.status, errors.status, failed.status] == [
        RunStatus.SUCCEEDED,
        RunStatus.COMPLETED_WITH_ERRORS,
        RunStatus.FAILED,
    ]


async def test_source_failure_fails_the_run_with_a_safe_reason(sessionmaker: SM) -> None:
    detail = await ingest(
        sessionmaker, scope_error=RuntimeError("postgresql://u:hunter2@db/x unreachable")
    )

    assert detail.status is RunStatus.FAILED
    assert detail.failure_reason is not None
    assert "RuntimeError" in detail.failure_reason
    assert "hunter2" not in detail.failure_reason
    [run] = await _runs(sessionmaker)
    assert run.status == "failed"
    assert run.failure_reason == detail.failure_reason
    assert run.completed_at is not None
    assert await _pages(sessionmaker) == []


async def test_failure_mid_iteration_keeps_what_was_persisted(sessionmaker: SM) -> None:
    detail = await ingest(
        sessionmaker,
        make_page("/a"),
        Boom(OSError("disk"), in_source=True),
        make_page("/b"),
    )
    assert detail.status is RunStatus.FAILED
    assert detail.counters.pages_created == 1
    assert [p.canonical_url for p in await _pages(sessionmaker)] == [url("/a")]


async def test_cancellation_marks_the_run_failed_and_propagates(sessionmaker: SM) -> None:
    normalizer = FakeNormalizer()
    source = FakeSource(
        [make_page("/a"), Boom(asyncio.CancelledError(), in_source=True)], normalizer
    )
    with pytest.raises(asyncio.CancelledError):
        await run_ingestion(source=source, sessionmaker=sessionmaker, normalizer=normalizer)
    [run] = await _runs(sessionmaker)
    assert (run.status, run.failure_reason) == ("failed", "ingestion interrupted")
    assert source.closed == 1
    assert len(await _events(sessionmaker, EventName.CRAWL_COMPLETED)) == 1


async def test_config_snapshot_is_persisted(sessionmaker: SM) -> None:
    snapshot = {"path": "exports/site.jsonl", "max_bytes": 1024, "hosts": ["site.test"]}
    detail = await ingest(sessionmaker, make_page("/a"), snapshot=snapshot)
    [run] = await _runs(sessionmaker)
    assert run.config_snapshot == snapshot
    assert detail.config_snapshot == snapshot
    assert (run.source_type, run.source_name) == ("file_export", "fake-source")


async def test_unserialisable_config_snapshot_is_refused_before_a_run(sessionmaker: SM) -> None:
    normalizer = FakeNormalizer()
    source = FakeSource([], normalizer, snapshot={"bad": object()})
    with pytest.raises(ValueError, match="JSON"):
        await run_ingestion(source=source, sessionmaker=sessionmaker, normalizer=normalizer)
    assert await _runs(sessionmaker) == []
    assert source.closed == 1


async def test_concurrent_run_is_refused_by_the_advisory_lock(
    sessionmaker: SM, async_engine: AsyncEngine
) -> None:
    async with async_engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": INGEST_LOCK_KEY})
        await holder.commit()
        normalizer = FakeNormalizer()
        source = FakeSource([make_page("/a")], normalizer)
        with pytest.raises(IngestionAlreadyRunningError):
            await run_ingestion(source=source, sessionmaker=sessionmaker, normalizer=normalizer)
        assert source.closed == 1
        assert not source.iterated
        assert await _runs(sessionmaker) == []
        await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": INGEST_LOCK_KEY})
        await holder.commit()

    detail = await ingest(sessionmaker, make_page("/a"))
    assert detail.status is RunStatus.SUCCEEDED
    # The lock is released after the run.
    async with async_engine.connect() as probe:
        held = await probe.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"))
    assert held == 0


async def test_events_carry_the_frozen_identity(sessionmaker: SM) -> None:
    await ingest(sessionmaker, make_page("/a"))
    detail = await ingest(
        sessionmaker,
        make_page("/a", body="changed"),
        make_page("/b"),
        make_issue(
            IssueCode.HTTP_NOT_FOUND, url("/b"), severity=IssueSeverity.ERROR, http_status=404
        ),
    )
    events = [e for e in await _events(sessionmaker) if e.correlation_id == detail.id]
    assert [e.event_name for e in events] == [
        "page.updated",
        "page.created",
        "page.deleted",
        "crawl.completed",
    ]
    for event in events:
        assert event.emitted_by == EMITTED_BY_INGEST
        if event.event_name == "crawl.completed":
            assert (event.subject_type, event.subject_id) == (
                SUBJECT_INGESTION_RUN,
                str(detail.id),
            )
        else:
            assert event.subject_type == SUBJECT_PAGE
            assert event.subject_id == event.payload["page_id"]


async def test_run_row_matches_the_returned_detail(sessionmaker: SM) -> None:
    detail = await ingest(
        sessionmaker,
        make_page("/a"),
        make_issue(IssueCode.TIMEOUT, url("/t")),
    )
    [run] = await _runs(sessionmaker)
    assert run.id == detail.id
    assert run.status == detail.status.value
    assert run.started_at == detail.started_at
    assert run.completed_at == detail.completed_at
    assert run.pages_created == detail.counters.pages_created == 1
    assert run.warning_count == detail.counters.warning_count == 1
