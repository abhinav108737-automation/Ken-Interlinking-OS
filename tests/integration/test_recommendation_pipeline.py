"""The recommendation job on the ``*_test`` database, end to end with the REAL modules.

Real file import + normaliser, real Phase 2 analysis (real taxonomy) with the deterministic
``MockModelProvider``, and workstream A's real filter / score / placement / freshness /
rationale modules. Fakes are injected only to force a failure or an experiment lock.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from ken_contracts.events import EventName
from ken_contracts.intelligence import ProtectionSource, analysis_version
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind
from ken_contracts.recommendations import (
    ENGINE_VERSION,
    CandidateRejection,
    LinkPlanEvent,
    LinkPlanStatus,
    ReasonCode,
    RecommendationPolicy,
    RetrievalChannel,
)
from ken_core.db.repositories import recommendations as rec_repo
from ken_core.db.repositories.events import list_events
from ken_core.db.repositories.intelligence import insert_protection
from ken_services.intelligence.embeddings import embedding_identity
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.inventory.hashing import sha256_hex
from ken_services.inventory.normalize import extract_blocks
from ken_services.recommendations.channels import graph_channel, lexical_channel
from ken_services.recommendations.pipeline import (
    EMITTED_BY,
    RECOMMENDATION_COUNTERS,
    RecommendationToolkit,
    load_policies,
)
from ken_services.recommendations.transitions import ENGINE_ACTOR, transition
from tests.integration.recommendation_support import (
    CHARGING,
    EV,
    GERMANY_EV,
    GLOBAL_EV,
    SITE,
    LI,
    SETTINGS,
    SM,
    SOLAR,
    STORAGE,
    _p,
    analyse,
    import_records,
    page_ids,
    provider,
    recommend,
    sibling_records,
    record,
    rows,
    scalar,
    seed_corpus,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

AV = analysis_version(load_taxonomy(SETTINGS.taxonomy_path).version)
POLICY, RANKING = load_policies(SETTINGS)


def _policy(**selection: Any) -> RecommendationPolicy:
    return POLICY.model_copy(update={"selection": POLICY.selection.model_copy(update=selection)})


async def _plans(sessionmaker: SM) -> dict[tuple[str, str], Any]:
    """(source path, target path) -> plan row, OPEN plans only."""
    ids = {v: k for k, v in (await page_ids(sessionmaker)).items()}
    found = await rows(
        sessionmaker,
        "SELECT * FROM link_plans WHERE status IN ('proposed', 'critiqued', 'policy_checked', "
        "'awaiting_approval', 'approved', 'validated')",
    )
    return {(ids[r.source_page_id], ids[r.target_page_id]): r for r in found}


async def _candidates(sessionmaker: SM, run_id: uuid.UUID) -> dict[tuple[str, str], Any]:
    ids = {v: k for k, v in (await page_ids(sessionmaker)).items()}
    found = await rows(sessionmaker, "SELECT * FROM link_candidates WHERE run_id = :r", r=run_id)
    return {(ids[r.source_page_id], ids[r.target_page_id]): r for r in found}


def _assert_counters_consistent(counters: dict[str, int], candidates: int) -> None:
    assert set(counters) == set(RECOMMENDATION_COUNTERS)
    rejected = sum(v for k, v in counters.items() if k.startswith("rejected_"))
    assert counters["candidates"] == candidates == rejected + counters["plans_created"]


# --------------------------------------------------------------------------- #
# First run
# --------------------------------------------------------------------------- #
async def test_first_run_creates_natural_explainable_plans(
    sessionmaker: SM, tmp_path: Path
) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    detail = await recommend(sessionmaker)
    assert detail.kind is JobKind.RECOMMENDATION
    assert detail.status is RunStatus.SUCCEEDED, detail.counters
    c = detail.counters
    count = await scalar(sessionmaker, "SELECT count(*) FROM link_candidates")
    _assert_counters_consistent(c, count)
    assert c["sources_considered"] == 5
    assert c["sources_skipped_ineligible"] == 0
    assert c["errors"] == 0
    assert c["plans_created"] == await scalar(sessionmaker, "SELECT count(*) FROM link_plans") > 0

    plans = await _plans(sessionmaker)
    # The anchor is the TARGET's own market keyword, verbatim from the source text -- never a
    # bare noun ("lithium-ion batteries") and never the broad market_id.
    assert plans[(EV, LI)].anchor_text == "lithium-ion batteries market"
    assert plans[(EV, CHARGING)].anchor_text == "EV charging market"
    assert plans[(STORAGE, SOLAR)].anchor_text == "solar power market"
    # Every page here is an India report, so no anchor needs a geography label.
    assert all(
        ReasonCode.ANCHOR_IS_TARGET_MARKET_KEYWORD.value in plan.reason_codes
        and ReasonCode.SAME_GEOGRAPHY.value in plan.reason_codes
        and ReasonCode.ANCHOR_NAMES_TARGET_GEOGRAPHY.value not in plan.reason_codes
        for plan in plans.values()
    )
    # Vehicles and solar power are unrelated markets: the gate refuses the pair outright.
    assert (EV, SOLAR) not in plans
    cands = await _candidates(sessionmaker, detail.id)
    assert cands[(EV, SOLAR)].rejection == CandidateRejection.MARKET_UNRELATED.value

    identity = await embedding_identity(provider())
    ids = await page_ids(sessionmaker)
    for (source, _target), plan in plans.items():
        body = await scalar(
            sessionmaker,
            "SELECT body_html FROM page_versions WHERE page_id = :p AND version = :v",
            p=ids[source],
            v=plan.source_page_version,
        )
        blocks = {b.block_id: b for b in extract_blocks(body)}
        block = blocks[plan.block_id]
        assert block.kind == plan.block_kind and block.kind in ("p", "li")
        assert block.text[plan.start_offset : plan.end_offset] == plan.anchor_text
        assert sha256_hex(block.text) == plan.context_hash
        assert plan.status == LinkPlanStatus.PROPOSED.value
        assert plan.engine_version == ENGINE_VERSION
        assert (plan.policy_version, plan.ranking_policy_version) == (
            POLICY.version,
            RANKING.version,
        )
        assert plan.analysis_version == AV
        assert plan.embedding_model == identity.model
        assert plan.calibration_version is None and plan.reasoning_model is None
        assert plan.confidence == plan.total_score == plan.score["total"]
        assert plan.score["factors"]["placement_quality"] > 0
        assert plan.run_id == detail.id
        assert plan.rationale.startswith("Link to ")
        assert cands[(source, _target)].plan_id == plan.id
        assert cands[(source, _target)].rejection is None
    # Every kept candidate is a plan; every rejected one is not.
    assert {k for k, v in cands.items() if v.rejection is None} == set(plans)


async def test_plans_events_and_transitions_are_written_together(
    sessionmaker: SM, tmp_path: Path
) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    detail = await recommend(sessionmaker)
    plans = await _plans(sessionmaker)
    history = await rows(
        sessionmaker, "SELECT plan_id, from_status, to_status, actor FROM link_plan_transitions"
    )
    assert sorted(h.plan_id for h in history) == sorted(p.id for p in plans.values())
    assert {(h.from_status, h.to_status, h.actor) for h in history} == {
        (None, "proposed", ENGINE_ACTOR)
    }
    async with sessionmaker() as session:
        events = await list_events(session, event_name=EventName.LINK_PROPOSED, limit=500)
        completed = await list_events(session, event_name=EventName.JOB_COMPLETED, limit=500)
    assert sorted(str(e.subject_id) for e in events) == sorted(str(p.id) for p in plans.values())
    for event in events:
        assert event.emitted_by == EMITTED_BY
        assert event.subject_type == "link_plan"
        assert event.correlation_id == detail.id
        payload = LinkPlanEvent.model_validate(event.payload)
        plan = next(p for p in plans.values() if p.id == payload.plan_id)
        assert payload.anchor_text == plan.anchor_text
        assert payload.status is LinkPlanStatus.PROPOSED
        assert payload.run_id == detail.id and payload.actor == ENGINE_ACTOR
        assert payload.score == pytest.approx(plan.total_score)
    assert [e.payload["kind"] for e in completed if e.subject_id == str(detail.id)] == [
        "recommendation"
    ]


async def test_rerun_on_unchanged_pages_is_idempotent(sessionmaker: SM, tmp_path: Path) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    first = await recommend(sessionmaker)
    second = await recommend(sessionmaker)
    assert second.status is RunStatus.SUCCEEDED
    assert second.counters["plans_created"] == 0
    assert second.counters["rejected_duplicate_recommendation"] == first.counters["plans_created"]
    assert (
        await scalar(sessionmaker, "SELECT count(*) FROM link_plans")
        == (first.counters["plans_created"])
    )
    # The audit is per run: both runs recorded their candidates.
    assert await scalar(sessionmaker, "SELECT count(DISTINCT run_id) FROM link_candidates") == 2


async def test_without_a_provider_the_semantic_channel_is_off(
    sessionmaker: SM, tmp_path: Path
) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    detail = await recommend(sessionmaker, model_provider=None)
    assert detail.status is RunStatus.SUCCEEDED
    assert detail.config_snapshot["embedding"] is None
    channels = await rows(sessionmaker, "SELECT channels FROM link_candidates")
    assert all(RetrievalChannel.SEMANTIC.value not in ch for (ch,) in channels)
    assert (
        await scalar(
            sessionmaker, "SELECT count(*) FROM link_plans WHERE embedding_model IS NOT NULL"
        )
        == 0
    )
    assert await scalar(sessionmaker, "SELECT count(*) FROM link_plans") > 0


async def test_explicit_sources_limit_the_run(sessionmaker: SM, tmp_path: Path) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    ids = await page_ids(sessionmaker)
    detail = await recommend(sessionmaker, source_page_ids=[ids[EV]])
    assert detail.counters["sources_considered"] == 1
    sources = await rows(sessionmaker, "SELECT DISTINCT source_page_id FROM link_plans")
    assert [s for (s,) in sources] == [ids[EV]]
    assert detail.config_snapshot["source_page_ids"] == [str(ids[EV])]


# --------------------------------------------------------------------------- #
# Freshness: a new source version
# --------------------------------------------------------------------------- #
async def test_source_change_expires_stale_plans_and_keeps_rebasable_ones(
    sessionmaker: SM, tmp_path: Path
) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    await recommend(sessionmaker)
    before = await _plans(sessionmaker)
    li_plan, charging_plan = before[(EV, LI)], before[(EV, CHARGING)]

    # v2 of the EV report: a NEW opening paragraph (every block id shifts) and the
    # lithium-ion paragraph rewritten; the charging paragraph is untouched.
    from tests.integration.recommendation_support import EV_MARKET_PARAS

    paragraphs = [
        _p("This edition adds the latest registration data for every state of the country."),
        EV_MARKET_PARAS[0],
        _p(
            "Vehicle makers still import most of their cells, although the lithium-ion "
            "batteries market at home is expected to grow through the coming decade."
        ),
        *EV_MARKET_PARAS[2:],
    ]
    await import_records(
        sessionmaker, tmp_path, [record(EV, paragraphs=paragraphs)], name="v2.jsonl"
    )
    await analyse(sessionmaker)
    detail = await recommend(sessionmaker)
    assert detail.status is RunStatus.SUCCEEDED, detail.counters
    assert detail.counters["plans_expired"] == 1

    expired = await rows(
        sessionmaker,
        "SELECT status FROM link_plans WHERE id = :id",
        id=li_plan.id,
    )
    assert expired[0].status == LinkPlanStatus.EXPIRED.value
    note = await scalar(
        sessionmaker,
        "SELECT note FROM link_plan_transitions WHERE plan_id = :id AND to_status = 'expired'",
        id=li_plan.id,
    )
    assert note.startswith("stale: version_changed")
    # Rebasable: same block TEXT, anchor at the same offsets -> kept, still on v1.
    after = await _plans(sessionmaker)
    assert after[(EV, CHARGING)].id == charging_plan.id
    assert after[(EV, CHARGING)].source_page_version == 1
    # The stale pair is regenerated on v2 from the new natural mention.
    regenerated = after[(EV, LI)]
    assert regenerated.id != li_plan.id
    assert regenerated.source_page_version == 2
    assert regenerated.anchor_text == "lithium-ion batteries market"
    assert regenerated.start_offset != li_plan.start_offset


# --------------------------------------------------------------------------- #
# Pair filters
# --------------------------------------------------------------------------- #
async def test_previously_rejected_pairs_cool_down(sessionmaker: SM, tmp_path: Path) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    await recommend(sessionmaker)
    plan = (await _plans(sessionmaker))[(EV, LI)]
    async with sessionmaker() as session, session.begin():
        await transition(
            session, plan_id=plan.id, to_status=LinkPlanStatus.REJECTED, actor="user:reviewer-1"
        )
    again = await recommend(sessionmaker)
    cands = await _candidates(sessionmaker, again.id)
    assert cands[(EV, LI)].rejection == CandidateRejection.PREVIOUSLY_REJECTED.value
    assert (EV, LI) not in await _plans(sessionmaker)

    # With no cooldown the pair is proposed again, as a NEW plan (never resurrected).
    third = await recommend(sessionmaker, policy=_policy(rejection_cooldown_days=0))
    assert third.counters["rejected_previously_rejected"] == 0
    reproposed = (await _plans(sessionmaker))[(EV, LI)]
    assert reproposed.id != plan.id
    status = await scalar(sessionmaker, "SELECT status FROM link_plans WHERE id = :i", i=plan.id)
    assert status == "rejected"


async def test_already_linked_targets_are_excluded_including_via_redirect_alias(
    sessionmaker: SM, tmp_path: Path
) -> None:
    from tests.integration.recommendation_support import CHARGING_PARAS, LI_PARAS, SITE

    li = list(LI_PARAS)
    li.append(_p('See also the <a href="/reports/india-electric-vehicles-market">EV study</a>.'))
    charging = list(CHARGING_PARAS)
    charging.append(_p('Background: <a href="/old/ev-report">our earlier vehicle study</a>.'))
    await import_records(
        sessionmaker,
        tmp_path,
        [
            record(EV),
            record(LI, paragraphs=li),
            record(CHARGING, paragraphs=charging),
            record(STORAGE),
            record(SOLAR),
        ],
    )
    await analyse(sessionmaker)
    ids = await page_ids(sessionmaker)
    async with sessionmaker() as session, session.begin():
        from sqlalchemy import text

        # What a crawl records for a retired URL that now redirects to the EV report.
        await session.execute(
            text(
                "INSERT INTO known_urls (url, page_id, outcome, http_status, first_seen_at, "
                "last_seen_at) VALUES (:u, :p, 'redirect', 301, now(), now()) "
                "ON CONFLICT (url) DO UPDATE SET page_id = :p, outcome = 'redirect'"
            ),
            {"u": SITE + "/old/ev-report", "p": ids[EV]},
        )
    detail = await recommend(sessionmaker)
    cands = await _candidates(sessionmaker, detail.id)
    assert cands[(LI, EV)].rejection == CandidateRejection.ALREADY_LINKED.value
    assert cands[(CHARGING, EV)].rejection == CandidateRejection.ALREADY_LINKED.value
    plans = await _plans(sessionmaker)
    assert (LI, EV) not in plans and (CHARGING, EV) not in plans
    # Inbound links now count for the EV report: no longer an orphan.
    orphan = await scalar(
        sessionmaker,
        "SELECT count(*) FROM opportunities WHERE page_id = :p AND kind = 'orphan'",
        p=ids[EV],
    )
    assert orphan == 0


async def test_protected_pages_are_never_linked_to_or_from(
    sessionmaker: SM, tmp_path: Path
) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    ids = await page_ids(sessionmaker)
    async with sessionmaker() as session, session.begin():
        await insert_protection(
            session,
            page_id=ids[LI],
            source=ProtectionSource.MANUAL.value,
            rule_id=None,
            reason="Legal review in progress.",
            created_by="user:editor",
            created_at=datetime.now(UTC),
        )
    detail = await recommend(sessionmaker)
    assert detail.counters["sources_skipped_ineligible"] == 1  # LI is not a source
    cands = await _candidates(sessionmaker, detail.id)
    into = [v.rejection for (s, t), v in cands.items() if t == LI]
    assert into and set(into) == {CandidateRejection.TARGET_PROTECTED.value}
    assert all(s != LI for s, _ in cands)
    plans = await _plans(sessionmaker)
    assert all(LI not in pair for pair in plans)

    # Named explicitly, the protected source records WHY it gets nothing.
    named = await recommend(sessionmaker, source_page_ids=[ids[LI]])
    named_cands = await _candidates(sessionmaker, named.id)
    assert named_cands
    assert {v.rejection for v in named_cands.values()} == {
        CandidateRejection.SOURCE_PROTECTED.value
    }
    assert named.counters["plans_created"] == 0


async def test_open_plans_are_expired_when_a_page_becomes_protected_or_gone(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """QA H2 (fail closed): no open plan may keep waiting for review once its source or
    target is protected, gone or otherwise ineligible."""
    await seed_corpus(sessionmaker, tmp_path)
    await recommend(sessionmaker)
    ids = await page_ids(sessionmaker)
    before = await _plans(sessionmaker)
    sources = {s for s, _ in before}
    targets = {t for _, t in before}
    protected_target = sorted(targets - sources)[0] if targets - sources else sorted(targets)[0]
    protected_source = sorted(sources - {protected_target})[0]
    gone_target = sorted(targets - {protected_target, protected_source})[0]
    involved = {protected_target, protected_source, gone_target}
    expected = {pair for pair in before if involved & set(pair)}
    assert expected
    async with sessionmaker() as session, session.begin():
        for path in (protected_target, protected_source):
            await insert_protection(
                session,
                page_id=ids[path],
                source=ProtectionSource.MANUAL.value,
                rule_id=None,
                reason="Legal review in progress.",
                created_by="user:editor",
                created_at=datetime.now(UTC),
            )
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("UPDATE pages SET gone_at = now() WHERE id = :i"), {"i": ids[gone_target]}
        )
    detail = await recommend(sessionmaker)
    assert detail.status is RunStatus.SUCCEEDED
    after = await _plans(sessionmaker)
    assert not any(involved & set(pair) for pair in after)
    assert detail.counters["plans_invalidated"] == len(expected)
    notes = await rows(
        sessionmaker,
        "SELECT t.note, t.actor FROM link_plan_transitions t WHERE t.to_status = 'expired'",
    )
    assert len(notes) == len(expected)
    assert all(n.actor == ENGINE_ACTOR and n.note.startswith("invalid: ") for n in notes)
    assert any("target_ineligible" in n.note for n in notes)
    assert any("source_ineligible" in n.note for n in notes)
    # Untouched pairs keep their plans (no collateral expiry).
    assert set(before) - expected <= set(after)


async def test_analysis_lag_never_expires_open_plans(sessionmaker: SM, tmp_path: Path) -> None:
    """QA re-review: after a re-ingest, a page's analysis is stale until ``make analyze``.
    That lag pauses generation but must NOT expire (terminally) any open plan."""
    from tests.integration.recommendation_support import CORPUS

    await seed_corpus(sessionmaker, tmp_path)
    await recommend(sessionmaker)
    before = await _plans(sessionmaker)
    edited = sorted({s for s, _ in before} | {t for _, t in before})[:2]
    records = [
        record(path, paragraphs=[*CORPUS[path][1], _p("A short closing note for readers.")])
        for path in edited
    ]
    await import_records(sessionmaker, tmp_path, records, name="v2.jsonl")
    detail = await recommend(sessionmaker)  # NO analysis run in between
    assert detail.status is RunStatus.SUCCEEDED, detail.counters
    assert detail.counters["plans_invalidated"] == 0
    assert detail.counters["plans_expired"] == 0  # appended block: every plan is rebasable
    assert set(await _plans(sessionmaker)) == set(before)


async def test_an_experiment_lock_rejects_the_pair(sessionmaker: SM, tmp_path: Path) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    ids = await page_ids(sessionmaker)

    class LockEvToLi:
        async def locked(self, *, source_page_id: uuid.UUID, target_page_id: uuid.UUID) -> bool:
            return (source_page_id, target_page_id) == (ids[EV], ids[LI])

    detail = await recommend(sessionmaker, experiment_guard=LockEvToLi())
    cands = await _candidates(sessionmaker, detail.id)
    assert cands[(EV, LI)].rejection == CandidateRejection.EXPERIMENT_LOCKED.value
    assert detail.counters["rejected_experiment_locked"] == 1


# --------------------------------------------------------------------------- #
# Selection caps
# --------------------------------------------------------------------------- #
async def test_per_source_limit(sessionmaker: SM, tmp_path: Path) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    detail = await recommend(sessionmaker, policy=_policy(max_plans_per_source=1))
    per_source = Counter(s for s, _ in await _plans(sessionmaker))
    assert per_source and max(per_source.values()) == 1
    assert detail.counters["rejected_per_source_limit"] >= 1
    cands = await _candidates(sessionmaker, detail.id)
    # The EV report kept its best-ranked placeable target and capped the next one.
    kept = {t for (s, t), v in cands.items() if s == EV and v.rejection is None}
    capped = {
        t
        for (s, t), v in cands.items()
        if s == EV and v.rejection == CandidateRejection.PER_SOURCE_LIMIT.value
    }
    assert kept == {CHARGING} and capped == {LI}


async def test_link_density_limit(sessionmaker: SM, tmp_path: Path) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    # EV has 157 words: one link is 0.64 per 100 words, two would be 1.27.
    detail = await recommend(sessionmaker, policy=_policy(max_internal_links_per_100_words=0.7))
    cands = await _candidates(sessionmaker, detail.id)
    ev = {t: v.rejection for (s, t), v in cands.items() if s == EV}
    assert ev[CHARGING] is None
    assert ev[LI] == CandidateRejection.LINK_DENSITY_EXCEEDED.value
    assert detail.counters["rejected_link_density_exceeded"] >= 1


async def test_destination_diversity_limit(sessionmaker: SM, tmp_path: Path) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    detail = await recommend(sessionmaker, policy=_policy(max_open_plans_per_target=1))
    per_target = Counter(t for _, t in await _plans(sessionmaker))
    assert per_target and max(per_target.values()) == 1
    assert detail.counters["rejected_diversity_limit"] >= 1


# --------------------------------------------------------------------------- #
# Failure isolation and atomicity
# --------------------------------------------------------------------------- #
async def test_one_failing_source_does_not_stop_the_run(sessionmaker: SM, tmp_path: Path) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    from ken_services.inventory.normalize import extract_blocks as real_extract

    def flaky(body_html: str) -> Any:
        if "photovoltaic" in body_html:  # only the solar report
            raise RuntimeError("scripted failure with secret postgresql://ken:ken@db")
        return real_extract(body_html)

    detail = await recommend(sessionmaker, toolkit=RecommendationToolkit(extract_blocks=flaky))
    assert detail.status is RunStatus.COMPLETED_WITH_ERRORS
    assert detail.counters["errors"] == 1
    assert detail.failure_reason is None
    plans = await _plans(sessionmaker)
    assert plans and all(s != SOLAR for s, _ in plans)
    assert (EV, LI) in plans
    cands = await _candidates(sessionmaker, detail.id)
    assert all(s != SOLAR for s, _ in cands)


async def test_a_source_commits_plans_transitions_events_and_candidates_atomically(
    sessionmaker: SM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    ids = await page_ids(sessionmaker)
    real = rec_repo.insert_candidates

    async def failing(session: Any, *, run_id: Any, candidates: Any, created_at: Any) -> int:
        if candidates and candidates[0].source_page_id == ids[EV]:
            raise RuntimeError("scripted storage failure")
        return await real(session, run_id=run_id, candidates=candidates, created_at=created_at)

    monkeypatch.setattr(rec_repo, "insert_candidates", failing)
    detail = await recommend(sessionmaker)
    assert detail.counters["errors"] == 1
    # The EV report's plans were inserted before the failure: all rolled back together.
    assert (
        await scalar(
            sessionmaker, "SELECT count(*) FROM link_plans WHERE source_page_id = :p", p=ids[EV]
        )
        == 0
    )
    assert (
        await scalar(
            sessionmaker,
            "SELECT count(*) FROM link_plan_transitions t JOIN link_plans p ON p.id = t.plan_id "
            "WHERE p.source_page_id = :p",
            p=ids[EV],
        )
        == 0
    )
    events = await rows(
        sessionmaker,
        "SELECT payload FROM events WHERE event_name = 'link.proposed'",
    )
    assert events and all(e.payload["source_page_id"] != str(ids[EV]) for e in events)
    plans = await scalar(sessionmaker, "SELECT count(*) FROM link_plans")
    assert plans == len(events) == detail.counters["plans_created"]


# --------------------------------------------------------------------------- #
# Opportunities and channels
# --------------------------------------------------------------------------- #
async def test_opportunities_are_replaced_per_run(sessionmaker: SM, tmp_path: Path) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    first = await recommend(sessionmaker)
    kinds = Counter(k for (k,) in await rows(sessionmaker, "SELECT kind FROM opportunities"))
    assert kinds["orphan"] == 5 and kinds["underlinked"] == 5  # nothing links anywhere yet
    assert sum(kinds.values()) == first.counters["opportunities"]
    second = await recommend(sessionmaker)
    found = await rows(sessionmaker, "SELECT DISTINCT run_id FROM opportunities")
    assert [r for (r,) in found] == [second.id]
    assert second.counters["opportunities"] == await scalar(
        sessionmaker, "SELECT count(*) FROM opportunities"
    )
    for score, evidence in await rows(sessionmaker, "SELECT score, evidence FROM opportunities"):
        assert 0.0 <= score <= 1.0 and evidence


async def test_lexical_channel_binds_keywords(sessionmaker: SM, tmp_path: Path) -> None:
    await seed_corpus(sessionmaker, tmp_path)
    ids = await page_ids(sessionmaker)
    async with sessionmaker() as session:
        hostile = await lexical_channel(
            session,
            source_page_id=ids[EV],
            keywords=["'); DROP TABLE pages; --", "solar' OR '1'='1", "", "   "],
            k=10,
        )
        found = await lexical_channel(
            session, source_page_id=ids[EV], keywords=["photovoltaic"], k=10
        )
        graph = await graph_channel(
            session,
            source_page_id=ids[EV],
            industry_id="automotive",
            analysis_version=AV,
            k=10,
        )
    assert ids[EV] not in hostile
    assert set(found) == {ids[SOLAR]}
    assert set(graph) == {ids[CHARGING]}  # same industry, never the source itself
    assert graph[ids[CHARGING]] == 1.0  # no inbound links: 1 / (1 + 0)
    assert await scalar(sessionmaker, "SELECT count(*) FROM pages") == 5


# --------------------------------------------------------------------------- #
# Related-report modules (ADR-0027 s1b)
# --------------------------------------------------------------------------- #
async def test_modules_are_built_from_regional_siblings(sessionmaker: SM, tmp_path: Path) -> None:
    """The owner's Priority 1, which inline anchors structurally cannot express.

    The India EV report gets a module naming the Germany and global EV reports -- link text
    the page does not contain, which is exactly why the module exists and the anchor cannot.
    """
    await seed_corpus(sessionmaker, tmp_path, *sibling_records())
    ids = await page_ids(sessionmaker)
    run = await recommend(sessionmaker)
    assert run.counters["errors"] == 0

    modules = await rows(
        sessionmaker,
        "SELECT m.page_id, m.market_core, m.geography_label, i.rank, i.target_url, "
        "i.geography_label, i.reason_codes FROM page_modules m "
        "JOIN page_module_items i ON i.module_id = m.id "
        "WHERE m.page_id = :p ORDER BY i.rank",
        p=ids[EV],
    )
    assert [r.target_url for r in modules] == [SITE + GLOBAL_EV, SITE + GERMANY_EV]
    assert {r.market_core for r in modules} == {"electric vehicles"}
    assert modules[0].geography_label is None  # a global target carries no geography
    assert modules[1].geography_label == "Germany"
    assert all("regional_sibling_market" in r.reason_codes for r in modules)
    # Every item is an orphan in this corpus, and says so rather than leaving it implied.
    assert all(ReasonCode.ORPHAN_TARGET.value in r.reason_codes for r in modules)
    assert run.counters["modules"] == await scalar(
        sessionmaker, "SELECT count(*) FROM page_modules"
    )
    assert run.counters["module_items"] == await scalar(
        sessionmaker, "SELECT count(*) FROM page_module_items"
    )


async def test_a_module_never_names_the_page_it_is_on(sessionmaker: SM, tmp_path: Path) -> None:
    await seed_corpus(sessionmaker, tmp_path, *sibling_records())
    await recommend(sessionmaker)
    self_links = await scalar(
        sessionmaker,
        "SELECT count(*) FROM page_modules m JOIN page_module_items i ON i.module_id = m.id "
        "WHERE i.target_page_id = m.page_id",
    )
    assert self_links == 0


async def test_modules_are_replaced_per_run_and_items_go_with_them(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """Replaced, not accumulated: ``UNIQUE (page_id, kind)`` would reject a second run."""
    await seed_corpus(sessionmaker, tmp_path, *sibling_records())
    first = await recommend(sessionmaker)
    second = await recommend(sessionmaker)
    found = await rows(sessionmaker, "SELECT DISTINCT run_id FROM page_modules")
    assert [r for (r,) in found] == [second.id]
    assert first.counters["modules"] == second.counters["modules"] > 0
    # The CASCADE left no item behind when the first run's modules went.
    orphaned = await scalar(
        sessionmaker,
        "SELECT count(*) FROM page_module_items i "
        "WHERE NOT EXISTS (SELECT 1 FROM page_modules m WHERE m.id = i.module_id)",
    )
    assert orphaned == 0


async def test_an_india_only_corpus_proposes_no_module(sessionmaker: SM, tmp_path: Path) -> None:
    """No sibling, no module. A module with nothing to compare is not a smaller module."""
    await seed_corpus(sessionmaker, tmp_path)
    run = await recommend(sessionmaker)
    assert run.counters["modules"] == 0
    assert await scalar(sessionmaker, "SELECT count(*) FROM page_modules") == 0
