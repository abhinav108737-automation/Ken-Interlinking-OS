"""The CMS provider contract (ADR-0014 s4 and s8), run against EVERY provider.

``mock``      -- :class:`MockCMSProvider` (in memory) with the REAL HTML engine;
``simulated`` -- :class:`SimulatedCMSProvider` over the DISPOSABLE database
                 ``ken_cms_sim_test`` (or ``KEN_TEST_CMS_SIM_DATABASE_URL``; its own
                 database, outside the app's Alembic chain;
                 ``@pytest.mark.integration``);
``http``      -- :class:`HttpCMSProvider` over the local TRD section 13 fixture server
                 (``tests/support/cms_fixture_server.py``, 127.0.0.1 port 0) wrapping a mock.

The HTML engine is the real one (``build_html_engine()``: ``ChangeEngine(HtmlMutator())``),
and every change is built the way the recommendation engine builds it: the block from
``extract_blocks``, ``context_hash = sha256_hex(block.text)``, the occurrence from
``phrase_occurrences``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Final

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ken_contracts.cms import (
    CMSNotFoundError,
    CMSPage,
    CMSProvider,
    CMSUnavailableError,
    CMSValidationError,
    CMSVersionConflictError,
    LinkChange,
    LinkChangeRequest,
    LinkLocator,
    LinkOperation,
)
from ken_core.providers.cms.factory import build_html_engine
from ken_core.providers.cms.http import HttpCMSProvider
from ken_core.providers.cms.mock import MockCMSProvider
from ken_core.providers.cms.simulated import (
    SCHEMA,
    SimFaults,
    SimulatedCMSProvider,
    ensure_database,
)
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import extract_blocks
from ken_services.recommendations.placement import phrase_occurrences
from tests.support.cms_fixture_server import CMSFixtureServer
from tests.support.cms_sim_db import CMS_SIM_DB_PREFIX, cms_sim_test_database_url

SIM_URL: Final = cms_sim_test_database_url()
TOKEN: Final = "fixture-token-7f3a9c"
ACTOR: Final = "user:operator"

SOURCE: Final = "src-india-ev"
TARGET: Final = "tgt-sea-ev"
TARGET_2: Final = "tgt-charging"
PROTECTED: Final = "protected-methodology"
SEED_VERSION: Final = "seed-v1"

SOURCE_URL: Final = "https://example.com/industry-reports/india-ev-market"
SOURCE_CANONICAL: Final = "https://example.com/industry-reports/india-ev-market-2030"
TARGET_HREF: Final = "https://example.com/industry-reports/sea-ev-market"
TARGET_2_HREF: Final = "https://example.com/insights/ev-charging-infrastructure"

SOURCE_HTML: Final = (
    "<main>\n"
    "<h1>India Electric Vehicle Market Outlook</h1>\n"
    '<p id="p-intro">The India electric vehicle market is expanding rapidly, driven by state '
    "purchase incentives &amp; falling battery pack prices.</p>\n"
    '<p id="p-regional">Manufacturers benchmark India against neighbouring Southeast Asian '
    "markets, where two-wheeler electrification follows a similar trajectory.</p>\n"
    '<p id="p-more">See the <a href="https://example.com/about">about page</a> for notes on '
    "battery swapping and public charging infrastructure across major cities.</p>\n"
    "</main>"
)
UNRELATED_FROM: Final = "is expanding rapidly"
UNRELATED_TO: Final = "is growing at double digits"

ANCHOR: Final = "Southeast Asian markets"
ANCHOR_2: Final = "public charging infrastructure"


def _pages() -> list[CMSPage]:
    def page(page_id: str, url: str, body: str, *, protected: bool = False) -> CMSPage:
        return CMSPage(
            page_id=page_id,
            url=url,
            title=page_id,
            body_html=body,
            version=SEED_VERSION,
            canonical_url=SOURCE_CANONICAL if page_id == SOURCE else None,
            protected=protected,
        )

    return [
        page(SOURCE, SOURCE_URL, SOURCE_HTML),
        page(TARGET, TARGET_HREF, "<main><p>South-east Asia.</p></main>"),
        page(TARGET_2, TARGET_2_HREF, "<main><p>Charging.</p></main>"),
        page(
            PROTECTED,
            "https://example.com/research-methodology",
            "<main><p>Our research uses Southeast Asian markets data.</p></main>",
            protected=True,
        ),
    ]


# --------------------------------------------------------------------------- #
# Change builders (the engine's own locator recipe)
# --------------------------------------------------------------------------- #
def insert(
    html: str,
    anchor: str,
    *,
    href: str = TARGET_HREF,
    target: str = TARGET,
    change_id: uuid.UUID | None = None,
    occurrence: int = 0,
) -> LinkChange:
    block = next(b for b in extract_blocks(html) if phrase_occurrences(anchor, b.text))
    start, end = phrase_occurrences(anchor, block.text)[occurrence]
    return LinkChange(
        change_id=change_id or uuid.uuid4(),
        operation=LinkOperation.INSERT,
        target_page_id=target,
        href=href,
        anchor_text=block.text[start:end],
        locator=LinkLocator(
            block_selector=block.block_id,
            occurrence_index=occurrence,
            context_hash=sha256_hex(block.text),
        ),
    )


def remove_of(change: LinkChange) -> LinkChange:
    """The per-plan rollback REMOVE of an applied INSERT (same id, block hash, occurrence)."""
    return change.model_copy(
        update={
            "operation": LinkOperation.REMOVE,
            "previous_href": change.href,
            "previous_anchor_text": change.anchor_text,
        }
    )


def request(
    changes: list[LinkChange],
    *,
    expected_version: str = SEED_VERSION,
    source: str = SOURCE,
    operation_id: uuid.UUID | None = None,
    dry_run: bool = False,
    actor: str = ACTOR,
) -> LinkChangeRequest:
    return LinkChangeRequest(
        operation_id=operation_id or uuid.uuid4(),
        source_page_id=source,
        expected_version=expected_version,
        changes=changes,
        dry_run=dry_run,
        actor=actor,
    )


def linked(html: str, change: LinkChange) -> str:
    """``html`` with exactly our splice around the (single) anchor occurrence."""
    assert html.count(change.anchor_text) == 1
    tag = f'<a href="{change.href}" data-ken-link-id="{change.change_id}">'
    return html.replace(change.anchor_text, f"{tag}{change.anchor_text}</a>", 1)


def codes(exc: CMSValidationError) -> set[str]:
    return {i.code for i in exc.issues}


# --------------------------------------------------------------------------- #
# Harness: one per provider kind
# --------------------------------------------------------------------------- #
@dataclass
class Harness:
    kind: str
    provider: CMSProvider
    edit: Callable[[str, str], Awaitable[CMSPage]]
    extra: Any = None

    async def body(self, page_id: str = SOURCE) -> str:
        return (await self.provider.get_page(page_id=page_id)).body_html

    async def version(self, page_id: str = SOURCE) -> str:
        return (await self.provider.get_page(page_id=page_id)).version


@pytest.fixture(scope="session")
def sim_database() -> str:
    """Create the DISPOSABLE simulated-CMS test database once (a ken_cms_sim*_test only).

    In a worker thread: the fixture may be requested lazily from inside a running loop."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, ensure_database(SIM_URL)).result()
    return SIM_URL


async def _mock_harness() -> AsyncIterator[Harness]:
    mock = MockCMSProvider(_pages(), html_engine=build_html_engine())

    async def edit(page_id: str, body: str) -> CMSPage:
        return mock.edit_page(page_id=page_id, body_html=body)

    yield Harness("mock", mock, edit, mock)
    await mock.aclose()


async def _sim_harness(url: str) -> AsyncIterator[Harness]:
    sim = SimulatedCMSProvider(database_url=url, html_engine=build_html_engine())
    await sim.ensure_schema()
    await sim.reset()
    await sim.upsert_pages(_pages())

    async def edit(page_id: str, body: str) -> CMSPage:
        return await sim.edit_page(page_id=page_id, body_html=body)

    try:
        yield Harness("simulated", sim, edit, sim)
    finally:
        await sim.aclose()


async def _http_harness() -> AsyncIterator[Harness]:
    backing = MockCMSProvider(_pages(), html_engine=build_html_engine())
    async with CMSFixtureServer(backing, token=TOKEN) as server:
        http = HttpCMSProvider(
            base_url=server.base_url, token=SecretStr(TOKEN), writes_enabled=True
        )

        async def edit(page_id: str, body: str) -> CMSPage:
            return backing.edit_page(page_id=page_id, body_html=body)

        try:
            yield Harness("http", http, edit, server)
        finally:
            await http.aclose()


@pytest.fixture(
    params=[
        "mock",
        pytest.param("simulated", marks=pytest.mark.integration),
        "http",
    ]
)
async def cms(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    kind = request.param
    if kind == "mock":
        harness = _mock_harness()
    elif kind == "simulated":
        harness = _sim_harness(request.getfixturevalue("sim_database"))
    else:
        harness = _http_harness()
    async for value in harness:
        yield value


@pytest.fixture
async def http_cms() -> AsyncIterator[Harness]:
    async for value in _http_harness():
        yield value


@pytest.fixture
async def sim(sim_database: str) -> AsyncIterator[Harness]:
    async for value in _sim_harness(sim_database):
        yield value


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
async def test_get_page_and_not_found(cms: Harness) -> None:
    page = await cms.provider.get_page(page_id=SOURCE)
    assert page.page_id == SOURCE
    assert page.body_html == SOURCE_HTML
    assert page.version == SEED_VERSION
    assert (await cms.provider.get_page(page_id=PROTECTED)).protected is True
    with pytest.raises(CMSNotFoundError):
        await cms.provider.get_page(page_id="no-such-page")


async def test_resolve_page_id_where_supported(cms: Harness) -> None:
    resolver = getattr(cms.provider, "resolve_page_id", None)
    if cms.kind == "http":
        assert resolver is None  # TRD s13 has no URL lookup: the page's external_id is used
        return
    assert resolver is not None
    assert await resolver(url=SOURCE_URL) == SOURCE
    assert await resolver(url=SOURCE_CANONICAL) == SOURCE
    assert await resolver(url="https://example.com/unknown") is None


def test_preserves_data_attributes_is_declared(cms: Harness) -> None:
    expected = {"mock": True, "simulated": True, "http": None}[cms.kind]
    assert cms.provider.preserves_data_attributes is expected  # type: ignore[attr-defined]


async def test_health_is_reachable(cms: Harness) -> None:
    health = await cms.provider.health()
    assert health.reachable is True
    assert health.provider == cms.kind


# --------------------------------------------------------------------------- #
# Validate
# --------------------------------------------------------------------------- #
async def test_validate_never_mutates_and_never_consumes_the_operation(cms: Harness) -> None:
    change = insert(SOURCE_HTML, ANCHOR)
    req = request([change])
    result = await cms.provider.validate_link_changes(request=req)
    assert result.valid is True
    assert result.issues == []
    assert [c.change_id for c in result.normalized_changes] == [change.change_id]
    assert await cms.body() == SOURCE_HTML
    assert await cms.version() == SEED_VERSION

    applied = await cms.provider.apply_link_changes(request=req)
    assert applied.status == "applied"
    assert applied.idempotent_replay is False


async def test_validate_reports_html_refusals_without_mutating(cms: Harness) -> None:
    good = insert(SOURCE_HTML, ANCHOR)
    stale = good.model_copy(
        update={"locator": good.locator.model_copy(update={"context_hash": sha256_hex("gone")})}
    )
    result = await cms.provider.validate_link_changes(request=request([stale]))
    assert result.valid is False
    assert {i.code for i in result.issues} == {"block_not_found"}
    assert result.normalized_changes == []
    assert await cms.body() == SOURCE_HTML
    assert await cms.version() == SEED_VERSION


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
async def test_apply_splices_the_link_and_nothing_else(cms: Harness) -> None:
    change = insert(SOURCE_HTML, ANCHOR)
    req = request([change])
    result = await cms.provider.apply_link_changes(request=req)

    page = await cms.provider.get_page(page_id=SOURCE)
    assert result.status == "applied"
    assert result.operation_id == req.operation_id
    assert result.source_page_id == SOURCE
    assert result.applied_change_ids == [change.change_id]
    assert result.previous_version == SEED_VERSION
    assert result.new_version == page.version != SEED_VERSION
    assert result.idempotent_replay is False
    # The splice: every other byte identical (the &amp; entity and the existing link too).
    assert page.body_html == linked(SOURCE_HTML, change)
    # Other pages untouched.
    assert await cms.version(TARGET) == SEED_VERSION


async def test_apply_two_changes_in_one_request(cms: Harness) -> None:
    first = insert(SOURCE_HTML, ANCHOR)
    second = insert(SOURCE_HTML, ANCHOR_2, href=TARGET_2_HREF, target=TARGET_2)
    result = await cms.provider.apply_link_changes(request=request([first, second]))
    assert result.status == "applied"
    assert set(result.applied_change_ids) == {first.change_id, second.change_id}
    assert await cms.body() == linked(linked(SOURCE_HTML, first), second)


async def test_dry_run_writes_nothing_and_never_consumes_the_operation(cms: Harness) -> None:
    change = insert(SOURCE_HTML, ANCHOR)
    req = request([change], dry_run=True)
    result = await cms.provider.apply_link_changes(request=req)
    assert result.status == "no_op"
    assert result.new_version == result.previous_version == SEED_VERSION
    assert result.applied_change_ids == []
    assert await cms.body() == SOURCE_HTML
    assert await cms.version() == SEED_VERSION
    # A dry-run handle is not a deployment.
    with pytest.raises(CMSNotFoundError):
        await cms.provider.rollback(
            deployment_id=result.deployment_id, operation_id=req.operation_id, actor=ACTOR
        )

    real = await cms.provider.apply_link_changes(request=req.model_copy(update={"dry_run": False}))
    assert real.status == "applied"
    assert real.idempotent_replay is False
    assert await cms.body() == linked(SOURCE_HTML, change)


async def test_dry_run_runs_the_splice_checks(cms: Harness) -> None:
    """A dry run that would fail for real fails too (and still writes nothing)."""
    good = insert(SOURCE_HTML, ANCHOR)
    stale = good.model_copy(
        update={"locator": good.locator.model_copy(update={"context_hash": sha256_hex("gone")})}
    )
    req = request([stale], dry_run=True)
    with pytest.raises(CMSValidationError) as exc:
        await cms.provider.apply_link_changes(request=req)
    assert codes(exc.value) == {"block_not_found"}
    assert await cms.version() == SEED_VERSION


async def test_idempotent_replay_even_after_the_page_moved(cms: Harness) -> None:
    change = insert(SOURCE_HTML, ANCHOR)
    req = request([change])
    first = await cms.provider.apply_link_changes(request=req)

    edited = (await cms.body()).replace(UNRELATED_FROM, UNRELATED_TO)
    moved = await cms.edit(SOURCE, edited)
    assert moved.version not in {SEED_VERSION, first.new_version}

    replay = await cms.provider.apply_link_changes(request=req)
    assert replay.idempotent_replay is True
    assert replay.deployment_id == first.deployment_id
    assert replay.new_version == first.new_version
    assert replay.applied_change_ids == first.applied_change_ids
    assert replay.status == first.status
    # The replay applied nothing: the page is exactly the edited one.
    page = await cms.provider.get_page(page_id=SOURCE)
    assert (page.body_html, page.version) == (edited, moved.version)

    reused = req.model_copy(update={"actor": "user:someone-else"})
    with pytest.raises(CMSValidationError) as exc:
        await cms.provider.apply_link_changes(request=reused)
    assert codes(exc.value) == {"operation_id_reused"}
    assert (await cms.provider.get_page(page_id=SOURCE)).version == moved.version


async def test_version_mismatch_is_a_conflict(cms: Harness) -> None:
    change = insert(SOURCE_HTML, ANCHOR)
    with pytest.raises(CMSVersionConflictError) as exc:
        await cms.provider.apply_link_changes(
            request=request([change], expected_version="stale-version")
        )
    assert exc.value.expected == "stale-version"
    assert exc.value.current == SEED_VERSION
    assert await cms.body() == SOURCE_HTML


async def test_rejected_operation_id_is_not_consumed(cms: Harness) -> None:
    change = insert(SOURCE_HTML, ANCHOR)
    req = request([change], expected_version="stale-version")
    with pytest.raises(CMSVersionConflictError):
        await cms.provider.apply_link_changes(request=req)
    retried = await cms.provider.apply_link_changes(
        request=req.model_copy(update={"expected_version": SEED_VERSION})
    )
    assert retried.status == "applied"
    assert retried.idempotent_replay is False


async def test_protected_page_is_refused(cms: Harness) -> None:
    change = insert("<main><p>Our research uses Southeast Asian markets data.</p></main>", ANCHOR)
    with pytest.raises(CMSValidationError) as exc:
        await cms.provider.apply_link_changes(request=request([change], source=PROTECTED))
    assert "protected_page" in codes(exc.value)
    assert await cms.version(PROTECTED) == SEED_VERSION


async def test_terminal_errors_take_precedence_over_a_version_conflict(cms: Harness) -> None:
    """mock.py "Apply": replay > terminal (unknown/protected page, missing actor) > version
    conflict > any other error."""
    protected_change = insert(
        "<main><p>Our research uses Southeast Asian markets data.</p></main>", ANCHOR
    )
    with pytest.raises(CMSValidationError) as exc:
        await cms.provider.apply_link_changes(
            request=request([protected_change], source=PROTECTED, expected_version="stale")
        )
    assert "protected_page" in codes(exc.value)

    change = insert(SOURCE_HTML, ANCHOR)
    with pytest.raises(CMSValidationError) as exc:
        await cms.provider.apply_link_changes(
            request=request([change], expected_version="stale", actor="  ")
        )
    assert "missing_actor" in codes(exc.value)

    with pytest.raises(CMSValidationError) as exc:
        await cms.provider.apply_link_changes(request=request([change], source="no-such-page"))
    assert "unknown_source_page" in codes(exc.value)

    # A change-level error is NOT terminal: a stale version wins (regenerate first).
    unknown_target = change.model_copy(update={"target_page_id": "no-such-target"})
    with pytest.raises(CMSVersionConflictError):
        await cms.provider.apply_link_changes(
            request=request([unknown_target], expected_version="stale")
        )
    with pytest.raises(CMSValidationError) as exc:
        await cms.provider.apply_link_changes(request=request([unknown_target]))
    assert codes(exc.value) == {"unknown_target_page"}
    assert await cms.version() == SEED_VERSION


async def test_already_present_change_is_a_no_op_deployment(cms: Harness) -> None:
    """A change already in place is not APPLIED by a later deployment: it is left out of
    ``applied_change_ids``, the deployment is ``no_op`` and the version does not move; rolling
    that deployment back leaves the link (it belongs to the first deployment)."""
    change = insert(SOURCE_HTML, ANCHOR)
    first_req = request([change])
    first = await cms.provider.apply_link_changes(request=first_req)
    again_req = request([change], expected_version=first.new_version)
    again = await cms.provider.apply_link_changes(request=again_req)
    assert again.status == "no_op"
    assert again.applied_change_ids == []
    assert again.new_version == again.previous_version == first.new_version
    assert await cms.version() == first.new_version

    undone = await cms.provider.rollback(
        deployment_id=again.deployment_id, operation_id=again_req.operation_id, actor=ACTOR
    )
    assert undone.status == "no_op"
    assert undone.restored_version == first.new_version
    assert await cms.body() == linked(SOURCE_HTML, change)

    # It still replays like any recorded operation.
    replay = await cms.provider.apply_link_changes(request=again_req)
    assert replay.idempotent_replay is True
    assert replay.deployment_id == again.deployment_id

    rolled = await cms.provider.rollback(
        deployment_id=first.deployment_id, operation_id=first_req.operation_id, actor=ACTOR
    )
    assert rolled.status == "rolled_back"
    assert await cms.body() == SOURCE_HTML


# --------------------------------------------------------------------------- #
# REMOVE (per-plan rollback)
# --------------------------------------------------------------------------- #
async def test_remove_change_unwraps_our_link(cms: Harness) -> None:
    change = insert(SOURCE_HTML, ANCHOR)
    other = insert(SOURCE_HTML, ANCHOR_2, href=TARGET_2_HREF, target=TARGET_2)
    applied = await cms.provider.apply_link_changes(request=request([change, other]))
    removal = await cms.provider.apply_link_changes(
        request=request([remove_of(change)], expected_version=applied.new_version)
    )
    assert removal.status == "applied"
    assert removal.applied_change_ids == [change.change_id]
    # Only that link is gone; the other one of ours stays.
    assert await cms.body() == linked(SOURCE_HTML, other)


async def test_rollback_of_a_remove_deployment_reinserts_the_link(cms: Harness) -> None:
    change = insert(SOURCE_HTML, ANCHOR)
    applied = await cms.provider.apply_link_changes(request=request([change]))
    remove_req = request([remove_of(change)], expected_version=applied.new_version)
    removal = await cms.provider.apply_link_changes(request=remove_req)
    assert await cms.body() == SOURCE_HTML

    await cms.edit(SOURCE, SOURCE_HTML.replace(UNRELATED_FROM, UNRELATED_TO))
    rolled = await cms.provider.rollback(
        deployment_id=removal.deployment_id, operation_id=remove_req.operation_id, actor=ACTOR
    )
    assert rolled.status == "rolled_back"
    assert await cms.body() == linked(SOURCE_HTML.replace(UNRELATED_FROM, UNRELATED_TO), change)


# --------------------------------------------------------------------------- #
# Rollback (ADR-0014 s8)
# --------------------------------------------------------------------------- #
async def test_rollback_restores_the_snapshot_with_a_new_version(cms: Harness) -> None:
    change = insert(SOURCE_HTML, ANCHOR)
    req = request([change])
    deployment = await cms.provider.apply_link_changes(request=req)

    result = await cms.provider.rollback(
        deployment_id=deployment.deployment_id, operation_id=req.operation_id, actor=ACTOR
    )
    page = await cms.provider.get_page(page_id=SOURCE)
    assert result.status == "rolled_back"
    assert result.deployment_id == deployment.deployment_id
    assert result.actor == ACTOR
    assert page.body_html == SOURCE_HTML
    assert page.version == result.restored_version
    assert result.restored_version not in {SEED_VERSION, deployment.new_version}
    # A plan built on the pre-deployment token does not pass the lock.
    with pytest.raises(CMSVersionConflictError):
        await cms.provider.apply_link_changes(request=request([insert(SOURCE_HTML, ANCHOR)]))


async def test_rollback_after_an_unrelated_edit_is_the_inverse_splice(cms: Harness) -> None:
    change = insert(SOURCE_HTML, ANCHOR)
    req = request([change])
    deployment = await cms.provider.apply_link_changes(request=req)
    edited = (await cms.body()).replace(UNRELATED_FROM, UNRELATED_TO)
    moved = await cms.edit(SOURCE, edited)

    result = await cms.provider.rollback(
        deployment_id=deployment.deployment_id, operation_id=req.operation_id, actor=ACTOR
    )
    page = await cms.provider.get_page(page_id=SOURCE)
    assert result.status == "rolled_back"
    # The unrelated edit survives; our link is gone; nothing else changed.
    assert page.body_html == SOURCE_HTML.replace(UNRELATED_FROM, UNRELATED_TO)
    assert "data-ken-link-id" not in page.body_html
    assert page.version == result.restored_version
    assert result.restored_version not in {SEED_VERSION, deployment.new_version, moved.version}


async def test_rollback_after_a_later_deployment_of_ours(cms: Harness) -> None:
    first = insert(SOURCE_HTML, ANCHOR)
    second = insert(SOURCE_HTML, ANCHOR_2, href=TARGET_2_HREF, target=TARGET_2)
    first_req = request([first])
    a = await cms.provider.apply_link_changes(request=first_req)
    second_req = request([second], expected_version=a.new_version)
    b = await cms.provider.apply_link_changes(request=second_req)

    await cms.provider.rollback(
        deployment_id=a.deployment_id, operation_id=first_req.operation_id, actor=ACTOR
    )
    assert await cms.body() == linked(SOURCE_HTML, second)
    await cms.provider.rollback(
        deployment_id=b.deployment_id, operation_id=second_req.operation_id, actor=ACTOR
    )
    assert await cms.body() == SOURCE_HTML


async def test_rollback_is_refused_when_our_link_itself_changed(cms: Harness) -> None:
    change = insert(SOURCE_HTML, ANCHOR)
    req = request([change])
    deployment = await cms.provider.apply_link_changes(request=req)
    tampered = (await cms.body()).replace(
        f'href="{TARGET_HREF}"', 'href="https://example.com/someone-else"'
    )
    moved = await cms.edit(SOURCE, tampered)

    with pytest.raises(CMSValidationError) as exc:
        await cms.provider.rollback(
            deployment_id=deployment.deployment_id, operation_id=req.operation_id, actor=ACTOR
        )
    assert "rollback_conflict" in codes(exc.value)
    page = await cms.provider.get_page(page_id=SOURCE)
    assert (page.body_html, page.version) == (tampered, moved.version)


async def test_second_rollback_is_a_no_op(cms: Harness) -> None:
    req = request([insert(SOURCE_HTML, ANCHOR)])
    deployment = await cms.provider.apply_link_changes(request=req)
    first = await cms.provider.rollback(
        deployment_id=deployment.deployment_id, operation_id=req.operation_id, actor=ACTOR
    )
    second = await cms.provider.rollback(
        deployment_id=deployment.deployment_id, operation_id=req.operation_id, actor="user:other"
    )
    assert second.status == "no_op"
    assert second.restored_version == first.restored_version
    assert second.actor == first.actor
    assert await cms.version() == first.restored_version
    assert await cms.body() == SOURCE_HTML


async def test_rollback_identity_errors(cms: Harness) -> None:
    req = request([insert(SOURCE_HTML, ANCHOR)])
    deployment = await cms.provider.apply_link_changes(request=req)
    with pytest.raises(CMSValidationError) as exc:
        await cms.provider.rollback(
            deployment_id=deployment.deployment_id, operation_id=uuid.uuid4(), actor=ACTOR
        )
    assert codes(exc.value) == {"deployment_operation_mismatch"}
    with pytest.raises(CMSValidationError) as exc:
        await cms.provider.rollback(
            deployment_id=deployment.deployment_id, operation_id=req.operation_id, actor=" "
        )
    assert codes(exc.value) == {"missing_actor"}
    with pytest.raises(CMSNotFoundError):
        await cms.provider.rollback(
            deployment_id="no-such-deployment", operation_id=req.operation_id, actor=ACTOR
        )
    assert await cms.version() == deployment.new_version


async def test_replay_after_rollback_does_not_reapply(cms: Harness) -> None:
    req = request([insert(SOURCE_HTML, ANCHOR)])
    deployment = await cms.provider.apply_link_changes(request=req)
    rolled = await cms.provider.rollback(
        deployment_id=deployment.deployment_id, operation_id=req.operation_id, actor=ACTOR
    )
    replay = await cms.provider.apply_link_changes(request=req)
    assert replay.idempotent_replay is True
    assert await cms.body() == SOURCE_HTML
    assert await cms.version() == rolled.restored_version


async def test_concurrent_applies_on_one_version_exactly_one_wins(cms: Harness) -> None:
    a = request([insert(SOURCE_HTML, ANCHOR)])
    b = request([insert(SOURCE_HTML, ANCHOR_2, href=TARGET_2_HREF, target=TARGET_2)])
    outcomes = await asyncio.gather(
        cms.provider.apply_link_changes(request=a),
        cms.provider.apply_link_changes(request=b),
        return_exceptions=True,
    )
    wins = [o for o in outcomes if not isinstance(o, BaseException)]
    conflicts = [o for o in outcomes if isinstance(o, CMSVersionConflictError)]
    assert (len(wins), len(conflicts)) == (1, 1)
    assert await cms.version() == wins[0].new_version


# --------------------------------------------------------------------------- #
# HTTP-only, end to end over the fixture server
# --------------------------------------------------------------------------- #
async def test_http_sends_the_token_and_the_server_refuses_without_it(
    http_cms: Harness,
) -> None:
    server: CMSFixtureServer = http_cms.extra
    await http_cms.provider.get_page(page_id=SOURCE)
    assert server.calls[-1].authorization == f"Bearer {TOKEN}"
    anonymous = HttpCMSProvider(base_url=server.base_url, token=None)
    try:
        with pytest.raises(CMSUnavailableError):
            await anonymous.get_page(page_id=SOURCE)
        assert server.calls[-1].authorization is None
    finally:
        await anonymous.aclose()


# --------------------------------------------------------------------------- #
# Simulated-only: fault injection and isolation
# --------------------------------------------------------------------------- #
@pytest.mark.integration
async def test_sim_fail_before_commit_applies_nothing(sim: Harness) -> None:
    provider: SimulatedCMSProvider = sim.extra
    req = request([insert(SOURCE_HTML, ANCHOR)])
    provider.faults.fail_before_commit = 1
    with pytest.raises(CMSUnavailableError):
        await provider.apply_link_changes(request=req)
    assert await sim.body() == SOURCE_HTML
    assert await sim.version() == SEED_VERSION

    resent = await provider.apply_link_changes(request=req)
    assert resent.status == "applied"
    assert resent.idempotent_replay is False
    assert await sim.version() == resent.new_version


@pytest.mark.integration
async def test_sim_fail_after_commit_applied_and_replays(sim: Harness) -> None:
    provider: SimulatedCMSProvider = sim.extra
    change = insert(SOURCE_HTML, ANCHOR)
    req = request([change])
    provider.faults.fail_after_commit = 1
    with pytest.raises(CMSUnavailableError):
        await provider.apply_link_changes(request=req)
    committed = await provider.get_page(page_id=SOURCE)
    assert committed.body_html == linked(SOURCE_HTML, change)

    resent = await provider.apply_link_changes(request=req)
    assert resent.idempotent_replay is True
    assert resent.status == "applied"
    assert resent.new_version == committed.version


@pytest.mark.integration
async def test_sim_rollback_faults_and_reconciliation(sim: Harness) -> None:
    provider: SimulatedCMSProvider = sim.extra
    req = request([insert(SOURCE_HTML, ANCHOR)])
    deployment = await provider.apply_link_changes(request=req)

    provider.faults.fail_before_commit = 1
    with pytest.raises(CMSUnavailableError):
        await provider.rollback(
            deployment_id=deployment.deployment_id, operation_id=req.operation_id, actor=ACTOR
        )
    assert await sim.version() == deployment.new_version

    provider.faults.fail_after_commit = 1
    with pytest.raises(CMSUnavailableError):
        await provider.rollback(
            deployment_id=deployment.deployment_id, operation_id=req.operation_id, actor=ACTOR
        )
    assert await sim.body() == SOURCE_HTML
    resent = await provider.rollback(
        deployment_id=deployment.deployment_id, operation_id=req.operation_id, actor=ACTOR
    )
    assert resent.status == "no_op"
    assert resent.restored_version == await sim.version()


@pytest.mark.integration
async def test_sim_unavailable_fault_refuses_every_call(sim: Harness) -> None:
    provider: SimulatedCMSProvider = sim.extra
    provider.faults = SimFaults(unavailable=True)
    with pytest.raises(CMSUnavailableError):
        await provider.get_page(page_id=SOURCE)
    with pytest.raises(CMSUnavailableError):
        await provider.apply_link_changes(request=request([insert(SOURCE_HTML, ANCHOR)]))
    provider.faults = SimFaults()
    assert await sim.version() == SEED_VERSION


@pytest.mark.integration
async def test_sim_commits_independently_in_its_own_database(sim: Harness) -> None:
    """Its own engine on the sim test database, committed as soon as the call returns: a separate
    connection (an "app" that never committed anything) sees the change."""
    provider: SimulatedCMSProvider = sim.extra
    assert SCHEMA == "cms_sim"
    database = provider._engine.url.database
    assert database is not None and database.startswith(CMS_SIM_DB_PREFIX)
    assert database == make_url(SIM_URL).database
    change = insert(SOURCE_HTML, ANCHOR)
    result = await provider.apply_link_changes(request=request([change]))

    observer = create_async_engine(SIM_URL, poolclass=NullPool)
    try:
        async with observer.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT body_html, version FROM cms_sim.pages WHERE page_id = :p"),
                    {"p": SOURCE},
                )
            ).one()
            ledger = await connection.scalar(text("SELECT count(*) FROM cms_sim.operations"))
    finally:
        await observer.dispose()
    assert (row.body_html, row.version) == (linked(SOURCE_HTML, change), result.new_version)
    assert ledger == 1


@pytest.mark.integration
async def test_sim_concurrent_identical_operations_apply_once(sim: Harness) -> None:
    provider: SimulatedCMSProvider = sim.extra
    req = request([insert(SOURCE_HTML, ANCHOR)])
    results = await asyncio.gather(*(provider.apply_link_changes(request=req) for _ in range(3)))
    assert {r.deployment_id for r in results} == {results[0].deployment_id}
    assert sum(not r.idempotent_replay for r in results) == 1
