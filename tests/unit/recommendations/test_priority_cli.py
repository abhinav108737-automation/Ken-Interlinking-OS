"""The priority CLI's argument contract and report shape (Business Phase 2A-1).

Database behaviour is covered by ``tests/integration/test_priority_store.py``; here the CLI's
own guarantees are checked without one: every write names its actor, instants carry a
timezone, tiers and types are the governed sets, and the JSON report states its assumptions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from ken_core.priority import PriorityType
from ken_services.recommendations.priority_cli import EXIT_USAGE, main, report_json
from ken_services.recommendations.priority_shadow import (
    SHADOW_FORMULA,
    ShadowCandidate,
    shadow_report,
)
from ken_services.recommendations.priority_store import ShadowRunContext

PAGE = str(uuid.uuid4())
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "argv",
    [
        # add without an actor: every change must be attributable
        ["add", "--page-id", PAGE, "--type", "trending_market", "--tier", "1",
         "--reason", "r", "--owner", "o"],
        # an ungoverned type
        ["add", "--page-id", PAGE, "--type", "vip_page", "--tier", "1",
         "--reason", "r", "--owner", "o", "--actor", "a"],
        # a tier outside 1..5
        ["add", "--page-id", PAGE, "--type", "trending_market", "--tier", "6",
         "--reason", "r", "--owner", "o", "--actor", "a"],
        # a naive instant: a window without a timezone is ambiguous
        ["add", "--page-id", PAGE, "--type", "trending_market", "--tier", "1",
         "--reason", "r", "--owner", "o", "--actor", "a", "--effective-from", "2026-11-19T00:00"],
        # not a UUID
        ["retire", "--id", "not-a-uuid", "--actor", "a"],
        # retire without an actor
        ["retire", "--id", PAGE],
        # a naive retirement instant
        ["retire", "--id", PAGE, "--actor", "a", "--at", "2026-11-19T00:00"],
        # no command
        [],
    ],
)  # fmt: skip
def test_invalid_invocations_are_refused_before_touching_the_database(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == EXIT_USAGE
    capsys.readouterr()


@pytest.mark.parametrize("priority_type", list(PriorityType))
def test_every_governed_type_is_accepted_by_the_parser(priority_type: PriorityType) -> None:
    from ken_services.recommendations.priority_cli import _parser

    args = _parser().parse_args(
        ["add", "--page-id", PAGE, "--type", priority_type.value, "--tier", "3",
         "--reason", "r", "--owner", "o", "--actor", "a"]
    )  # fmt: skip
    assert args.type == priority_type.value
    assert args.effective_from is None, "defaults to now at execution, not at parse"


def test_retire_accepts_a_future_instant_so_a_handover_can_be_scheduled() -> None:
    from ken_services.recommendations.priority_cli import _parser

    args = _parser().parse_args(
        ["retire", "--id", PAGE, "--actor", "a", "--at", "2026-12-01T00:00:00+00:00"]
    )
    assert args.at == datetime(2026, 12, 1, tzinfo=UTC)
    assert _parser().parse_args(["retire", "--id", PAGE, "--actor", "a"]).at is None


def test_the_json_report_states_its_assumptions_and_that_nothing_changed() -> None:
    source = uuid.uuid4()
    candidate = ShadowCandidate(
        source_page_id=source,
        target_page_id=uuid.uuid4(),
        target_url="/x",
        live_score=0.5,
        live_selected=True,
    )
    report = shadow_report([candidate], designations={}, eligibility={}, at=NOW)
    context = ShadowRunContext(
        run_id=uuid.uuid4(),
        analysis_version="tax3-alg2",
        policy_version=1,
        ranking_policy_version=1,
        started_at=NOW,
    )

    data = report_json(context, report)

    assert data["live_scores_changed"] is False
    assert data["formula"] == SHADOW_FORMULA
    assert data["assumed_weight"] == report.assumed_weight
    assert data["shadowed_run"]["analysis_version"] == "tax3-alg2"
    assert data["shadowed_run"]["policy_version"] == 1
    assert data["summary"]["qualified_candidates"] == 1
    assert data["rows"][0]["target_url"] == "/x"
    assert set(data["summary"]) == {
        "qualified_candidates",
        "sources",
        "designated",
        "refused_by_eligibility",
        "rank_changes",
        "plan_set_changes",
        "max_priority_share",
    }
