"""The Phase 4 agent-eval dataset is well formed and agrees with ADR-0012 (no database).

``tests/fixtures/agents/agent_cases.json`` states each expectation twice: per plan (verdicts,
hand-offs, invariant problems, conflicts) and per run (outcome, status, error code, risk).
The oracle in ``cases.py`` re-derives the second from the first with the ADR rules; any
disagreement is an authoring error. The rest checks coverage floors, that recorded raw model
answers validate (or not) exactly as declared, that every page exists in the corpus, and that
cases sharing a seeded database cannot interfere with each other.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from ken_contracts.agents import MAX_CANDIDATES_FOR_MODEL, TriageDecision
from tests.unit.agent_evals.cases import (
    HOOK_ACTIONS,
    HOOK_POINTS,
    MODEL_KINDS,
    SETUP_ACTIONS,
    SUITES,
    Case,
    dataset,
    oracle_problems,
    unique,
    url,
    validate_text_answers,
)
from tests.unit.recommendation_evals.judgments import CORPUS_PATH, goldens, source_judgments

COVERAGE_FLOORS = {
    "destination-selection": 5,
    "critic": 8,
    "no-action": 6,
    "malformed-output": 15,
    "protected-content": 5,
    "stale-page-version": 5,
}
"""Minimum cases per suite (TRD section 22: candidate selection, critic, no-action; section
19: protected-content, stale-page-version; ADR-0012 section 6: malformed / hostile output).
A floor only ever goes up."""

DS = dataset()
CORPUS = {
    json.loads(line)["url"]: json.loads(line)
    for line in CORPUS_PATH.read_text("utf-8").splitlines()[1:]
}


def _ids(cases: tuple[Case, ...]) -> list[str]:
    return [c.id for c in cases]


def test_header_and_unique_ids() -> None:
    assert DS.version == 1
    assert DS.threshold == 0.5  # agents.v1.yaml critic.accept_min_confidence
    assert 0 < DS.confidence_tolerance <= 0.005
    assert unique(_ids(DS.cases))
    assert {c.suite for c in DS.cases} <= set(SUITES)
    assert {c.group for c in DS.cases} == {g.name for g in DS.groups}
    assert DS.rule_baseline_group in {g.name for g in DS.groups}


def test_coverage_floors() -> None:
    counts = Counter(c.suite for c in DS.cases)
    for suite, floor in COVERAGE_FLOORS.items():
        assert counts[suite] >= floor, (suite, counts[suite], floor)
    outcomes = Counter(r.expect.outcome.value for c in DS.cases for r in c.runs)
    assert {"recommend", "needs_human_review", "no_action"} <= set(outcomes)
    triage = {r.expect.triage for c in DS.cases for r in c.runs}
    assert triage >= {TriageDecision.OPTIMIZE, TriageDecision.NO_ACTION, TriageDecision.REINDEX}
    kinds = {r.model.kind for c in DS.cases for r in c.runs}
    assert kinds == set(MODEL_KINDS)


@pytest.mark.parametrize("case", DS.cases, ids=_ids(DS.cases))
def test_expectations_follow_the_adr_rules(case: Case) -> None:
    assert oracle_problems(case, DS) == []


@pytest.mark.parametrize("case", DS.cases, ids=_ids(DS.cases))
def test_recorded_answers_validate_as_declared(case: Case) -> None:
    assert validate_text_answers(case) == []
    for run in case.runs:
        assert run.model.kind in MODEL_KINDS
        if run.model.kind != "recorded":
            assert run.model.planner is None and run.model.critic is None
        if case.policy.critic and run.model.kind == "recorded" and run.expect.critic:
            assert run.model.critic is not None, case.id
        optimize = run.expect.triage is TriageDecision.OPTIMIZE
        if case.policy.planner and run.model.kind == "recorded" and optimize:
            assert run.model.planner is not None, case.id
        if run.model.kind == "timeout":
            assert case.policy.critic_timeout_seconds is not None
            assert case.policy.critic_timeout_seconds <= 1.0


@pytest.mark.parametrize("case", DS.cases, ids=_ids(DS.cases))
def test_pages_plans_and_actions_are_well_formed(case: Case) -> None:
    assert url(case.source) in CORPUS
    assert unique(case.targets)
    assert all(url(t) in CORPUS for t in case.targets)
    assert len(case.engine_plans) <= 3  # recommendation.v1 max_plans_per_source
    assert len(case.engine_plans) <= MAX_CANDIDATES_FOR_MODEL
    confidences = [p.confidence for p in case.engine_plans]
    assert confidences == sorted(confidences, reverse=True), "engine order: score desc"
    assert 1 <= case.policy.max_plans_per_run <= 3
    for action in case.setup:
        assert action.action in SETUP_ACTIONS
        if action.action != "analyse":
            assert action.page is not None and url(action.page) in CORPUS
        if action.action == "edit":
            assert action.page == case.source, "only the case's own source is edited"
            assert action.find and action.replace and action.find != action.replace
    for run in case.runs:
        for hook in run.hooks:
            assert hook.at in HOOK_POINTS and hook.action in HOOK_ACTIONS
            if hook.action == "expire":
                assert hook.target in case.targets
            else:
                assert hook.page is not None and url(hook.page) in CORPUS
        e = run.expect
        for review in e.critic:
            assert review.target in case.targets
        for target in (*e.handed_off, *e.recorded_not_handed_off, *e.unreviewed):
            assert target in case.targets


def test_engine_plans_of_the_base_corpus_are_judged() -> None:
    """Every recorded engine plan of the committed corpus is a Phase 3 judged pair; every
    source without plans is either ineligible (goldens) or an eligible source with none."""
    judged = source_judgments()
    eligibility = {u: p["eligibility"] for u, p in goldens()["pages"].items()}
    for case in DS.in_group(DS.rule_baseline_group):
        source = url(case.source)
        if not case.engine_plans:
            continue
        assert source in judged, case.id
        for plan in case.engine_plans:
            assert judged[source].label(url(plan.target)) != "unjudged", (case.id, plan.target)
    for case in DS.cases:
        if not case.engine_plans:
            reasons = eligibility[url(case.source)]["source_reasons"]
            for note in case.runs[0].expect.notes_contain:
                assert note == f"Source is not eligible to carry links ({', '.join(reasons)}).", (
                    case.id
                )


def test_base_group_covers_every_source_once() -> None:
    sources = [c.source for c in DS.in_group(DS.rule_baseline_group)]
    assert unique(sources)
    with_plans = [c for c in DS.in_group(DS.rule_baseline_group) if c.engine_plans]
    assert len(with_plans) >= 25, "the rule baseline needs (nearly) every source with plans"


@pytest.mark.parametrize("group", [g.name for g in DS.groups])
def test_cases_sharing_a_database_cannot_interfere(group: str) -> None:
    cases = DS.in_group(group)
    assert unique([c.source for c in cases]), "one case per source per group"
    for a in cases:
        for b in cases:
            if a is not b:
                assert not (a.mutated_pages & b.footprint), (a.id, b.id)
    analysed = False
    for case in cases:
        actions = [x.action for x in case.setup]
        if "edit" in actions and "analyse" not in actions:
            assert not analysed, f"{case.id}: an unanalysed edit must precede every analyse"
        analysed = analysed or "analyse" in actions


def test_edits_apply_exactly_once() -> None:
    from tests.unit.agent_evals.flow import _apply, corpus_lines

    for group in DS.groups:
        lines = corpus_lines(group.corpus_edits)  # raises unless each find occurs once
        for case in DS.in_group(group.name):
            for action in case.setup:
                if action.action == "edit":
                    lines = _apply(lines, action)


def test_injection_text_is_in_the_corpus_variant() -> None:
    for case in DS.cases:
        for run in case.runs:
            text = run.expect.prompt_contains
            if text is None:
                continue
            group = next(g for g in DS.groups if g.name == case.group)
            assert any(text in (e.replace or "") for e in group.corpus_edits), case.id
