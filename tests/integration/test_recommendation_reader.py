"""``SqlRecommendationReader`` against the ``*_test`` database.

Plans come from a real engine run over the synthetic corpus (so the block text, score JSON
and locators are real), plus hand-made plans for ordering and filtering edge cases.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ken_contracts.recommendations import (
    CandidateRejection,
    LinkPlanStatus,
    OpportunityKind,
    PlanNotFoundError,
    RecommendationReader,
)
from ken_services.inventory.hashing import sha256_hex
from ken_services.recommendations.reader import SqlRecommendationReader
from ken_services.recommendations.transitions import transition
from tests.integration.recommendation_support import (
    EV,
    LI,
    SETTINGS,
    SM,
    STORAGE,
    import_records,
    insert_plans,
    make_plan,
    page_ids,
    recommend,
    record,
    scalar,
    seed_corpus,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]


def _reader(sessionmaker: SM) -> SqlRecommendationReader:
    return SqlRecommendationReader(sessionmaker=sessionmaker, settings=SETTINGS)


async def test_the_reader_satisfies_the_protocol(sessionmaker: SM) -> None:
    assert isinstance(_reader(sessionmaker), RecommendationReader)


async def test_plans_list_detail_candidates_and_summary_over_a_real_run(
    sessionmaker: SM, tmp_path: Path
) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    run = await recommend(sessionmaker)
    reader = _reader(sessionmaker)
    ids = await page_ids(sessionmaker)

    plans = await reader.list_plans(limit=200, offset=0)
    assert plans.page.total == run.counters["plans_created"] == len(plans.items)
    keys = [(-p.score.total, p.created_at, p.id) for p in plans.items]
    assert keys == sorted(keys)  # total score desc, then created_at, then id
    for plan in plans.items:
        assert plan.score.total == pytest.approx(plan.confidence)
        assert plan.locator.end - plan.locator.start == len(plan.anchor_text)

    ev_li = next(
        p for p in plans.items if (p.source_page_id, p.target_page_id) == (ids[EV], ids[LI])
    )
    detail = await reader.get_plan(plan_id=ev_li.id)
    assert detail.id == ev_li.id
    assert detail.source_title == "India Electric Vehicles Market Outlook 2030"
    assert sha256_hex(detail.source_block_text) == detail.locator.context_hash
    block = detail.source_block_text
    assert block[detail.locator.start : detail.locator.end] == detail.anchor_text
    assert detail.preview.anchor == detail.anchor_text
    assert block.startswith(detail.preview.before)
    assert [(t.from_status, t.to_status) for t in detail.transitions] == [
        (None, LinkPlanStatus.PROPOSED)
    ]
    assert detail.score.factors["placement_quality"] > 0
    assert detail.reason_codes  # structured, never free text

    # Filters.
    by_source = await reader.list_plans(limit=50, offset=0, source_page_id=ids[EV])
    assert {p.source_page_id for p in by_source.items} == {ids[EV]}
    by_target = await reader.list_plans(limit=50, offset=0, target_page_id=ids[STORAGE])
    assert by_target.items and {p.target_page_id for p in by_target.items} == {ids[STORAGE]}
    top = plans.items[0].score.total
    high = await reader.list_plans(limit=50, offset=0, min_score=top)
    assert high.items and all(p.score.total >= top for p in high.items)
    paged = await reader.list_plans(limit=2, offset=1)
    assert [p.id for p in paged.items] == [p.id for p in plans.items[1:3]]
    assert paged.page.total == plans.page.total

    candidates = await reader.list_candidates(limit=200, offset=0, run_id=run.id)
    assert candidates.page.total == run.counters["candidates"]
    kept = await reader.list_candidates(limit=200, offset=0, rejected=False)
    rejected = await reader.list_candidates(limit=200, offset=0, rejected=True)
    assert kept.page.total == run.counters["plans_created"]
    assert all(c.plan_id is not None and c.rejection is None for c in kept.items)
    assert all(c.plan_id is None and c.rejection is not None for c in rejected.items)
    assert kept.page.total + rejected.page.total == candidates.page.total
    assert {c.rejection for c in rejected.items} >= {CandidateRejection.MARKET_UNRELATED}
    ev_candidates = await reader.list_candidates(limit=200, offset=0, source_page_id=ids[EV])
    assert ev_candidates.items and {c.source_page_id for c in ev_candidates.items} == {ids[EV]}
    assert all(c.target_url.startswith("https://site.test/") for c in ev_candidates.items)

    opportunities = await reader.list_opportunities(limit=200, offset=0)
    assert opportunities.page.total == run.counters["opportunities"]
    scores = [o.score for o in opportunities.items]
    assert scores == sorted(scores, reverse=True)
    orphans = await reader.list_opportunities(limit=200, offset=0, kind=OpportunityKind.ORPHAN)
    assert orphans.items and {o.kind for o in orphans.items} == {OpportunityKind.ORPHAN}
    assert all(o.run_id == run.id for o in orphans.items)

    summary = await reader.get_summary()
    assert summary.plans_by_status == {"proposed": run.counters["plans_created"]}
    assert summary.last_run_id == run.id
    assert summary.sources_with_plans == len({p.source_page_id for p in plans.items})
    assert summary.targets_with_plans == len({p.target_page_id for p in plans.items})
    assert sum(summary.opportunities_by_kind.values()) == run.counters["opportunities"]
    assert (summary.policy_version, summary.ranking_policy_version) == (1, 1)


async def test_unknown_plan_and_paging_errors(sessionmaker: SM) -> None:
    reader = _reader(sessionmaker)
    with pytest.raises(PlanNotFoundError):
        await reader.get_plan(plan_id=uuid.uuid4())
    for limit, offset in ((0, 0), (201, 0), (10, -1)):
        with pytest.raises(ValueError):
            await reader.list_plans(limit=limit, offset=offset)
        with pytest.raises(ValueError):
            await reader.list_candidates(limit=limit, offset=offset)
        with pytest.raises(ValueError):
            await reader.list_opportunities(limit=limit, offset=offset)


async def test_empty_database_summary(sessionmaker: SM) -> None:
    summary = await _reader(sessionmaker).get_summary()
    assert summary.plans_by_status == {} and summary.opportunities_by_kind == {}
    assert summary.last_run_id is None
    assert (summary.sources_with_plans, summary.targets_with_plans) == (0, 0)


async def test_status_filter_ordering_ties_and_history(sessionmaker: SM, tmp_path: Path) -> None:
    await import_records(
        sessionmaker,
        tmp_path,
        [record(p, title=t, paragraphs=["<p>Batteries power electric vehicles.</p>"])
         for p, t in (("/a", "A"), ("/b", "B"), ("/c", "C"))],
    )  # fmt: skip
    ids = await page_ids(sessionmaker)
    t0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    tie_first = make_plan(ids["/a"], ids["/b"], total=0.5, created_at=t0)
    tie_second = make_plan(
        ids["/a"],
        ids["/c"],
        total=0.5,
        created_at=t0 + timedelta(seconds=1),
        block_text="Electric vehicles need batteries.",
        anchor="batteries",
    )
    best = make_plan(ids["/b"], ids["/a"], total=0.9, created_at=t0 + timedelta(seconds=2))
    await insert_plans(sessionmaker, tie_first, tie_second, best)
    async with sessionmaker() as session, session.begin():
        await transition(
            session,
            plan_id=best.id,
            to_status=LinkPlanStatus.REJECTED,
            actor="user:reviewer",
            note="off topic",
        )
    reader = _reader(sessionmaker)
    listed = await reader.list_plans(limit=10, offset=0)
    assert [p.id for p in listed.items] == [best.id, tie_first.id, tie_second.id]
    rejected = await reader.list_plans(limit=10, offset=0, status=LinkPlanStatus.REJECTED)
    assert [p.id for p in rejected.items] == [best.id]
    assert rejected.items[0].status is LinkPlanStatus.REJECTED
    detail = await reader.get_plan(plan_id=best.id)
    assert [(t.to_status, t.actor, t.note) for t in detail.transitions] == [
        (LinkPlanStatus.PROPOSED, "system:test", None),
        (LinkPlanStatus.REJECTED, "user:reviewer", "off topic"),
    ]
    assert detail.source_block_text == "Batteries power electric vehicles."
    summary = await reader.get_summary()
    assert summary.plans_by_status == {"proposed": 2, "rejected": 1}
    assert await scalar(sessionmaker, "SELECT count(*) FROM link_plans") == 3
