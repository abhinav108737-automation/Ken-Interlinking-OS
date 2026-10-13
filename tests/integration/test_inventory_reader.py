"""SqlInventoryReader against the ``*_test`` database, over data written by ingestion."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.inventory import (
    InventoryNotFoundError,
    InventoryReader,
    IssueCode,
    IssueSeverity,
    LinkResolution,
    LinkType,
    PublicationState,
    RunStatus,
    SourceType,
)
from ken_services.inventory.reader import SqlInventoryReader
from tests.integration.conftest import (
    TickClock,
    ingest,
    make_issue,
    make_link,
    make_page,
    url,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SM = async_sessionmaker[AsyncSession]

# Links of /a's CURRENT version, by ordinal -> (target path or URL, expected resolution).
A_LINKS: dict[int, tuple[str, LinkResolution]] = {
    0: ("/b", LinkResolution.RESOLVED),
    1: ("/old-b", LinkResolution.RESOLVED),  # redirected to /b
    2: ("/missing", LinkResolution.BROKEN),  # answered 404
    3: ("/gone-page", LinkResolution.BROKEN),  # the identity URL answered 404
    4: ("/never", LinkResolution.UNRESOLVED),  # never observed
    5: ("https://elsewhere.test/", LinkResolution.NOT_APPLICABLE),
    6: ("/y", LinkResolution.RESOLVED),  # canonical alias of /b
    7: ("/private", LinkResolution.UNRESOLVED),  # robots-blocked
    8: ("/old-gone", LinkResolution.BROKEN),  # redirects to a page that is now gone
}


@dataclass
class World:
    reader: SqlInventoryReader
    run1: uuid.UUID
    run2: uuid.UUID
    a: uuid.UUID
    b: uuid.UUID
    gone: uuid.UUID


def _a_links() -> tuple:  # type: ignore[type-arg]
    links = []
    for ordinal, (target, _) in A_LINKS.items():
        if target.startswith("http"):
            links.append(make_link(ordinal, target, link_type=LinkType.EXTERNAL))
        else:
            links.append(make_link(ordinal, url(target), anchor=f"to {target}"))
    return tuple(links)


@pytest.fixture
async def world(sessionmaker: SM) -> World:
    clock = TickClock()
    error = IssueSeverity.ERROR
    first = await ingest(
        sessionmaker,
        make_page("/a", title="Alpha 100% report", body="v1", links=(make_link(0, url("/b")),)),
        make_page(
            "/b",
            title="Beta_under",
            requested=url("/old-b"),
            redirect_chain=(url("/old-b"),),
            links=(make_link(0, url("/a")),),
        ),
        make_page("/y", canonical="/b", body="alias of b"),
        make_page(
            "/gone-page",
            title="Gone",
            publication_state=PublicationState.DRAFT,
            requested=url("/old-gone"),
            redirect_chain=(url("/old-gone"),),
        ),
        make_issue(IssueCode.HTTP_NOT_FOUND, url("/missing"), severity=error, http_status=404),
        make_issue(IssueCode.ROBOTS_DISALLOWED, url("/private"), severity=IssueSeverity.INFO),
        clock=clock,
        snapshot={"path": "site.jsonl"},
    )
    second = await ingest(
        sessionmaker,
        make_page("/a", title="Alpha 100% report", body="v2", links=_a_links()),
        make_issue(IssueCode.HTTP_NOT_FOUND, url("/gone-page"), severity=error, http_status=404),
        clock=clock,
    )
    reader = SqlInventoryReader(sessionmaker)
    pages = {p.canonical_url: p.id for p in (await reader.list_pages(limit=10, offset=0)).items}
    return World(
        reader=reader,
        run1=first.id,
        run2=second.id,
        a=pages[url("/a")],
        b=pages[url("/b")],
        gone=pages[url("/gone-page")],
    )


def test_reader_satisfies_the_protocol(sessionmaker: SM) -> None:
    assert isinstance(SqlInventoryReader(sessionmaker), InventoryReader)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
async def test_list_pages_newest_change_first_with_totals(world: World) -> None:
    result = await world.reader.list_pages(limit=10, offset=0)
    assert [p.canonical_url for p in result.items] == [url("/a"), url("/gone-page"), url("/b")]
    assert (result.page.total, result.page.has_more) == (3, False)

    paged = await world.reader.list_pages(limit=2, offset=2)
    assert [p.canonical_url for p in paged.items] == [url("/b")]
    assert (paged.page.total, paged.page.limit, paged.page.offset) == (3, 2, 2)


@pytest.mark.parametrize(
    ("search", "expected"),
    [
        ("alpha", ["/a"]),
        ("100%", ["/a"]),
        ("%", ["/a"]),  # a literal percent sign, not a wildcard
        ("_", ["/b"]),  # a literal underscore, not a wildcard
        ("SITE.TEST/GONE", ["/gone-page"]),  # case-insensitive, matches the URL
        ("nothing-like-this", []),
    ],
)
async def test_list_pages_search(world: World, search: str, expected: list[str]) -> None:
    result = await world.reader.list_pages(limit=10, offset=0, search=search)
    assert sorted(p.canonical_url for p in result.items) == sorted(url(p) for p in expected)
    assert result.page.total == len(expected)


async def test_list_pages_by_publication_state(world: World) -> None:
    drafts = await world.reader.list_pages(
        limit=10, offset=0, publication_state=PublicationState.DRAFT
    )
    assert [p.id for p in drafts.items] == [world.gone]
    assert drafts.items[0].gone_at is not None


async def test_get_page_includes_the_current_version(world: World) -> None:
    detail = await world.reader.get_page(page_id=world.a)
    assert detail.current_version == 2
    assert detail.current.version == 2
    assert detail.current.body_text == "v2"
    assert detail.current.run_id == world.run2
    assert detail.internal_link_count == 8
    assert detail.external_link_count == 1
    assert detail.current.canonical_status == "self"


async def test_unknown_page_raises_not_found(world: World) -> None:
    missing = uuid.uuid4()
    for call in (
        world.reader.get_page(page_id=missing),
        world.reader.list_page_versions(page_id=missing, limit=10, offset=0),
        world.reader.get_page_version(page_id=missing, version=1),
        world.reader.list_page_links(page_id=missing, limit=10, offset=0),
    ):
        with pytest.raises(InventoryNotFoundError) as info:
            await call
        assert info.value.resource == "page"
        assert str(missing) not in str(info.value)


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #
async def test_list_page_versions_newest_first(world: World) -> None:
    result = await world.reader.list_page_versions(page_id=world.a, limit=10, offset=0)
    assert [v.version for v in result.items] == [2, 1]
    assert result.page.total == 2
    second = await world.reader.list_page_versions(page_id=world.a, limit=1, offset=1)
    assert [v.version for v in second.items] == [1]
    assert second.page.total == 2


async def test_get_page_version_detail(world: World) -> None:
    v1 = await world.reader.get_page_version(page_id=world.a, version=1)
    assert (v1.version, v1.body_text, v1.run_id) == (1, "v1", world.run1)
    assert v1.source_type is SourceType.FILE_EXPORT
    b = await world.reader.get_page_version(page_id=world.b, version=1)
    assert b.redirect_chain == [url("/old-b")]
    assert b.requested_url == url("/old-b")

    with pytest.raises(InventoryNotFoundError) as info:
        await world.reader.get_page_version(page_id=world.a, version=9)
    assert info.value.resource == "page_version"


# --------------------------------------------------------------------------- #
# Links and read-time resolution
# --------------------------------------------------------------------------- #
async def test_page_links_default_to_the_current_version_with_resolution(world: World) -> None:
    result = await world.reader.list_page_links(page_id=world.a, limit=50, offset=0)
    assert result.page.total == len(A_LINKS)
    by_ordinal = {link.ordinal: link for link in result.items}
    for ordinal, (_, resolution) in A_LINKS.items():
        assert by_ordinal[ordinal].resolution is resolution, ordinal
        assert by_ordinal[ordinal].page_version == 2
        assert by_ordinal[ordinal].source_url == url("/a")
    assert by_ordinal[0].target_page_id == world.b
    assert by_ordinal[1].target_page_id == world.b
    assert by_ordinal[6].target_page_id == world.b
    assert by_ordinal[8].target_page_id == world.gone
    assert by_ordinal[2].target_page_id is None
    # The identity URL now answers 404, but it is still known to be that page.
    assert by_ordinal[3].target_page_id == world.gone
    assert by_ordinal[4].target_page_id is None
    assert by_ordinal[5].target_page_id is None


async def test_page_links_of_a_non_current_version(world: World) -> None:
    result = await world.reader.list_page_links(page_id=world.a, limit=50, offset=0, version=1)
    [link] = result.items
    assert (link.page_version, link.resolved_url) == (1, url("/b"))
    assert link.resolution is LinkResolution.RESOLVED

    with pytest.raises(InventoryNotFoundError) as info:
        await world.reader.list_page_links(page_id=world.a, limit=50, offset=0, version=7)
    assert info.value.resource == "page_version"


async def test_page_links_by_type(world: World) -> None:
    result = await world.reader.list_page_links(
        page_id=world.a, limit=50, offset=0, link_type=LinkType.EXTERNAL
    )
    assert [link.ordinal for link in result.items] == [5]
    assert result.page.total == 1


async def test_list_links_covers_current_versions_only(world: World) -> None:
    result = await world.reader.list_links(limit=200, offset=0)
    # /a v2 (9 links) + /b (1 link); /a v1's link is not current.
    assert result.page.total == len(A_LINKS) + 1
    assert {link.page_version for link in result.items if link.page_id == world.a} == {2}


@pytest.mark.parametrize(
    ("resolution", "expected_total"),
    [
        (LinkResolution.RESOLVED, 4),  # /a -> /b, /old-b, /y and /b -> /a
        (LinkResolution.BROKEN, 3),
        (LinkResolution.UNRESOLVED, 2),
        (LinkResolution.NOT_APPLICABLE, 1),
    ],
)
async def test_resolution_filter_is_applied_before_paging(
    world: World, resolution: LinkResolution, expected_total: int
) -> None:
    first = await world.reader.list_links(limit=1, offset=0, resolution=resolution)
    assert first.page.total == expected_total
    assert len(first.items) == 1
    every = await world.reader.list_links(limit=200, offset=0, resolution=resolution)
    assert len(every.items) == expected_total
    assert {link.resolution for link in every.items} == {resolution}


async def test_inbound_links_by_target_page(world: World) -> None:
    inbound = await world.reader.list_links(limit=50, offset=0, target_page_id=world.b)
    assert sorted(link.ordinal for link in inbound.items) == [0, 1, 6]
    assert {link.page_id for link in inbound.items} == {world.a}
    assert inbound.page.total == 3

    to_a = await world.reader.list_links(limit=50, offset=0, target_page_id=world.a)
    assert [(link.page_id, link.source_url) for link in to_a.items] == [(world.b, url("/b"))]

    internal_only = await world.reader.list_links(
        limit=50, offset=0, link_type=LinkType.EXTERNAL, target_page_id=world.b
    )
    assert internal_only.page.total == 0


# --------------------------------------------------------------------------- #
# Runs and issues
# --------------------------------------------------------------------------- #
async def test_list_runs_newest_first_and_filters(world: World) -> None:
    result = await world.reader.list_runs(limit=10, offset=0)
    assert [r.id for r in result.items] == [world.run2, world.run1]
    assert result.page.total == 2
    assert all(r.status is RunStatus.COMPLETED_WITH_ERRORS for r in result.items)

    none = await world.reader.list_runs(limit=10, offset=0, source_type=SourceType.CMS)
    assert (none.items, none.page.total) == ([], 0)
    some = await world.reader.list_runs(
        limit=10, offset=0, source_type=SourceType.FILE_EXPORT, status=RunStatus.FAILED
    )
    assert some.page.total == 0


async def test_get_run_with_issue_counts(world: World) -> None:
    run = await world.reader.get_run(run_id=world.run1)
    assert run.config_snapshot == {"path": "site.jsonl"}
    assert run.issue_counts == {
        "duplicate_canonical": 1,
        "http_not_found": 1,
        "robots_disallowed": 1,
    }
    assert run.counters.pages_created == 3
    assert run.counters.pages_duplicate == 1
    assert run.failure_reason is None
    assert run.completed_at is not None

    second = await world.reader.get_run(run_id=world.run2)
    assert second.counters.pages_gone == 1

    with pytest.raises(InventoryNotFoundError) as info:
        await world.reader.get_run(run_id=uuid.uuid4())
    assert info.value.resource == "run"


async def test_list_run_issues(world: World) -> None:
    result = await world.reader.list_run_issues(run_id=world.run1, limit=10, offset=0)
    assert result.page.total == 3
    assert [i.code for i in result.items] == [
        IssueCode.HTTP_NOT_FOUND,
        IssueCode.ROBOTS_DISALLOWED,
        IssueCode.DUPLICATE_CANONICAL,
    ]
    errors = await world.reader.list_run_issues(
        run_id=world.run1, limit=10, offset=0, severity=IssueSeverity.ERROR
    )
    assert [(i.code, i.http_status) for i in errors.items] == [(IssueCode.HTTP_NOT_FOUND, 404)]

    with pytest.raises(InventoryNotFoundError) as info:
        await world.reader.list_run_issues(run_id=uuid.uuid4(), limit=10, offset=0)
    assert info.value.resource == "run"


# --------------------------------------------------------------------------- #
# Defensive paging validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (201, 0), (10, -1)])
async def test_paging_is_validated(sessionmaker: SM, limit: int, offset: int) -> None:
    reader = SqlInventoryReader(sessionmaker)
    with pytest.raises(ValueError, match=r"limit|offset"):
        await reader.list_pages(limit=limit, offset=offset)
    with pytest.raises(ValueError, match=r"limit|offset"):
        await reader.list_links(limit=limit, offset=offset)
    with pytest.raises(ValueError, match=r"limit|offset"):
        await reader.list_runs(limit=limit, offset=offset)


async def test_a_transient_failure_does_not_unresolve_an_established_link(
    sessionmaker: SM,
) -> None:
    """ADR-0009 section 9: known_urls keeps the last page a URL led to, so links don't flap."""
    clock = TickClock()
    await ingest(
        sessionmaker,
        make_page("/src", links=(make_link(0, url("/dst")),)),
        make_page("/dst"),
        clock=clock,
    )
    for code in (IssueCode.TIMEOUT, IssueCode.ROBOTS_DISALLOWED, IssueCode.HTTP_ERROR):
        await ingest(
            sessionmaker,
            make_issue(code, url("/dst"), severity=IssueSeverity.ERROR),
            clock=clock,
        )
        reader = SqlInventoryReader(sessionmaker)
        pages = {p.canonical_url: p.id for p in (await reader.list_pages(limit=10, offset=0)).items}
        [link] = (await reader.list_page_links(page_id=pages[url("/src")], limit=5, offset=0)).items
        assert link.resolution is LinkResolution.RESOLVED, code
        assert link.target_page_id == pages[url("/dst")]
