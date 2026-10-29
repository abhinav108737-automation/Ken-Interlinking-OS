"""Phase 4 agent evals, end to end on the ``*_test`` database (ADR-0012, TRD sections 19/22).

For every group of ``tests/fixtures/agents/agent_cases.json`` the demo corpus is ingested,
analysed (MockModelProvider) and recommended for real. Each case then runs the REAL LangGraph
supervisor over the real ``DatabaseAgentBackend`` / ``DatabaseAgentRunStore`` inside a real
``agent`` job run, with the case's RECORDED model outputs replayed by ``MockModelProvider``:

* the engine's plans for the case's source must match the fixture (precondition; a mismatch
  is FIXTURE DRIFT, reported separately);
* the outcome of every run must equal the spec-derived expectation EXACTLY (outcome, status,
  error code, triage, risk, steps, fallbacks, per-plan verdicts, invariant problems,
  conflicts, which plans are handed off / recorded / unreviewed, review notes, model calls and
  the refs the model was shown);
* hard gates (``flow.gate_violations``) hold after EVERY batch: nothing reaches ``approved``
  or beyond, every handed-off plan has an accept/uncertain review row and the exact
  CRITIC_ACTOR / POLICY_GATE_ACTOR transition chain, every reject stays ``proposed``, every
  step carries its run's trace id, and no handed-off plan fails ``check_plan``.

A second test runs one batch over every source with both model slots disabled and proves the
outcome equals the deterministic rule. Offline eval (RUN_OFFLINE_EVAL, ALLOW): no network, no
learning, nothing proposed or promoted.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from ken_agents.prompts import SYSTEM_PROMPT
from ken_contracts.agents import (
    AgentNode,
    AgentTrigger,
    CriticAssessment,
    CriticVerdict,
    FallbackReason,
)
from ken_core.providers.model.mock import MockModelProvider
from tests.unit.agent_evals import flow
from tests.unit.agent_evals.cases import (
    Case,
    Dataset,
    Policy,
    Run,
    candidates_for_run,
    critic_called,
    dataset,
    expected_fallback_counter,
    expected_steps,
    planner_called,
    rule_reasons,
    rule_verdict,
    url,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _short(value: str) -> str:
    return value.removeprefix("https://research.example.com")


# --------------------------------------------------------------------------- #
# Precondition: the engine still proposes the recorded plans
# --------------------------------------------------------------------------- #
async def _drift(sm: flow.SM, case: Case, tolerance: float) -> list[str]:
    plans = await flow.source_plans(sm, url(case.source), status="proposed")
    got = [(_short(p.target_url), p.anchor, round(p.confidence, 4)) for p in plans]
    want = [(p.target, p.anchor, p.confidence) for p in case.engine_plans]
    if [g[:2] for g in got] != [w[:2] for w in want] or any(
        abs(g[2] - w[2]) > tolerance for g, w in zip(got, want, strict=True)
    ):
        return [f"FIXTURE DRIFT {case.id}: engine proposes {got}, fixture records {want}"]
    return []


# --------------------------------------------------------------------------- #
# One run against its expectation
# --------------------------------------------------------------------------- #
async def _check_run(
    sm: flow.SM,
    ds: Dataset,
    case: Case,
    index: int,
    run: Run,
    result: Any,
    provider: Any,
    plan_ids: dict[str, uuid.UUID],
) -> list[str]:
    e = run.expect
    tag = f"{case.id}[run {index}]"
    out: list[str] = []

    def eq(name: str, got: Any, want: Any) -> None:
        if got != want:
            out.append(f"{tag}: {name} = {got!r}, expected {want!r}")

    if result is None or result.state is None:
        return [f"{tag}: no run result"]
    state = result.state
    target_of = {v: _short(k) for k, v in plan_ids.items()}
    eq("outcome", result.outcome, e.outcome)
    eq("status", result.status, e.status)
    eq("error_code", result.error_code, e.error_code)
    eq("triage", state.triage_decision, e.triage)
    eq("risk_level", state.risk_level.value if state.risk_level else None, e.risk_level)
    eq("steps", [(s.node, s.status.value) for s in result.steps], list(expected_steps(e)))
    eq("fallback counter", result.fallbacks, expected_fallback_counter(e))
    steps = {s.node: s for s in result.steps}
    if AgentNode.PLAN in steps:
        eq("plan fallback", steps[AgentNode.PLAN].fallback_reason, e.planner_fallback)
    if AgentNode.CRITIQUE in steps:
        critique = steps[AgentNode.CRITIQUE]
        eq("critique fallback", critique.fallback_reason, e.critique_fallback)
        if e.unknown_refs is not None:
            eq("unknown_refs", critique.output.get("unknown_refs"), list(e.unknown_refs))
        if e.omitted_refs is not None:
            eq("omitted_refs", critique.output.get("omitted_refs"), list(e.omitted_refs))

    # Per-plan reviews (in critic-ref order) and their recorded reasons.
    got_reviews = [
        (target_of[r.plan_id], r.verdict, r.fallback_reason) for r in state.critic_result
    ]
    eq("critic", got_reviews, [(r.target, r.verdict, r.fallback) for r in e.critic])
    by_target = {target_of[r.plan_id]: r for r in state.critic_result}
    for want in e.critic:
        review = by_target.get(want.target)
        if review is None:
            continue
        reasons = want.reasons
        if reasons is None and want.fallback is not None:
            reasons = rule_reasons(want.verdict)
        if reasons is not None:
            eq(f"reasons {want.target}", review.reasons, reasons)
        expected_prefix = (
            "Deterministic rule: raw confidence"
            if want.fallback is FallbackReason.MODEL_DISABLED
            else "Deterministic fallback ("
        )
        if want.fallback is not None and not review.explanation.startswith(expected_prefix):
            out.append(f"{tag}: rule explanation for {want.target} is {review.explanation!r}")
        if want.explanation_contains and want.explanation_contains not in review.explanation:
            out.append(f"{tag}: explanation of {want.target} lacks {want.explanation_contains!r}")
    eq(
        "invariant_problems",
        {target_of[k]: v for k, v in state.invariant_problems.items()},
        dict(e.invariant_problems),
    )
    eq("conflicts", {target_of[k]: v for k, v in state.conflicts.items()}, dict(e.conflicts))
    eq("handed off (state)", sorted(target_of[i] for i in state.link_plans), sorted(e.handed_off))

    # Rows: this run's agent_run_plans, the plan statuses, the run row.
    reviews = await flow.run_reviews(sm, result.run_id)
    eq(
        "rows handed_off",
        sorted(_short(r["target_url"]) for r in reviews if r["handed_off"]),
        sorted(e.handed_off),
    )
    eq(
        "rows not handed off",
        sorted(_short(r["target_url"]) for r in reviews if not r["handed_off"]),
        sorted(e.recorded_not_handed_off),
    )
    for r in reviews:
        want_review = next((x for x in e.critic if x.target == _short(r["target_url"])), None)
        if want_review is None:
            out.append(f"{tag}: unexpected review row for {r['target_url']}")
            continue
        eq(f"row verdict {want_review.target}", r["verdict"], want_review.verdict.value)
        eq(
            f"row fallback {want_review.target}",
            r["fallback_reason"],
            want_review.fallback.value if want_review.fallback else None,
        )
    reviewed_ever = {
        _short(str(r["target_url"]))
        for r in await flow.rows(
            sm,
            "SELECT p.target_url FROM agent_run_plans r JOIN link_plans p ON p.id = r.plan_id "
            "WHERE p.source_url = :s",
            s=url(case.source),
        )
    }
    candidates = candidates_for_run(case, index)
    for target in e.unreviewed:
        if target in reviewed_ever:
            out.append(f"{tag}: {target} should have no review row")
    run_rows = await flow.rows(
        sm,
        "SELECT status, outcome, triage_decision, error_code, trigger, review_notes, "
        "source_url FROM agent_runs WHERE id = :r",
        r=result.run_id,
    )
    row = run_rows[0]
    eq("run row", (row["status"], row["outcome"]), (e.status.value, e.outcome.value))
    eq("run triage", row["triage_decision"], e.triage.value)
    eq("run error", row["error_code"], e.error_code.value if e.error_code else None)
    eq("run trigger", row["trigger"], AgentTrigger.MANUAL.value)
    eq("run notes", list(row["review_notes"]), list(state.review_notes))
    for note in e.notes_contain:
        if not any(note in n for n in state.review_notes):
            out.append(f"{tag}: no review note contains {note!r} (got {state.review_notes})")
    if not e.notes_contain and state.review_notes:
        out.append(f"{tag}: unexpected review notes {state.review_notes}")

    # Model calls and what the model was shown (refs, never ids; page text only in DATA).
    planner_calls = flow.structured_calls(provider, "PlannerSelection")
    critic_calls = flow.structured_calls(provider, "CriticAssessment")
    eq("planner calls", len(planner_calls), int(planner_called(case, run)))
    eq("critic calls", len(critic_calls), int(critic_called(case, run)))
    if planner_calls:
        data = flow.prompt_data(planner_calls[0])
        eq(
            "planner offered",
            [(c["ref"], _short(c["target_url"])) for c in data["candidates"]],
            [(f"c{i}", t) for i, t in enumerate(candidates[:10])],
        )
    if critic_calls:
        data = flow.prompt_data(critic_calls[0])
        eq(
            "critic offered",
            [(c["ref"], _short(c["target_url"])) for c in data["candidates"]],
            [(f"c{i}", r.target) for i, r in enumerate(e.critic)],
        )
        system = critic_calls[0].messages[0]
        eq("system prompt", (system.role, system.content), ("system", SYSTEM_PROMPT))
        if e.prompt_contains:
            if e.prompt_contains in system.content:
                out.append(f"{tag}: page text leaked into the system message")
            contexts = " ".join(c["context"] for c in data["candidates"])
            if e.prompt_contains not in contexts:
                out.append(f"{tag}: injected text not in the DATA contexts: {contexts!r}")
    for call in [*planner_calls, *critic_calls]:
        for message in call.messages:
            if UUID_RE.search(message.content):
                out.append(f"{tag}: a prompt contains an id")
    return out


async def _final_statuses(
    sm: flow.SM, case: Case, expired: set[str], handed: set[str]
) -> list[str]:
    out = []
    for plan in await flow.source_plans(sm, url(case.source)):
        target = _short(plan.target_url)
        if target not in case.targets:
            continue
        want = (
            "expired" if target in expired else "policy_checked" if target in handed else "proposed"
        )
        if plan.status != want:
            out.append(f"{case.id}: {target} is {plan.status}, expected {want}")
    return out


# --------------------------------------------------------------------------- #
# The recorded cases
# --------------------------------------------------------------------------- #
async def test_recorded_agent_cases(
    sessionmaker: flow.SM, test_database_url: str, tmp_path: Path
) -> None:
    ds = dataset()
    settings = flow.settings_for(test_database_url)
    base = flow.base_policy(settings)
    tools = flow.backend(sessionmaker, settings)
    drift: list[str] = []
    failures: list[str] = []
    gates: list[str] = []
    passed: Counter[str] = Counter()
    total: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()
    fallbacks: Counter[str] = Counter()
    runs_executed = 0

    for group in ds.groups:
        lines = flow.corpus_lines(group.corpus_edits)
        corpus = flow.write_lines(tmp_path, lines, f"{group.name}.jsonl")
        await flow.seed(sessionmaker, settings, corpus)
        cases = ds.in_group(group.name)
        for case in cases:  # every precondition BEFORE any mutation of the group
            drift += await _drift(sessionmaker, case, ds.confidence_tolerance)
        if drift:
            continue
        expired_by_hook: set[uuid.UUID] = set()
        for case in cases:
            total[case.suite] += 1
            problems: list[str] = []
            ids = await flow.page_ids(sessionmaker)
            for action in case.setup:
                if action.action == "protect":
                    await flow.protect(sessionmaker, ids[url(action.page or "")])
                elif action.action == "edit":
                    lines = await flow.reingest_edit(sessionmaker, lines, action, tmp_path)
                else:
                    await flow.analyse(sessionmaker, settings)
            plan_ids = {
                p.target_url: p.id for p in await flow.source_plans(sessionmaker, url(case.source))
            }
            handed: set[str] = set()
            expired: set[str] = set()
            for index, run in enumerate(case.runs):
                discover, hand_off = flow.hook_callbacks(sessionmaker, run.hooks, ids, plan_ids)
                for hook in run.hooks:
                    if hook.action == "expire" and hook.target:
                        expired.add(hook.target)
                        expired_by_hook.add(plan_ids[url(hook.target)])
                hooked = flow.HookedBackend(tools, on_discover=discover, on_hand_off=hand_off)
                provider = flow.recorded_provider(settings, run.model)
                try:
                    _detail, results = await flow.run_batch(
                        sessionmaker,
                        settings,
                        backend=hooked,
                        provider=provider,
                        policy=flow.agent_policy(base, case.policy),
                        sources=[ids[url(case.source)]],
                    )
                finally:
                    if provider is not None:
                        await provider.aclose()
                runs_executed += 1
                if len(hooked.fired) != len(run.hooks):
                    problems.append(f"{case.id}[run {index}]: hooks fired {hooked.fired}")
                result = results[0] if len(results) == 1 else None
                problems += await _check_run(
                    sessionmaker, ds, case, index, run, result, provider, plan_ids
                )
                gates += await flow.gate_violations(
                    sessionmaker, tools, results, expired_by_hook=frozenset(expired_by_hook)
                )
                handed |= set(run.expect.handed_off)
                for review in run.expect.critic:
                    verdicts[review.verdict.value] += 1
                    fallbacks[review.fallback.value if review.fallback else "model"] += 1
            problems += await _final_statuses(sessionmaker, case, expired, handed)
            if problems:
                failures += problems
            else:
                passed[case.suite] += 1

    summary = {
        "cases": sum(total.values()),
        "runs": runs_executed,
        "passed_by_suite": {s: f"{passed[s]}/{n}" for s, n in sorted(total.items())},
        "expected_verdicts": dict(verdicts),
        "expected_review_sources": dict(fallbacks),
        "gate_violations": len(gates),
        "fixture_drift": len(drift),
    }
    print(json.dumps(summary, indent=2))
    assert not drift, "\n".join(drift)
    assert not gates, "\n".join(gates)
    assert not failures, "\n".join(failures)
    assert sum(passed.values()) == len(ds.cases)


# --------------------------------------------------------------------------- #
# Model disabled: the outcome equals the deterministic rule, for every source
# --------------------------------------------------------------------------- #
async def test_model_disabled_equals_the_deterministic_rule(
    sessionmaker: flow.SM, test_database_url: str, tmp_path: Path
) -> None:
    ds = dataset()
    settings = flow.settings_for(test_database_url)
    group = next(g for g in ds.groups if g.name == ds.rule_baseline_group)
    corpus = flow.write_lines(tmp_path, flow.corpus_lines(group.corpus_edits), "rule.jsonl")
    await flow.seed(sessionmaker, settings, corpus)
    policy = flow.agent_policy(
        flow.base_policy(settings), Policy(planner=False, critic=False, max_plans_per_run=3)
    )
    tools = flow.backend(sessionmaker, settings)
    provider = MockModelProvider(settings)
    try:
        detail, results = await flow.run_batch(
            sessionmaker, settings, backend=tools, provider=provider, policy=policy, sources=None
        )
    finally:
        await provider.aclose()
    assert detail.counters["runs_failed"] == 0, detail.counters
    assert flow.structured_calls(provider, "CriticAssessment") == []
    assert flow.structured_calls(provider, "PlannerSelection") == []

    cases = {url(c.source): c for c in ds.in_group(group.name) if c.engine_plans}
    ids = await flow.page_ids(sessionmaker)
    by_id = {v: k for k, v in ids.items()}
    assert {by_id[r.source_page_id] for r in results} == set(cases)
    assert all(r.state.trigger is AgentTrigger.BATCH for r in results)
    mismatches: list[str] = []
    for result in results:
        case = cases[by_id[result.source_page_id]]
        chosen = case.engine_plans[: policy.max_plans_per_run]
        want = []
        seen_anchors: set[str] = set()  # the cannibalisation guard, restated independently
        for p in chosen:
            verdict = rule_verdict(p.confidence, ds.threshold)
            anchor = " ".join(p.anchor.casefold().split())
            if verdict is CriticVerdict.ACCEPT:
                if anchor in seen_anchors:
                    verdict = CriticVerdict.UNCERTAIN
                else:
                    seen_anchors.add(anchor)
            want.append((p.target, verdict, FallbackReason.MODEL_DISABLED))
        targets = {
            p.id: _short(p.target_url)
            for p in await flow.source_plans(sessionmaker, url(case.source))
        }
        got = [
            (targets[r.plan_id], r.verdict, r.fallback_reason) for r in result.state.critic_result
        ]
        accepted = any(v is CriticVerdict.ACCEPT for _t, v, _f in want)
        outcome, status = (
            ("recommend", "awaiting_approval") if accepted else ("no_action", "completed")
        )
        if got != want:
            mismatches.append(f"{case.source}: reviews {got} != rule {want}")
        if (result.outcome.value, result.status.value) != (outcome, status):
            mismatches.append(f"{case.source}: {result.outcome} != rule {outcome}")
        if result.fallbacks != 0:  # model_disabled is not a model problem
            mismatches.append(f"{case.source}: fallbacks {result.fallbacks}")
        if sorted(targets[i] for i in result.state.link_plans) != sorted(
            t for t, v, _f in want if v is CriticVerdict.ACCEPT
        ):
            mismatches.append(f"{case.source}: handed off {result.state.link_plans}")
    gates = await flow.gate_violations(sessionmaker, tools, results, expired_by_hook=frozenset())
    print(
        json.dumps(
            {
                "sources": len(results),
                "plans_reviewed": detail.counters["plans_reviewed"],
                "plans_handed_off": detail.counters["plans_handed_off"],
                "verdicts": dict(
                    Counter(r.verdict.value for x in results for r in x.state.critic_result)
                ),
            },
            indent=2,
        )
    )
    assert not gates, "\n".join(gates)
    assert not mismatches, "\n".join(mismatches)


# --------------------------------------------------------------------------- #
# Harness self-test: every hard gate fires on a bad record
# --------------------------------------------------------------------------- #
async def test_hard_gates_fire_on_bad_records(sessionmaker: flow.SM, tmp_path: Path) -> None:
    """Hand-made violations the agent runtime never produces: a human approval (beyond the
    agent's reach), an expiry outside a hook, and a handed-off plan whose target was protected
    afterwards. Each must be reported, so a passing eval is meaningful."""
    from ken_contracts.recommendations import LinkPlanStatus
    from ken_services.recommendations.transitions import transition
    from tests.integration.agent_support import SETTINGS, plans_corpus

    ids = await plans_corpus(sessionmaker, tmp_path)
    tools = flow.backend(sessionmaker, SETTINGS)
    provider = MockModelProvider(SETTINGS)
    provider.register(
        CriticAssessment,
        {"critiques": [{"ref": f"c{i}", "verdict": "accept"} for i in range(3)]},
    )
    _detail, results = await flow.run_batch(
        sessionmaker,
        SETTINGS,
        backend=tools,
        provider=provider,
        policy=flow.base_policy(SETTINGS),
        sources=None,
    )
    assert (
        await flow.gate_violations(sessionmaker, tools, results, expired_by_hook=frozenset()) == []
    )
    handed = [p for r in results for p in r.state.link_plans]
    assert len(handed) >= 2
    proposed = await flow.rows(sessionmaker, "SELECT id FROM link_plans WHERE status='proposed'")
    async with sessionmaker() as session, session.begin():
        await transition(  # the exceptional approval ladder: policy_checked -> awaiting_approval
            session,
            plan_id=handed[0],
            to_status=LinkPlanStatus.AWAITING_APPROVAL,
            actor="agent:policy-gate@graph-v1",
            note="self-test",
        )
        await transition(
            session,
            plan_id=handed[0],
            to_status=LinkPlanStatus.APPROVED,
            actor="user:gate-self-test",
            note="self-test",
        )
    if proposed:
        await flow.expire(sessionmaker, proposed[0]["id"])
    target = (
        await flow.rows(
            sessionmaker, "SELECT target_page_id FROM link_plans WHERE id = :p", p=handed[1]
        )
    )[0]["target_page_id"]
    await flow.protect(sessionmaker, target)
    found = await flow.gate_violations(sessionmaker, tools, results, expired_by_hook=frozenset())
    codes = {v.split(" ", 1)[0] for v in found}
    assert {"G1", "G2", "G5", "G6"} <= codes, found
    assert any("reached approved" in v for v in found)
    assert any("user:gate-self-test" in v for v in found)
    if proposed:
        assert any("expired outside a hook" in v for v in found)
    assert set(ids)  # the corpus was seeded
