"""The live-link index without a database: the pure rules and failure behaviour.

The SQL side (which plans are live, and the href comparison against
``deployment_items.href``) is asserted against a real PostgreSQL in
``tests/integration/test_collector_ingest.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from ken_contracts.analytics import MatchMode
from ken_services.analytics.collector.index import (
    APPLIED_STAGES,
    LiveLinkSnapshot,
    SqlLiveLinkIndex,
    live_plan_query,
)
from ken_services.analytics.protocol import CollectorRejection as R
from ken_services.deployment.service import LINK_ON_PAGE
from tests.unit.analytics._collector_support import SITE, live_plan, plan_id


def test_the_query_asks_for_the_statuses_and_stages_the_adr_names() -> None:
    sql = str(live_plan_query())
    assert "link_plan_transitions" in sql
    assert "deployment_items" in sql
    assert APPLIED_STAGES == ("applied", "already_applied")
    # failed is live ON PURPOSE (amendment 1e); rolled_back is not a LINK_ON_PAGE status.
    assert "failed" in {status.value for status in LINK_ON_PAGE}
    assert "rolled_back" not in {status.value for status in LINK_ON_PAGE}


def test_an_empty_snapshot_resolves_nothing() -> None:
    snapshot = LiveLinkSnapshot()
    assert len(snapshot) == 0
    assert snapshot.resolve_attr(plan_id=plan_id(1), path="/a").rejection is R.LINK_NOT_LIVE
    assert snapshot.resolve_href(path="/a", href="/b", anchor_text="x").rejection is (
        R.UNRESOLVED_LINK
    )


def test_attr_resolution_is_plan_and_path() -> None:
    snapshot = LiveLinkSnapshot([live_plan(1, path="/reports/a")])
    accepted = snapshot.resolve_attr(plan_id=plan_id(1), path="/reports/a")
    assert accepted.plan_id == plan_id(1)
    assert accepted.match_mode is MatchMode.ATTR
    assert accepted.rejection is None
    assert snapshot.resolve_attr(plan_id=plan_id(1), path="/reports/b").rejection is R.PATH_MISMATCH
    assert snapshot.resolve_attr(plan_id=plan_id(2), path="/reports/a").rejection is R.LINK_NOT_LIVE


@pytest.mark.parametrize(
    "href",
    [
        f"{SITE}/insights/b",
        "/insights/b",
        "../insights/b",
        "  /insights/b  ",
    ],
)
def test_href_resolution_compares_resolved_urls(href: str) -> None:
    snapshot = LiveLinkSnapshot([live_plan(1, path="/reports/a")])
    assert snapshot.resolve_href(
        path="/reports/a", href=href, anchor_text="battery storage"
    ).plan_id == plan_id(1)


@pytest.mark.parametrize("href", ["", "javascript:void(0)", "/insights/other", "http://[::"])
def test_an_unmatched_or_unparseable_href_is_unresolved(href: str) -> None:
    snapshot = LiveLinkSnapshot([live_plan(1)])
    resolution = snapshot.resolve_href(path="/reports/a", href=href, anchor_text="battery storage")
    assert resolution.plan_id is None
    assert resolution.rejection is R.UNRESOLVED_LINK


def test_a_plan_without_a_written_href_never_matches_in_href_mode() -> None:
    snapshot = LiveLinkSnapshot([live_plan(1, hrefs=())])
    assert (
        snapshot.resolve_href(
            path="/reports/a", href=f"{SITE}/insights/b", anchor_text="battery storage"
        ).rejection
        is R.UNRESOLVED_LINK
    )
    # attr mode still works: the link id does not depend on the deployment item.
    assert snapshot.resolve_attr(plan_id=plan_id(1), path="/reports/a").plan_id == plan_id(1)


def test_the_anchor_must_match_exactly() -> None:
    snapshot = LiveLinkSnapshot([live_plan(1, anchor="battery storage")])
    for anchor in ("Battery storage", "battery  storage", " battery storage", "battery"):
        assert (
            snapshot.resolve_href(
                path="/reports/a", href=f"{SITE}/insights/b", anchor_text=anchor
            ).rejection
            is R.UNRESOLVED_LINK
        )


async def test_a_refresh_failure_keeps_the_previous_snapshot() -> None:
    index = SqlLiveLinkIndex(_Broken())  # type: ignore[arg-type]
    await index.refresh()  # must never raise: it runs behind an ASGI handler
    assert index.size == 0
    assert index.resolve_attr(plan_id=plan_id(1), path="/a").rejection is R.LINK_NOT_LIVE


async def test_a_snapshot_satisfies_the_protocol_refresh() -> None:
    snapshot = LiveLinkSnapshot([live_plan(1)])
    await snapshot.refresh()
    assert len(snapshot) == 1


class _Broken:
    """A sessionmaker whose every call fails (an unreachable database)."""

    def __call__(self) -> Any:
        raise ConnectionError("database down")
