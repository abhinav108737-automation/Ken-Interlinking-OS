"""``DatabaseAgentBackend`` against the ``*_test`` database, over REAL engine plans.

READ tools read live state; ``list_proposed_plans`` is engine-ordered and excludes reviewed
and non-proposed plans; ``check_plan`` reports every problem code; ``hand_off`` is atomic
per run with a savepoint per plan, binds its actors, and reports concurrent changes.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from ken_contracts.agents import (
    CRITIC_ACTOR,
    POLICY_GATE_ACTOR,
    AgentOutcome,
    AgentRunStatus,
    CriticReason,
    CriticVerdict,
    FallbackReason,
)
from ken_contracts.intelligence import IneligibilityReason, analysis_version
from ken_contracts.recommendations import LinkPlan, LinkPlanStatus
from ken_services.agents.backend import ALREADY_REVIEWED, MAX_LIST_PLANS, PROBLEM_CODES
from ken_services.agents.protocol import AgentBackend, PlanReviewInput
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.recommendations.transitions import ENGINE_ACTOR, transition
from tests.integration.agent_support import (
    CHARGING,
    EV,
    LI,
    SETTINGS,
    SM,
    T0,
    backend,
    engine_order,
    expire,
    extra_plan,
    plans_corpus,
    protect,
    start,
    store,
)
from tests.integration.recommendation_support import (
    EV_MARKET_PARAS,
    STORAGE,
    _p,
    analyse,
    import_records,
    record,
    rows,
    scalar,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

V = CriticVerdict
RS = AgentRunStatus
AO = AgentOutcome
FR = FallbackReason
S = LinkPlanStatus
AV = analysis_version(load_taxonomy(SETTINGS.taxonomy_path).version)


async def _ev_plans(sessionmaker: SM) -> dict[uuid.UUID, LinkPlan]:
    """The EV source's proposed plans by target id."""
    ids = await _ids(sessionmaker)
    plans = await backend(sessionmaker).list_proposed_plans(source_page_id=ids[EV], limit=10)
    return {p.target_page_id: p for p in plans}


async def _ids(sessionmaker: SM) -> dict[str, uuid.UUID]:
    from tests.integration.recommendation_support import page_ids

    return await page_ids(sessionmaker)


async def _status(sessionmaker: SM, plan_id: uuid.UUID) -> str:
    return str(
        await scalar(sessionmaker, "SELECT status FROM link_plans WHERE id = :id", id=plan_id)
    )


async def _history(sessionmaker: SM, plan_id: uuid.UUID) -> list[tuple[str | None, str, str, str]]:
    found = await rows(
        sessionmaker,
        "SELECT from_status, to_status, actor, note FROM link_plan_transitions "
        "WHERE plan_id = :id ORDER BY id",
        id=plan_id,
    )
    return [(r.from_status, r.to_status, r.actor, r.note) for r in found]


async def _review_rows(sessionmaker: SM) -> list[tuple[uuid.UUID, str, bool]]:
    found = await rows(
        sessionmaker, "SELECT plan_id, verdict, handed_off FROM agent_run_plans ORDER BY id"
    )
    return [(r.plan_id, r.verdict, r.handed_off) for r in found]


def test_the_backend_satisfies_the_protocol(sessionmaker: SM) -> None:
    assert isinstance(backend(sessionmaker), AgentBackend)


# --------------------------------------------------------------------------- #
# READ tools
# --------------------------------------------------------------------------- #
async def test_page_analysis_and_eligibility_read_the_current_version(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    tools = backend(sessionmaker)
    page = await tools.get_page(page_id=ids[EV])
    assert page is not None
    assert (page.id, page.current_version, page.gone) == (ids[EV], 1, False)
    assert page.canonical_url.endswith(EV)
    assert page.publication_state == "published"
    assert page.word_count > 0
    analysis = await tools.get_analysis(page_id=ids[EV])
    assert analysis is not None
    assert (analysis.page_id, analysis.page_version, analysis.analysis_version) == (
        ids[EV],
        1,
        AV,
    )
    eligibility = await tools.get_eligibility(page_id=ids[EV])
    assert eligibility is not None
    assert eligibility.as_source and eligibility.as_target

    unknown = uuid.uuid4()
    assert await tools.get_page(page_id=unknown) is None
    assert await tools.get_analysis(page_id=unknown) is None
    assert await tools.get_eligibility(page_id=unknown) is None


async def test_a_new_unanalysed_version_has_no_analysis_and_is_ineligible(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    paragraphs = [*EV_MARKET_PARAS, _p("A short closing note for readers of this edition.")]
    await import_records(sessionmaker, tmp_path, [record(EV, paragraphs=paragraphs)], name="v2")
    tools = backend(sessionmaker)
    page = await tools.get_page(page_id=ids[EV])
    assert page is not None and page.current_version == 2
    assert await tools.get_analysis(page_id=ids[EV]) is None
    eligibility = await tools.get_eligibility(page_id=ids[EV])
    assert eligibility is not None and not eligibility.as_source
    assert IneligibilityReason.STALE_ANALYSIS in eligibility.source_reasons


async def test_list_proposed_plans_is_engine_ordered_bounded_and_unreviewed(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    third = await extra_plan(sessionmaker, ids[EV], ids[STORAGE], "Fleet operators", total=0.7)
    tools = backend(sessionmaker)
    plans = await tools.list_proposed_plans(source_page_id=ids[EV], limit=10)
    assert len(plans) == 3
    assert plans == engine_order(plans)
    assert plans[0].id == third.id  # highest score first
    assert {p.source_page_id for p in plans} == {ids[EV]}
    assert all(p.status is S.PROPOSED for p in plans)
    assert await tools.list_proposed_plans(source_page_id=ids[EV], limit=1) == plans[:1]

    # A reviewed plan (row in agent_run_plans) and a non-proposed plan are excluded.
    run = await start(sessionmaker, ids[EV])
    await tools.hand_off(
        agent_run_id=run.id,
        reviews=[PlanReviewInput(plan_id=plans[0].id, verdict=V.REJECT)],
        reviewed_at=T0,
    )
    await expire(sessionmaker, plans[1].id)
    remaining = await tools.list_proposed_plans(source_page_id=ids[EV], limit=10)
    assert [p.id for p in remaining] == [plans[2].id]
    assert await tools.list_proposed_plans(source_page_id=uuid.uuid4(), limit=10) == []
    for bad in (0, MAX_LIST_PLANS + 1):
        with pytest.raises(ValueError, match="limit"):
            await tools.list_proposed_plans(source_page_id=ids[EV], limit=bad)


# --------------------------------------------------------------------------- #
# check_plan
# --------------------------------------------------------------------------- #
async def test_a_fresh_engine_plan_passes_every_check(sessionmaker: SM, tmp_path: Path) -> None:
    await plans_corpus(sessionmaker, tmp_path)
    tools = backend(sessionmaker)
    for plan in (await _ev_plans(sessionmaker)).values():
        check = await tools.check_plan(plan_id=plan.id)
        assert (check.ok, check.problems) == (True, ()), plan.anchor_text


async def test_not_proposed(sessionmaker: SM, tmp_path: Path) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    plan = (await _ev_plans(sessionmaker))[ids[LI]]
    await expire(sessionmaker, plan.id)
    check = await backend(sessionmaker).check_plan(plan_id=plan.id)
    assert (check.ok, check.problems) == (False, ("not_proposed",))
    missing = await backend(sessionmaker).check_plan(plan_id=uuid.uuid4())
    assert (missing.ok, missing.problems) == (False, ("not_proposed",))


async def test_source_version_changed_only_when_stale(sessionmaker: SM, tmp_path: Path) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    plans = await _ev_plans(sessionmaker)
    li_plan, charging_plan = plans[ids[LI]], plans[ids[CHARGING]]
    # v2: a new opening paragraph (block ids shift) and the lithium-ion paragraph rewritten.
    paragraphs = [
        _p("This edition adds the latest registration data for every state of the country."),
        EV_MARKET_PARAS[0],
        _p("Vehicle makers still import most of their Li-ion batteries, for now."),
        *EV_MARKET_PARAS[2:],
    ]
    await import_records(sessionmaker, tmp_path, [record(EV, paragraphs=paragraphs)], name="v2")
    tools = backend(sessionmaker)
    # Before re-analysis the source is ineligible (analysis lag also blocks a hand-off).
    lagging = await tools.check_plan(plan_id=charging_plan.id)
    assert lagging.problems == ("source_ineligible",)
    await analyse(sessionmaker)
    stale = await tools.check_plan(plan_id=li_plan.id)
    assert (stale.ok, stale.problems) == (False, ("source_version_changed",))
    rebasable = await tools.check_plan(plan_id=charging_plan.id)
    assert (rebasable.ok, rebasable.problems) == (True, ())


async def test_anchor_not_verbatim(sessionmaker: SM, tmp_path: Path) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    good = await extra_plan(sessionmaker, ids[EV], ids[STORAGE], "Fleet operators")
    assert (await backend(sessionmaker).check_plan(plan_id=good.id)).ok
    await expire(sessionmaker, good.id)  # frees the pair and the span
    shifted = good.locator.model_copy(
        update={"start": good.locator.start + 1, "end": good.locator.end + 1}
    )
    bad = await extra_plan(
        sessionmaker, ids[EV], ids[STORAGE], "Fleet operators", id=uuid.uuid4(), locator=shifted
    )
    check = await backend(sessionmaker).check_plan(plan_id=bad.id)
    assert (check.ok, check.problems) == (False, ("anchor_not_verbatim",))


async def test_source_and_target_ineligible(sessionmaker: SM, tmp_path: Path) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    plans = await _ev_plans(sessionmaker)
    tools = backend(sessionmaker)
    await protect(sessionmaker, ids[LI])
    assert (await tools.check_plan(plan_id=plans[ids[LI]].id)).problems == ("target_ineligible",)
    assert (await tools.check_plan(plan_id=plans[ids[CHARGING]].id)).ok
    await protect(sessionmaker, ids[EV])
    check = await tools.check_plan(plan_id=plans[ids[LI]].id)
    assert check.problems == ("source_ineligible", "target_ineligible")


async def test_already_linked(sessionmaker: SM, tmp_path: Path) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    plan = (await _ev_plans(sessionmaker))[ids[LI]]
    # A body link of the source's CURRENT version that resolves to the target.
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO existing_links (page_id, page_version, ordinal, href, resolved_url, "
                "link_type, anchor_text, anchor_source, block_id, block_kind, "
                "occurrence_index, context_text, context_hash) SELECT :s, 1, 999, "
                "p.canonical_url, p.canonical_url, 'internal', 'x', 'text', 'b99', 'p', 0, "
                "'x', :h FROM pages p WHERE p.id = :t"
            ),
            {"s": ids[EV], "t": ids[LI], "h": "0" * 64},
        )
    check = await backend(sessionmaker).check_plan(plan_id=plan.id)
    assert (check.ok, check.problems) == (False, ("already_linked",))


def test_problem_codes_are_the_protocol_codes() -> None:
    assert PROBLEM_CODES == (
        "not_proposed",
        "source_version_changed",
        "anchor_not_verbatim",
        "source_ineligible",
        "target_ineligible",
        "already_linked",
    )


# --------------------------------------------------------------------------- #
# hand_off
# --------------------------------------------------------------------------- #
async def test_accept_and_uncertain_reach_the_queue_and_reject_stays_proposed(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    third = await extra_plan(sessionmaker, ids[EV], ids[STORAGE], "Fleet operators")
    plans = await _ev_plans(sessionmaker)
    accept, uncertain = plans[ids[CHARGING]], plans[ids[LI]]
    run = await start(sessionmaker, ids[EV])
    result = await backend(sessionmaker).hand_off(
        agent_run_id=run.id,
        reviews=[
            PlanReviewInput(
                plan_id=accept.id,
                verdict=V.ACCEPT,
                reasons=(CriticReason.NATURAL_ANCHOR, CriticReason.RELEVANT_DESTINATION),
                explanation="Reads naturally.",
                model="reasoning-model",
            ),
            PlanReviewInput(
                plan_id=uncertain.id,
                verdict=V.UNCERTAIN,
                reasons=(CriticReason.LOW_CONFIDENCE,),
                fallback_reason=FR.MODEL_DISABLED,
            ),
            PlanReviewInput(
                plan_id=third.id, verdict=V.REJECT, reasons=(CriticReason.WEAK_RELEVANCE,)
            ),
        ],
        reviewed_at=T0,
    )
    assert result.handed_off == (accept.id, uncertain.id)
    assert result.recorded == (accept.id, uncertain.id, third.id)
    assert dict(result.conflicts) == {}

    assert await _status(sessionmaker, accept.id) == "awaiting_approval"
    assert await _status(sessionmaker, uncertain.id) == "awaiting_approval"
    assert await _status(sessionmaker, third.id) == "proposed"
    for plan, verdict in ((accept, "accept"), (uncertain, "uncertain")):
        history = await _history(sessionmaker, plan.id)
        assert history[0][:3] == (None, "proposed", ENGINE_ACTOR)
        assert history[1:] == [
            ("proposed", "critiqued", CRITIC_ACTOR, f"agent review: {verdict}"),
            ("critiqued", "policy_checked", POLICY_GATE_ACTOR, f"agent review: {verdict}"),
            ("policy_checked", "awaiting_approval", POLICY_GATE_ACTOR, f"agent review: {verdict}"),
        ]
    assert len(await _history(sessionmaker, third.id)) == 1  # never transitioned

    found = await rows(
        sessionmaker,
        "SELECT run_id, plan_id, verdict, reasons, explanation, handed_off, fallback_used, "
        "fallback_reason, model, reviewed_at FROM agent_run_plans ORDER BY id",
    )
    assert [(r.plan_id, r.verdict, r.handed_off) for r in found] == [
        (accept.id, "accept", True),
        (uncertain.id, "uncertain", True),
        (third.id, "reject", False),
    ]
    assert {r.run_id for r in found} == {run.id}
    assert found[0].reasons == ["natural_anchor", "relevant_destination"]
    assert (found[0].explanation, found[0].model, found[0].fallback_used) == (
        "Reads naturally.",
        "reasoning-model",
        False,
    )
    assert (found[1].fallback_used, found[1].fallback_reason, found[1].model) == (
        True,
        "model_disabled",
        None,
    )
    assert all(r.reviewed_at == T0 for r in found)
    # No actor ever approves: there is no edge to approved.
    assert not await rows(
        sessionmaker, "SELECT 1 FROM link_plan_transitions WHERE to_status = 'approved'"
    )


async def test_a_plan_expired_before_the_hand_off_is_a_conflict_and_the_rest_commit(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    plans = await _ev_plans(sessionmaker)
    gone, kept = plans[ids[LI]], plans[ids[CHARGING]]
    run = await start(sessionmaker, ids[EV])
    await expire(sessionmaker, gone.id)  # a concurrent `make recommend`
    result = await backend(sessionmaker).hand_off(
        agent_run_id=run.id,
        reviews=[
            PlanReviewInput(plan_id=gone.id, verdict=V.ACCEPT),
            PlanReviewInput(plan_id=kept.id, verdict=V.ACCEPT),
        ],
        reviewed_at=T0,
    )
    assert dict(result.conflicts) == {gone.id: "not_proposed"}
    assert result.handed_off == (kept.id,)
    assert result.recorded == (kept.id,)
    assert await _status(sessionmaker, gone.id) == "expired"
    assert await _status(sessionmaker, kept.id) == "awaiting_approval"
    assert await _review_rows(sessionmaker) == [(kept.id, "accept", True)]


async def test_a_reject_of_an_expired_plan_is_a_conflict_too(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    plan = (await _ev_plans(sessionmaker))[ids[LI]]
    run = await start(sessionmaker, ids[EV])
    await expire(sessionmaker, plan.id)
    result = await backend(sessionmaker).hand_off(
        agent_run_id=run.id,
        reviews=[PlanReviewInput(plan_id=plan.id, verdict=V.REJECT)],
        reviewed_at=T0,
    )
    assert (result.handed_off, result.recorded, dict(result.conflicts)) == (
        (),
        (),
        {plan.id: "not_proposed"},
    )
    assert await _review_rows(sessionmaker) == []


async def test_a_concurrent_expiry_waiting_on_the_plan_lock_is_a_conflict(
    sessionmaker: SM, tmp_path: Path
) -> None:
    """The engine holds the plan row lock while it expires it; the gate waits, then sees it."""
    ids = await plans_corpus(sessionmaker, tmp_path)
    plans = await _ev_plans(sessionmaker)
    gone, kept = plans[ids[LI]], plans[ids[CHARGING]]
    run = await start(sessionmaker, ids[EV])
    async with sessionmaker() as engine_session:
        await engine_session.begin()
        await transition(
            engine_session,
            plan_id=gone.id,
            to_status=S.EXPIRED,
            actor=ENGINE_ACTOR,
            note="stale: version_changed",
        )
        gate = asyncio.create_task(
            backend(sessionmaker).hand_off(
                agent_run_id=run.id,
                reviews=[
                    PlanReviewInput(plan_id=kept.id, verdict=V.ACCEPT),
                    PlanReviewInput(plan_id=gone.id, verdict=V.ACCEPT),
                ],
                reviewed_at=T0,
            )
        )
        await asyncio.sleep(0.5)
        assert not gate.done()  # blocked on the FOR UPDATE lock
        await engine_session.commit()
    result = await asyncio.wait_for(gate, timeout=10)
    assert dict(result.conflicts) == {gone.id: "not_proposed"}
    assert result.handed_off == (kept.id,)
    assert await _status(sessionmaker, gone.id) == "expired"
    assert [h[1] for h in await _history(sessionmaker, gone.id)] == ["proposed", "expired"]


async def test_a_plan_lock_held_too_long_aborts_the_hand_off_cleanly(
    sessionmaker: SM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA N1: the write path has no asyncio deadline, so the DATABASE bounds it. A plan row
    locked elsewhere past ``lock_timeout`` aborts the whole hand-off before commit: an
    error, and nothing written (no review row, no transition)."""
    from sqlalchemy.exc import DBAPIError

    import ken_services.agents.backend as backend_module

    monkeypatch.setattr(backend_module, "HAND_OFF_LOCK_TIMEOUT_MS", 300)
    ids = await plans_corpus(sessionmaker, tmp_path)
    plans = await _ev_plans(sessionmaker)
    held, other = plans[ids[LI]], plans[ids[CHARGING]]
    run = await start(sessionmaker, ids[EV])
    async with sessionmaker() as holder:
        await holder.begin()
        await holder.execute(
            text("SELECT id FROM link_plans WHERE id = :i FOR UPDATE"), {"i": held.id}
        )
        with pytest.raises(DBAPIError):
            await asyncio.wait_for(
                backend(sessionmaker).hand_off(
                    agent_run_id=run.id,
                    reviews=[
                        PlanReviewInput(plan_id=other.id, verdict=V.ACCEPT),
                        PlanReviewInput(plan_id=held.id, verdict=V.ACCEPT),
                    ],
                    reviewed_at=T0,
                ),
                timeout=10,  # the test's own guard: the DB must abort well before this
            )
        await holder.rollback()
    assert await scalar(sessionmaker, "SELECT count(*) FROM agent_run_plans") == 0
    assert await _status(sessionmaker, other.id) == "proposed"
    assert await _status(sessionmaker, held.id) == "proposed"


async def test_invariants_are_rechecked_inside_the_hand_off(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    plans = await _ev_plans(sessionmaker)
    blocked, kept = plans[ids[LI]], plans[ids[CHARGING]]
    tools = backend(sessionmaker)
    assert (await tools.check_plan(plan_id=blocked.id)).ok
    run = await start(sessionmaker, ids[EV])
    await protect(sessionmaker, ids[LI])  # after the check, before the gate
    result = await tools.hand_off(
        agent_run_id=run.id,
        reviews=[
            PlanReviewInput(plan_id=blocked.id, verdict=V.UNCERTAIN),
            PlanReviewInput(plan_id=kept.id, verdict=V.ACCEPT),
        ],
        reviewed_at=T0,
    )
    assert dict(result.conflicts) == {blocked.id: "target_ineligible"}
    assert result.handed_off == (kept.id,)
    assert await _status(sessionmaker, blocked.id) == "proposed"
    assert len(await _history(sessionmaker, blocked.id)) == 1
    assert await _review_rows(sessionmaker) == [(kept.id, "accept", True)]


async def test_a_plan_another_run_reviewed_is_a_conflict_and_rolls_back(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    plan = (await _ev_plans(sessionmaker))[ids[LI]]
    first = await start(sessionmaker, ids[EV])
    tools = backend(sessionmaker)
    await tools.hand_off(
        agent_run_id=first.id,
        reviews=[PlanReviewInput(plan_id=plan.id, verdict=V.REJECT)],
        reviewed_at=T0,
    )
    await store(sessionmaker).finish_run(
        run_id=first.id, status=RS.COMPLETED, outcome=AO.NO_ACTION, finished_at=T0
    )
    second = await start(sessionmaker, ids[EV])
    result = await tools.hand_off(
        agent_run_id=second.id,
        reviews=[PlanReviewInput(plan_id=plan.id, verdict=V.ACCEPT)],
        reviewed_at=T0,
    )
    assert dict(result.conflicts) == {plan.id: ALREADY_REVIEWED}
    assert result.recorded == ()
    # The savepoint rolled the three transitions back with the duplicate row.
    assert await _status(sessionmaker, plan.id) == "proposed"
    assert len(await _history(sessionmaker, plan.id)) == 1
    assert await _review_rows(sessionmaker) == [(plan.id, "reject", False)]


async def test_hand_off_refuses_bad_input_and_writes_nothing(
    sessionmaker: SM, tmp_path: Path
) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    plans = await _ev_plans(sessionmaker)
    plan = plans[ids[LI]]
    foreign = (await backend(sessionmaker).list_proposed_plans(source_page_id=ids[LI], limit=1))[0]
    tools = backend(sessionmaker)
    run = await start(sessionmaker, ids[EV])
    ok = PlanReviewInput(plan_id=plan.id, verdict=V.ACCEPT)
    cases: list[tuple[uuid.UUID, list[PlanReviewInput], str]] = [
        (uuid.uuid4(), [ok], "unknown agent run"),
        (run.id, [ok, PlanReviewInput(plan_id=foreign.id, verdict=V.ACCEPT)], "source page"),
        (run.id, [PlanReviewInput(plan_id=uuid.uuid4(), verdict=V.ACCEPT)], "source page"),
        (run.id, [ok, ok], "at most once"),
        (run.id, [PlanReviewInput(plan_id=plan.id, verdict=V.ACCEPT, explanation="x" * 301)],
         "explanation"),
        (run.id, [PlanReviewInput(plan_id=plan.id, verdict=V.ACCEPT,
                                  reasons=(CriticReason.NATURAL_ANCHOR,) * 2)], "reasons"),
    ]  # fmt: skip
    for run_id, reviews, message in cases:
        with pytest.raises(ValueError, match=message):
            await tools.hand_off(agent_run_id=run_id, reviews=reviews, reviewed_at=T0)
    with pytest.raises(ValueError):
        await tools.hand_off(
            agent_run_id=run.id,
            reviews=[PlanReviewInput(plan_id=plan.id, verdict="approve")],  # type: ignore[arg-type]
            reviewed_at=T0,
        )
    await store(sessionmaker).finish_run(
        run_id=run.id, status=RS.COMPLETED, outcome=AO.NO_ACTION, finished_at=T0
    )
    with pytest.raises(ValueError, match="finished"):
        await tools.hand_off(agent_run_id=run.id, reviews=[ok], reviewed_at=T0)
    assert await _review_rows(sessionmaker) == []
    assert [await _status(sessionmaker, p.id) for p in plans.values()] == ["proposed"] * 2


async def test_an_empty_hand_off_is_a_no_op(sessionmaker: SM, tmp_path: Path) -> None:
    ids = await plans_corpus(sessionmaker, tmp_path)
    run = await start(sessionmaker, ids[EV])
    result = await backend(sessionmaker).hand_off(agent_run_id=run.id, reviews=[], reviewed_at=T0)
    assert (result.handed_off, result.recorded, dict(result.conflicts)) == ((), (), {})
