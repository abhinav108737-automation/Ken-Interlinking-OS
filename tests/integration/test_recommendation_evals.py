"""Phase 3 recommendation evals, end to end on the ``*_test`` database (ADR-0011).

The demo JSONL is ingested with the real ``FileExportContentSource`` and ``run_ingestion``,
analysed by ``run_analysis`` with ``MockModelProvider`` (semantic floor 0.3 for the mock),
then ``run_recommendations`` proposes plans. The plans are scored against the spec-derived
judgments in ``tests/fixtures/demo/recommendation_judgments.json``:

* every must-not rule is a HARD GATE (zero tolerance);
* precision@3, recall of GOOD, BAD rate and anchor exact match have conservative floors
  (``FLOORS``; derivation in evals/recommendations/README.md);
* a second run proposes nothing (idempotency) and every plan's locator round-trips through
  ``check_plan_freshness``;
* a synthetic placement-safety page proves anchors never land under excluded markup.

Offline eval (RUN_OFFLINE_EVAL, ALLOW): no network, no learning, nothing promoted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.inventory import RunStatus
from ken_contracts.recommendations import PlacementLocator, PlanFreshness
from ken_core.providers.model.mock import MockModelProvider
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.recommendations.freshness import check_plan_freshness
from ken_services.recommendations.pipeline import load_policies
from ken_services.recommendations.reader import SqlRecommendationReader
from tests.conftest import make_settings
from tests.unit.recommendation_evals import db_flow
from tests.unit.recommendation_evals.judgments import (
    CORPUS_PATH,
    PageState,
    PlanRecord,
    corpus_with_safety_page,
    judgments,
    locate_block,
    must_not_violations,
    quality_metrics,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SM = async_sessionmaker[AsyncSession]

FLOORS: dict[str, float] = {
    "precision_at_3_macro": 0.90,
    "recall_good_macro": 0.70,
    "anchor_exact_match": 0.85,
}
"""Regression floors (a gate, not a target).

History (Phase 3, 2026-10-14): the self-describing-anchor rule (ADR-0011 section 9.1: no
link anchored on the SOURCE page's own market name unless the block names the target's
distinctive geography, except on a commercial hop) dropped recall of the ORIGINAL GOOD set
from 0.92 to 0.20, because most of those GOOD pairs expected exactly that anchor. QA (M3)
found the labels contradicted the rule; the 45 affected pairs were relabelled ACCEPTABLE by
the deterministic rule R5 in the judgments file (tagged in ``why``; still judged positives,
so precision is unchanged) and the recall floor was RESTORED to a meaningful 0.70 on the
remaining 30 GOOD pairs. Measured (mock): precision@3 0.98, recall 0.78, anchor exact
match 1.00, BAD rate 0.021. Whether same-market cross-country links deserve an anchor at
all is an open editorial decision for the product owner (NEXT_STEPS)."""
MAX_BAD_RATE = 0.03
"""Ceiling on the share of judged plans whose target is BAD (mock 0.022, nomic see README)."""
MAX_UNJUDGED_RATE = 0.05
"""Ceiling on plans to targets the judgments do not cover (keeps the metrics meaningful)."""
MIN_SOURCES_WITH_PLANS = 25
"""Of 46 judged sources, 30 receive plans under the context rule (42-43 before it)."""
MIN_ANCHOR_PAIRS_PROPOSED = 5
"""The exact-anchor metric needs enough of its 24 pairs to be proposed to mean anything."""


def _settings(test_database_url: str) -> Any:
    return make_settings(
        database_url=test_database_url,
        model_provider="mock",
        similarity_min_score=db_flow.MOCK_SIMILARITY_MIN_SCORE,
    )


async def _pipeline(sessionmaker: SM, settings: Any, corpus: Path) -> dict[str, Any]:
    provider = MockModelProvider(settings)
    try:
        await db_flow.ingest_corpus(sessionmaker, corpus)
        analysis = await db_flow.analyse(sessionmaker, settings, provider)
        assert analysis.status is RunStatus.SUCCEEDED, analysis.counters
        run = await db_flow.recommend(sessionmaker, settings, provider)
    finally:
        await provider.aclose()
    assert run.status is RunStatus.SUCCEEDED, run.counters
    return {
        "run": run,
        "plans": await db_flow.load_plans(sessionmaker),
        "pages": await db_flow.load_pages(sessionmaker),
    }


def _gate(plans: list[PlanRecord], pages: dict[str, PageState], settings: Any) -> None:
    policy, _ranking = load_policies(settings)
    taxonomy = load_taxonomy(settings.taxonomy_path)
    violations = must_not_violations(plans, pages, policy=policy, taxonomy=taxonomy)
    assert violations == {}, json.dumps(violations, indent=2)


async def test_demo_corpus_recommendations_pass_every_gate(
    sessionmaker: SM, test_database_url: str
) -> None:
    settings = _settings(test_database_url)
    result = await _pipeline(sessionmaker, settings, CORPUS_PATH)
    run, plans, pages = result["run"], result["plans"], result["pages"]
    assert plans, "the engine proposed nothing on the demo corpus"
    assert run.counters["plans_created"] == len(plans)

    # 1. Must-not rules: hard gates over EVERY plan.
    _gate(plans, pages, settings)

    # 2. Quality metrics against the judgments (printed for -rA).
    policy, _ranking = load_policies(settings)
    taxonomy = load_taxonomy(settings.taxonomy_path)
    metrics = quality_metrics(
        plans, policy=policy, taxonomy=taxonomy, target_markets=db_flow.target_markets(pages)
    )
    summary = {k: v for k, v in metrics.items() if k != "per_source"}
    print(json.dumps(summary, indent=2))
    for name, floor in FLOORS.items():
        assert metrics[name] is not None and metrics[name] >= floor, (name, metrics[name])
    assert metrics["bad_rate"] is not None and metrics["bad_rate"] <= MAX_BAD_RATE, metrics[
        "bad_examples"
    ]
    assert metrics["unjudged_rate"] <= MAX_UNJUDGED_RATE, metrics["unjudged_examples"]
    assert metrics["sources_with_plans"] >= MIN_SOURCES_WITH_PLANS, metrics["sources_with_plans"]
    proposed, _total = map(int, metrics["anchor_pairs_proposed"].split("/"))
    assert proposed >= MIN_ANCHOR_PAIRS_PROPOSED, metrics["anchor_pairs_proposed"]

    # 3. Locators round-trip: the anchor is the block-text slice and the plan is fresh on
    #    the unchanged source (and rebasable if only the version number moved).
    for plan in plans:
        page = pages[plan.source_page_id]
        block = locate_block(plan, page)
        assert block is not None and block.text[plan.start : plan.end] == plan.anchor_text
        locator = PlacementLocator(
            block_id=plan.block_id,
            block_kind=plan.block_kind,
            occurrence_index=plan.occurrence_index,
            start=plan.start,
            end=plan.end,
            context_hash=plan.context_hash,
        )
        blocks = page.blocks()
        same = check_plan_freshness(
            plan_source_version=plan.source_page_version,
            current_version=page.current_version,
            locator=locator,
            anchor_text=plan.anchor_text,
            current_blocks=blocks,
            excluded_ancestors=policy.placement.excluded_ancestors,
        )
        assert same.freshness is PlanFreshness.FRESH, (plan.source_url, same)
        bumped = check_plan_freshness(
            plan_source_version=plan.source_page_version,
            current_version=page.current_version + 1,
            locator=locator,
            anchor_text=plan.anchor_text,
            current_blocks=blocks,
            excluded_ancestors=policy.placement.excluded_ancestors,
        )
        assert bumped.freshness is PlanFreshness.REBASABLE, (plan.source_url, bumped)

    # 4. The reader sees exactly these plans.
    reader = SqlRecommendationReader(sessionmaker=sessionmaker, settings=settings)
    listed = await reader.list_plans(limit=200, offset=0)
    assert listed.page.total == len(plans)
    assert {str(p.id) for p in listed.items} == {p.id for p in plans}

    # 5. Idempotency: a second run on unchanged pages proposes nothing and touches nothing.
    provider = MockModelProvider(settings)
    try:
        again = await db_flow.recommend(sessionmaker, settings, provider)
    finally:
        await provider.aclose()
    assert again.status is RunStatus.SUCCEEDED, again.counters
    assert again.counters["plans_created"] == 0, again.counters
    assert again.counters["plans_expired"] == 0, again.counters
    after = await db_flow.load_plans(sessionmaker)
    assert [(p.id, p.status) for p in after] == [(p.id, p.status) for p in plans]


async def test_placement_safety_page_never_anchors_under_excluded_markup(
    sessionmaker: SM, test_database_url: str, tmp_path: Path
) -> None:
    settings = _settings(test_database_url)
    corpus = corpus_with_safety_page(tmp_path / "with_safety_page.jsonl")
    result = await _pipeline(sessionmaker, settings, corpus)
    plans, pages = result["plans"], result["pages"]
    _gate(plans, pages, settings)

    safety = judgments()["placement_safety"]
    url = safety["record"]["url"]
    allowed = set(safety["allowed_block_texts"])
    mine = [p for p in plans if p.source_url == url]
    print(json.dumps([[p.anchor_text, p.target_url, p.block_kind] for p in mine], indent=1))
    for plan in mine:
        block = locate_block(plan, pages[plan.source_page_id])
        assert block is not None
        assert block.text in allowed, (plan.anchor_text, block.kind, block.text)
    assert len(mine) >= safety["min_plans_in_controls"], [p.anchor_text for p in mine]
