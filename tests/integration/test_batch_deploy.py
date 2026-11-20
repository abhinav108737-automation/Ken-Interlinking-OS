"""Bulk deploy / rollback over the REAL service, database and in-memory CMS.

The unit tests cover the argument surface; these cover the behaviour that only shows up against
a database: which plans the selector picks, that a dry run writes nothing, that an execute run
is idempotent on replay, and that ``rollback --all-live`` finds exactly the live deployments and
reverses them. This is the path that will carry the Ken Consulting 100-report run, so it is
tested against the same machinery, not against a mock of it.
"""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path
from typing import Any

import pytest

from ken_contracts.recommendations import LinkPlanStatus
from ken_services.deployment import batch_cli as cli
from ken_services.deployment.batch_cli import (
    Tally,
    apply_operation_id,
    approved_by_source,
    live_deployments,
)
from tests.integration.deployment_support import World, world
from tests.integration.recommendation_support import CHARGING, EV, LI, SM, rows

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

S = LinkPlanStatus


@pytest.fixture
async def w(sessionmaker: SM, tmp_path: Path) -> World:
    return await world(sessionmaker, tmp_path)


def _args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "command": "apply",
        "sources": None,
        "max_sources": cli.DEFAULT_MAX_SOURCES,
        "stop_after": cli.DEFAULT_STOP_AFTER,
        "pause_s": 0.0,
        "execute": False,
        "out": None,
        "ids": None,
        "all_live": True,
        "reason": "editorial_reversal",
        "note": "",
        "max_deployments": cli.HARD_MAX_SOURCES,
    }
    return argparse.Namespace(**{**base, **overrides})


async def _ev_plans(w: World) -> list[uuid.UUID]:
    """The EV page's two plans (charging infrastructure, lithium-ion batteries), approved."""
    return await w.approve(w.plans[(EV, CHARGING)], w.plans[(EV, LI)])


async def _plan_statuses(sessionmaker: SM) -> dict[str, int]:
    found = await rows(sessionmaker, "SELECT status, count(*) AS n FROM link_plans GROUP BY status")
    return {r.status: r.n for r in found}


async def _deployments(sessionmaker: SM) -> list[Any]:
    return await rows(sessionmaker, "SELECT id, operation_id, status FROM deployments")


def _cms_bodies(w: World) -> dict[str, str]:
    return {page.page_id: page.body_html for page in w.mock._pages.values()}


# --------------------------------------------------------------------------- #
# Selection: approval is the gate
# --------------------------------------------------------------------------- #
async def test_only_approved_plans_are_ever_selected(sessionmaker: SM, w: World) -> None:
    """A proposed plan must be invisible to the selector: approval is the human review gate,
    and a bulk command that could reach past it would make the gate decorative."""
    assert await approved_by_source(sessionmaker, None) == {}, "nothing is approved yet"

    ids = await _ev_plans(w)
    grouped = await approved_by_source(sessionmaker, None)

    assert sum(len(found) for found in grouped.values()) == len(ids) == 2
    assert len(grouped) == 1, "both plans belong to the one EV source page"
    (selected,) = grouped.values()
    assert sorted(selected) == sorted(ids)


async def test_a_source_filter_narrows_the_selection(sessionmaker: SM, w: World) -> None:
    await _ev_plans(w)
    grouped = await approved_by_source(sessionmaker, None)
    (only,) = grouped

    assert await approved_by_source(sessionmaker, [only]) == grouped
    assert await approved_by_source(sessionmaker, [uuid.uuid4()]) == {}


# --------------------------------------------------------------------------- #
# A dry run writes nothing
# --------------------------------------------------------------------------- #
async def test_a_dry_run_changes_no_plan_no_deployment_and_no_cms_page(
    sessionmaker: SM, w: World
) -> None:
    await _ev_plans(w)
    before_html = _cms_bodies(w)
    before_status = await _plan_statuses(sessionmaker)

    code = await cli._apply(w.service(), sessionmaker, w.operator, _args(execute=False))

    assert code == cli.EXIT_OK
    assert await _plan_statuses(sessionmaker) == before_status, "no plan moved"
    assert await _deployments(sessionmaker) == []
    assert _cms_bodies(w) == before_html, "the CMS was never written to"


# --------------------------------------------------------------------------- #
# Execute, and replay
# --------------------------------------------------------------------------- #
async def test_execute_deploys_and_a_re_run_replays_instead_of_deploying_twice(
    sessionmaker: SM, w: World
) -> None:
    """The deterministic operation id is what makes a re-run safe: an interrupted batch can be
    restarted without first working out which pages already went out."""
    await _ev_plans(w)

    assert (
        await cli._apply(w.service(), sessionmaker, w.operator, _args(execute=True)) == cli.EXIT_OK
    )
    first = await _deployments(sessionmaker)
    assert len(first) == 1
    after_first = _cms_bodies(w)

    assert (
        await cli._apply(w.service(), sessionmaker, w.operator, _args(execute=True)) == cli.EXIT_OK
    )
    second = await _deployments(sessionmaker)

    assert [r.id for r in second] == [r.id for r in first], "no second deployment was created"
    assert _cms_bodies(w) == after_first, "and the page was not edited a second time"


async def test_the_operation_id_is_derived_from_the_plans_that_were_deployed(
    sessionmaker: SM, w: World
) -> None:
    ids = await _ev_plans(w)
    await cli._apply(w.service(), sessionmaker, w.operator, _args(execute=True))
    (stored,) = await _deployments(sessionmaker)
    assert stored.operation_id == apply_operation_id(ids)


async def test_the_deployed_link_is_actually_on_the_cms_page(sessionmaker: SM, w: World) -> None:
    before = _cms_bodies(w)
    await _ev_plans(w)
    await cli._apply(w.service(), sessionmaker, w.operator, _args(execute=True))
    changed = [key for key, html in _cms_bodies(w).items() if html != before[key]]
    assert len(changed) == 1, "exactly the one source page changed"
    assert "<a " in _cms_bodies(w)[changed[0]]


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #
async def test_all_live_finds_the_live_deployments_and_reverses_them(
    sessionmaker: SM, w: World
) -> None:
    clean = _cms_bodies(w)
    await _ev_plans(w)
    await cli._apply(w.service(), sessionmaker, w.operator, _args(execute=True))
    assert len(await live_deployments(sessionmaker, None)) == 1

    code = await cli._rollback(
        w.service(), sessionmaker, w.operator, _args(command="rollback", execute=True)
    )

    assert code == cli.EXIT_OK
    assert await live_deployments(sessionmaker, None) == [], "nothing is live any more"
    assert {r.status for r in await _deployments(sessionmaker)} == {"rolled_back"}
    assert _cms_bodies(w) == clean, "every page is back to the bytes it started with"


async def test_a_rollback_dry_run_reverses_nothing(sessionmaker: SM, w: World) -> None:
    await _ev_plans(w)
    await cli._apply(w.service(), sessionmaker, w.operator, _args(execute=True))
    deployed = _cms_bodies(w)

    code = await cli._rollback(
        w.service(), sessionmaker, w.operator, _args(command="rollback", execute=False)
    )

    assert code == cli.EXIT_OK
    assert await live_deployments(sessionmaker, None) != [], "still live"
    assert _cms_bodies(w) == deployed


async def test_rolling_back_twice_is_idempotent(sessionmaker: SM, w: World) -> None:
    await _ev_plans(w)
    await cli._apply(w.service(), sessionmaker, w.operator, _args(execute=True))
    live = await live_deployments(sessionmaker, None)
    args = _args(command="rollback", execute=True, ids=live)

    assert await cli._rollback(w.service(), sessionmaker, w.operator, args) == cli.EXIT_OK
    # The second pass selects nothing, because nothing is live: that is the idempotence.
    assert await cli._rollback(w.service(), sessionmaker, w.operator, args) == cli.EXIT_OK
    assert {r.status for r in await _deployments(sessionmaker)} == {"rolled_back"}


# --------------------------------------------------------------------------- #
# The breaker, against a CMS that is actually down
# --------------------------------------------------------------------------- #
async def test_a_dead_cms_trips_the_breaker_and_deploys_nothing(sessionmaker: SM, w: World) -> None:
    await _ev_plans(w)
    w.cms.unavailable = True

    code = await cli._apply(
        w.service(), sessionmaker, w.operator, _args(execute=True, stop_after=1)
    )

    assert code == cli.EXIT_RUN_ERRORS
    assert await _deployments(sessionmaker) == []


def test_a_success_between_failures_resets_the_breaker() -> None:
    tally = Tally()
    for failed in (True, True, False, True, True):
        tally.record({"x": 1}, failed=failed, stop_after=3)
    assert not tally.tripped
    tally.record({"x": 1}, failed=True, stop_after=3)
    assert tally.tripped
