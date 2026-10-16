"""The collector against a real PostgreSQL: the live-link index and ``link_events`` ingest.

What the live window IS (ADR-0015 §12.6.3): the ``link_plan_transitions`` rows ``deployed``
and ``rolled_back`` -- never the ``deployment_items`` stage. ``failed`` plans are live on
purpose (amendment 1e). href mode compares the written ``deployment_items.href``.

Ingest is pseudonymous and idempotent: the stored column set is exactly the eight
non-identifying columns, ``received_at`` is minute-truncated, and a replayed page view
inserts nothing (``ON CONFLICT (dedupe_key) DO NOTHING``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from hashlib import sha256

import pytest
from sqlalchemy import Connection, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.analytics import LinkEventType, MatchMode
from ken_contracts.recommendations import LinkPlanStatus
from ken_core.db.models.analytics import LINK_EVENT_COLUMNS, LinkEvent
from ken_services.analytics.collector.index import SqlLiveLinkIndex
from ken_services.analytics.collector.sink import SqlLinkEventSink
from ken_services.analytics.protocol import CollectorRejection as R
from ken_services.analytics.protocol import dedupe_key
from tests.integration.conftest import SyncDb
from tests.integration.test_review_schema import _page, _user
from tests.unit.analytics._collector_support import (
    NOW,
    PEER,
    UA,
    Clock,
    build_app,
    call,
    href_event,
    payload,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SITE = "https://s.test"
SRC = uuid.UUID("00000000-0000-4000-8000-000000000a01")
TGT = uuid.UUID("00000000-0000-4000-8000-000000000a02")
USER = uuid.UUID("00000000-0000-4000-8000-000000000a03")
DEPLOY = uuid.UUID("00000000-0000-4000-8000-000000000a04")
CTX = sha256(b"block").hexdigest()
FINGERPRINT = sha256(b"request").hexdigest()

LIVE_ATTR = uuid.UUID("00000000-0000-4000-8000-000000000b01")
LIVE_FAILED = uuid.UUID("00000000-0000-4000-8000-000000000b02")
ROLLED_BACK = uuid.UUID("00000000-0000-4000-8000-000000000b03")
NEVER_DEPLOYED = uuid.UUID("00000000-0000-4000-8000-000000000b04")
SUPERSEDED = uuid.UUID("00000000-0000-4000-8000-000000000b05")
TWIN = uuid.UUID("00000000-0000-4000-8000-000000000b06")


def _plan(
    c: Connection,
    plan_id: uuid.UUID,
    *,
    status: str,
    anchor: str,
    start: int,
    source_path: str = "/s",
) -> None:
    c.execute(
        text(
            "INSERT INTO link_plans (id, source_page_id, source_page_version, source_url, "
            "target_page_id, target_page_version, target_url, anchor_text, anchor_source, "
            "block_id, block_kind, occurrence_index, start_offset, end_offset, context_hash, "
            "total_score, confidence, rationale, engine_version, policy_version, "
            "ranking_policy_version, analysis_version, status) VALUES (:id, :s, 1, :src_url, "
            ":t, 1, :tgt_url, :anchor, 'target_topic', 'b0', 'p', 0, :start, :end, :ctx, "
            "0.5, 0.5, 'r', 'e', 1, 1, 'tax1-alg1', :status)"
        ),
        {
            "id": plan_id,
            "s": SRC,
            "t": TGT,
            "src_url": f"{SITE}{source_path}",
            "tgt_url": f"{SITE}/t",
            "anchor": anchor,
            "start": start,
            "end": start + len(anchor),
            "ctx": CTX,
            "status": status,
        },
    )


def _transition(c: Connection, plan_id: uuid.UUID, to_status: str) -> None:
    c.execute(
        text(
            "INSERT INTO link_plan_transitions (plan_id, from_status, to_status, actor) "
            "VALUES (:p, NULL, :to, 'system:test')"
        ),
        {"p": plan_id, "to": to_status},
    )


def _deployment(c: Connection) -> None:
    c.execute(
        text(
            "INSERT INTO deployments (id, operation_id, request_fingerprint, request_json, "
            "cms_provider, cms_page_id, source_page_id, source_page_version, status, actor, "
            "user_id, applied_at) VALUES (:id, :op, :fp, '{}', 'simulated', 'sim-1', :s, 1, "
            "'applied', 'user:tester', :u, now())"
        ),
        {"id": DEPLOY, "op": uuid.uuid4(), "fp": FINGERPRINT, "s": SRC, "u": USER},
    )


def _item(c: Connection, plan_id: uuid.UUID, *, stage: str, href: str, anchor: str) -> None:
    c.execute(
        text(
            "INSERT INTO deployment_items (deployment_id, plan_id, change_id, stage, href, "
            "anchor_text, target_page_id) VALUES (:d, :p, :p, :stage, :href, :anchor, :t)"
        ),
        {"d": DEPLOY, "p": plan_id, "stage": stage, "href": href, "anchor": anchor, "t": TGT},
    )


def _seed(c: Connection) -> None:
    _page(c, SRC, "s")
    _page(c, TGT, "t")
    _user(c, USER)
    _deployment(c)

    # Live: deployed, with the href that was actually written.
    _plan(c, LIVE_ATTR, status=LinkPlanStatus.DEPLOYED.value, anchor="alpha", start=0)
    _transition(c, LIVE_ATTR, LinkPlanStatus.DEPLOYED.value)
    _item(c, LIVE_ATTR, stage="applied", href=f"{SITE}/t", anchor="alpha")

    # Live on purpose: a failed item may still have left the link on the page (amendment 1e),
    # and the CMS no-op stage is 'already_applied' with a RELATIVE href.
    _plan(c, LIVE_FAILED, status=LinkPlanStatus.FAILED.value, anchor="beta", start=10)
    _transition(c, LIVE_FAILED, LinkPlanStatus.DEPLOYED.value)
    _item(c, LIVE_FAILED, stage="already_applied", href="/t", anchor="beta")

    # Not live: the window closed.
    _plan(c, ROLLED_BACK, status=LinkPlanStatus.ROLLED_BACK.value, anchor="gamma", start=20)
    _transition(c, ROLLED_BACK, LinkPlanStatus.DEPLOYED.value)
    _transition(c, ROLLED_BACK, LinkPlanStatus.ROLLED_BACK.value)
    _item(c, ROLLED_BACK, stage="applied", href=f"{SITE}/t", anchor="gamma")

    # Not live: never on a page.
    _plan(
        c, NEVER_DEPLOYED, status=LinkPlanStatus.AWAITING_APPROVAL.value, anchor="delta", start=30
    )
    _plan(c, SUPERSEDED, status=LinkPlanStatus.SUPERSEDED.value, anchor="epsilon", start=40)


def _twin(c: Connection) -> None:
    """A second live plan with the SAME source path, anchor and written href."""
    _plan(c, TWIN, status=LinkPlanStatus.VERIFIED.value, anchor="alpha", start=50)
    _transition(c, TWIN, LinkPlanStatus.DEPLOYED.value)
    _item(c, TWIN, stage="applied", href=f"{SITE}/t", anchor="alpha")


@pytest.fixture
def seeded(sync_db: SyncDb) -> SyncDb:
    sync_db.run(_seed)
    return sync_db


@pytest.fixture
def seeded_twin(seeded: SyncDb) -> SyncDb:
    seeded.run(_twin)
    return seeded


async def _index(sessionmaker: async_sessionmaker[AsyncSession]) -> SqlLiveLinkIndex:
    index = SqlLiveLinkIndex(sessionmaker)
    await index.refresh()
    return index


async def _events(sessionmaker: async_sessionmaker[AsyncSession]) -> Sequence[LinkEvent]:
    async with sessionmaker() as session:
        return (await session.execute(select(LinkEvent).order_by(LinkEvent.id))).scalars().all()


# --------------------------------------------------------------------------- #
# The live-link index
# --------------------------------------------------------------------------- #
async def test_only_plans_with_an_open_window_are_live(
    seeded: SyncDb, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    index = await _index(sessionmaker)
    assert index.size == 2
    assert index.resolve_attr(plan_id=LIVE_ATTR, path="/s").plan_id == LIVE_ATTR
    assert index.resolve_attr(plan_id=LIVE_FAILED, path="/s").plan_id == LIVE_FAILED
    for absent in (ROLLED_BACK, NEVER_DEPLOYED, SUPERSEDED, uuid.uuid4()):
        resolution = index.resolve_attr(plan_id=absent, path="/s")
        assert resolution.plan_id is None
        assert resolution.rejection is R.LINK_NOT_LIVE


async def test_a_live_plan_on_another_path_is_a_path_mismatch(
    seeded: SyncDb, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    index = await _index(sessionmaker)
    resolution = index.resolve_attr(plan_id=LIVE_ATTR, path="/other")
    assert resolution.plan_id is None
    assert resolution.rejection is R.PATH_MISMATCH


async def test_href_mode_matches_the_written_href_resolved_against_the_source_url(
    seeded: SyncDb, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    index = await _index(sessionmaker)
    # The plan's own href is absolute; the beacon may send either form.
    for href in (f"{SITE}/t", "/t", "t"):
        resolution = index.resolve_href(path="/s", href=href, anchor_text="alpha")
        assert resolution.plan_id == LIVE_ATTR, href
        assert resolution.match_mode is MatchMode.HREF
    # And the plan whose WRITTEN href is relative matches an absolute one.
    assert index.resolve_href(path="/s", href=f"{SITE}/t", anchor_text="beta").plan_id == (
        LIVE_FAILED
    )


@pytest.mark.parametrize(
    ("href", "anchor"),
    [(f"{SITE}/elsewhere", "alpha"), (f"{SITE}/t", "ALPHA"), (f"{SITE}/t", "gamma")],
)
async def test_href_mode_without_a_match_is_unresolved(
    seeded: SyncDb, sessionmaker: async_sessionmaker[AsyncSession], href: str, anchor: str
) -> None:
    index = await _index(sessionmaker)
    resolution = index.resolve_href(path="/s", href=href, anchor_text=anchor)
    assert resolution.plan_id is None
    assert resolution.rejection is R.UNRESOLVED_LINK


async def test_two_live_plans_with_the_same_href_and_anchor_are_ambiguous(
    seeded_twin: SyncDb, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    index = await _index(sessionmaker)
    assert index.size == 3
    resolution = index.resolve_href(path="/s", href=f"{SITE}/t", anchor_text="alpha")
    assert resolution.plan_id is None
    assert resolution.rejection is R.AMBIGUOUS_LINK


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #
async def test_an_accepted_event_stores_only_pseudonymous_columns(
    seeded: SyncDb, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    app, _ = build_app(
        clock=Clock(),
        index=await _index(sessionmaker),  # the REAL index
        sink=SqlLinkEventSink(sessionmaker),  # and the REAL sink
    )
    response, _ = await call(
        app,
        body=payload(
            {"t": "imp", "l": str(LIVE_ATTR)}, {"t": "clk", "l": str(LIVE_ATTR)}, path="/s"
        ),
    )
    assert response.status == 204

    rows = await _events(sessionmaker)
    assert [(row.event_type, row.match_mode) for row in rows] == [
        (LinkEventType.IMPRESSION.value, MatchMode.ATTR.value),
        (LinkEventType.CLICK.value, MatchMode.ATTR.value),
    ]
    assert {row.plan_id for row in rows} == {LIVE_ATTR}
    assert rows[0].received_at == NOW.replace(second=0, microsecond=0)
    assert rows[0].event_date == NOW.date()
    assert rows[0].synthetic is True
    assert rows[0].dedupe_key == dedupe_key("0" * 32, "imp", LIVE_ATTR)

    async with sessionmaker() as session:
        columns: set[str] = set(
            (
                await session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'link_events'"
                    )
                )
            )
            .scalars()
            .all()
        )
        dumped: Sequence[str] = (
            (await session.execute(text("SELECT to_jsonb(e)::text FROM link_events e")))
            .scalars()
            .all()
        )
    assert columns == set(LINK_EVENT_COLUMNS)
    for line in dumped:
        for identifier in (PEER, UA, "0" * 32, "/s"):
            assert identifier not in line


async def test_a_replayed_page_view_inserts_nothing(
    seeded: SyncDb, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    app, _ = build_app(index=await _index(sessionmaker), sink=SqlLinkEventSink(sessionmaker))
    body = payload({"t": "imp", "l": str(LIVE_ATTR)}, path="/s")
    for _ in range(3):
        response, _ = await call(app, body=body)
        assert response.status == 204
    assert len(await _events(sessionmaker)) == 1
    assert app.counters.counts[R.DUPLICATE.value] == 2


async def test_href_mode_stores_the_href_match_mode(
    seeded: SyncDb, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    app, _ = build_app(index=await _index(sessionmaker), sink=SqlLinkEventSink(sessionmaker))
    await call(app, body=payload(href_event(href="/t", anchor="beta"), path="/s"))
    rows = await _events(sessionmaker)
    assert [(row.plan_id, row.match_mode) for row in rows] == [(LIVE_FAILED, MatchMode.HREF.value)]


async def test_events_for_a_rolled_back_or_unknown_plan_store_nothing(
    seeded: SyncDb, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    app, _ = build_app(index=await _index(sessionmaker), sink=SqlLinkEventSink(sessionmaker))
    for plan_id in (ROLLED_BACK, NEVER_DEPLOYED, SUPERSEDED, uuid.uuid4()):
        response, _ = await call(app, body=payload({"t": "imp", "l": str(plan_id)}, path="/s"))
        assert response.status == 204
    assert await _events(sessionmaker) == []
    assert app.counters.counts == {R.LINK_NOT_LIVE.value: 4}


async def test_the_sink_reports_only_new_rows(
    seeded: SyncDb, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from ken_services.analytics.collector.sink import NewLinkEvent

    sink = SqlLinkEventSink(sessionmaker)
    event = NewLinkEvent(
        event_type=LinkEventType.IMPRESSION,
        plan_id=LIVE_ATTR,
        match_mode=MatchMode.ATTR,
        synthetic=True,
        received_at=NOW,
        dedupe_key=dedupe_key("f" * 32, "imp", LIVE_ATTR),
    )
    assert await sink.store([event]) == 1
    assert await sink.store([event]) == 0
    assert await sink.store([]) == 0
    assert len(await _events(sessionmaker)) == 1
