"""The critic's outcomes, mapping and deterministic guards (ADR-0018 s1, ADR-0019).

Claims:

* the verdict -> outcome mapping is a pure, TOTAL function and ``accept`` is its only route to
  ACCEPT: an ``uncertain``, absent or unknown verdict is NO_ACTION, never permissive;
* the three outcomes are each reached, from a model and from the deterministic layer;
* a deterministic guard cannot be overridden by a model's accept, and only ever moves an
  outcome toward reject / no_action;
* a model that is unavailable, slow or invalid degrades to a RECORDED fallback that is never
  an accept, whatever the engine's confidence;
* the critic never touches the deterministic safety layer: it imports none of it.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Any

import pytest

from ken_agents.critic import (
    GENERIC_ANCHORS,
    Guard,
    critic_outcome,
    guard_for,
    is_handed_on,
)
from ken_contracts.agents import (
    AgentNode,
    AgentOutcome,
    CriticAssessment,
    CriticReason,
    CriticVerdict,
    FallbackReason,
)
from ken_contracts.autonomy import CriticOutcome
from ken_core.providers.model.mock import MockModelProvider
from ken_services.autonomy.engine import critic_outcome_of
from tests.unit.agents._support import (
    FakeBackend,
    FakeStore,
    make_plan,
    make_policy,
    run_source,
)

PACKAGE = Path(__file__).resolve().parents[3] / "agents" / "ken_agents"


def _provider(*pairs: tuple[str, str], reasons: list[str] | None = None) -> MockModelProvider:
    model = MockModelProvider(dimensions=8)
    model.register(
        CriticAssessment,
        {
            "critiques": [
                {"ref": ref, "verdict": verdict, "reasons": reasons or []} for ref, verdict in pairs
            ]
        },
    )
    return model


def _step(result: Any, node: AgentNode) -> Any:
    return next(s for s in result.steps if s.node is node)


# --------------------------------------------------------------------------- mapping
@pytest.mark.parametrize(
    ("verdict", "outcome"),
    [
        (CriticVerdict.ACCEPT, CriticOutcome.ACCEPT),
        (CriticVerdict.REJECT, CriticOutcome.REJECT),
        (CriticVerdict.UNCERTAIN, CriticOutcome.NO_ACTION),
    ],
)
def test_every_verdict_maps_to_exactly_one_outcome(
    verdict: CriticVerdict, outcome: CriticOutcome
) -> None:
    assert critic_outcome(verdict) is outcome
    assert set(CriticVerdict) == {
        CriticVerdict.ACCEPT,
        CriticVerdict.REJECT,
        CriticVerdict.UNCERTAIN,
    }, "a new verdict needs a deliberate mapping and a row in this test"


@pytest.mark.parametrize(
    "unknown",
    [None, "", "accept", "ACCEPT", "approved", "yes", 1, True, object(), ("accept",), b"accept"],
)
def test_anything_that_is_not_the_accept_member_is_never_accept(unknown: object) -> None:
    """Unparseable / absent / look-alike input is NO_ACTION: the only route to ACCEPT is the
    enum member itself, compared by identity (a string equal to its value does not count)."""
    assert critic_outcome(unknown) is CriticOutcome.NO_ACTION


def test_only_accept_is_handed_on() -> None:
    assert [o for o in CriticOutcome if is_handed_on(o)] == [CriticOutcome.ACCEPT]


@pytest.mark.parametrize("handed_off", [True, False], ids=["handed_on", "not_handed_on"])
@pytest.mark.parametrize("verdict", list(CriticVerdict))
def test_the_mapping_agrees_with_the_autonomy_engines_reading_of_a_stored_review(
    verdict: CriticVerdict, handed_off: bool
) -> None:
    """The engine derives the outcome from ``agent_run_plans`` (verdict + handed_off). Over
    EVERY verdict and BOTH ``handed_off`` values -- including the LEGACY row ``uncertain`` +
    ``handed_off=True`` that only data written before the critic change can carry:

    * the engine is never MORE permissive than the reference mapping;
    * ``reject`` and ``uncertain`` agree exactly, whatever ``handed_off`` says -- in particular
      an ``uncertain`` review is NO_ACTION even when a legacy row says it was handed on;
    * ``accept`` agrees when handed on, and is the stricter NO_ACTION when the run declined to
      hand it on.
    """
    reference = critic_outcome(verdict)
    engine = critic_outcome_of(verdict, handed_off=handed_off)
    permissive = {CriticOutcome.ACCEPT: 2, CriticOutcome.NO_ACTION: 1, CriticOutcome.REJECT: 0}
    assert permissive[engine] <= permissive[reference]
    if verdict is not CriticVerdict.ACCEPT:
        assert engine is reference
    elif handed_off:
        assert engine is reference is CriticOutcome.ACCEPT
    else:
        assert engine is CriticOutcome.NO_ACTION


def test_a_legacy_uncertain_review_marked_handed_on_is_never_an_accept() -> None:
    assert critic_outcome_of(CriticVerdict.UNCERTAIN, handed_off=True) is CriticOutcome.NO_ACTION
    assert critic_outcome(CriticVerdict.UNCERTAIN) is CriticOutcome.NO_ACTION


# --------------------------------------------------------------------------- three outcomes
async def test_accept_is_reached_from_a_model_answer_and_is_handed_on() -> None:
    plans = [make_plan(0)]
    backend = FakeBackend.ready(plans)
    result = await run_source(backend, FakeStore(), _provider(("c0", "accept")), make_policy())
    assert result.state is not None
    assert [r.outcome for r in result.state.critic_result] == [CriticOutcome.ACCEPT]
    assert backend.status[plans[0].id] == "policy_checked"
    assert result.outcome is AgentOutcome.RECOMMEND


async def test_reject_is_reached_from_a_model_answer_and_is_not_handed_on() -> None:
    plans = [make_plan(0)]
    backend = FakeBackend.ready(plans)
    result = await run_source(backend, FakeStore(), _provider(("c0", "reject")), make_policy())
    assert result.state is not None
    assert [r.outcome for r in result.state.critic_result] == [CriticOutcome.REJECT]
    assert backend.status[plans[0].id] == "proposed"
    assert result.plans_handed_off == 0


async def test_no_action_is_reached_from_an_uncertain_model_answer() -> None:
    plans = [make_plan(0)]
    backend = FakeBackend.ready(plans)
    result = await run_source(backend, FakeStore(), _provider(("c0", "uncertain")), make_policy())
    assert result.state is not None
    assert [r.outcome for r in result.state.critic_result] == [CriticOutcome.NO_ACTION]
    assert backend.status[plans[0].id] == "proposed"
    assert result.outcome is AgentOutcome.NO_ACTION


# --------------------------------------------------------------------------- guards
async def test_a_generic_anchor_is_rejected_even_when_the_model_accepts() -> None:
    plans = [make_plan(0, anchor_text="click here"), make_plan(1)]
    backend = FakeBackend.ready(plans)
    result = await run_source(
        backend, FakeStore(), _provider(("c0", "accept"), ("c1", "accept")), make_policy()
    )
    assert result.state is not None
    first, second = result.state.critic_result
    assert first.verdict is CriticVerdict.REJECT
    assert first.guard == Guard.GENERIC_ANCHOR.value
    assert CriticReason.MISLEADING_ANCHOR in first.reasons
    assert second.outcome is CriticOutcome.ACCEPT
    assert backend.status[plans[0].id] == "proposed"


async def test_a_duplicate_destination_is_no_action_and_the_better_candidate_survives() -> None:
    target = uuid.UUID(int=0xC0FFEE)
    plans = [make_plan(0, target_id=target), make_plan(1, target_id=target)]
    backend = FakeBackend.ready(plans)
    result = await run_source(
        backend, FakeStore(), _provider(("c0", "accept"), ("c1", "accept")), make_policy()
    )
    assert result.state is not None
    first, second = result.state.critic_result
    assert first.outcome is CriticOutcome.ACCEPT and first.guard is None
    assert second.outcome is CriticOutcome.NO_ACTION
    assert second.guard == Guard.DUPLICATE_DESTINATION.value
    assert backend.status[plans[1].id] == "proposed"
    assert result.plans_handed_off == 1


async def test_cannibalisation_same_anchor_for_another_page_is_no_action() -> None:
    plans = [
        make_plan(0, anchor_text="charging networks"),
        make_plan(1, anchor_text="Charging  Networks"),
    ]
    result = await run_source(
        FakeBackend.ready(plans),
        FakeStore(),
        _provider(("c0", "accept"), ("c1", "accept")),
        make_policy(),
    )
    assert result.state is not None
    assert [r.guard for r in result.state.critic_result] == [None, Guard.CANNIBALISATION.value]


def test_overlapping_placement_is_no_action() -> None:
    first = make_plan(0)
    second = make_plan(1).model_copy(update={"locator": first.locator})
    finding = guard_for(second, [first])
    assert finding is not None and finding.guard is Guard.OVERLAPPING_PLACEMENT
    assert finding.outcome is CriticOutcome.NO_ACTION
    disjoint = make_plan(2)
    assert guard_for(disjoint, [first]) is None


async def test_a_rejected_better_candidate_does_not_shadow_a_later_one() -> None:
    target = uuid.UUID(int=0xC0FFEE)
    plans = [make_plan(0, target_id=target), make_plan(1, target_id=target)]
    backend = FakeBackend.ready(plans)
    result = await run_source(
        backend, FakeStore(), _provider(("c0", "reject"), ("c1", "accept")), make_policy()
    )
    assert result.state is not None
    first, second = result.state.critic_result
    assert first.outcome is CriticOutcome.REJECT
    assert second.outcome is CriticOutcome.ACCEPT and second.guard is None


async def test_a_guard_never_upgrades_a_reject() -> None:
    plans = [make_plan(0), make_plan(1, anchor_text=next(iter(GENERIC_ANCHORS)))]
    result = await run_source(
        FakeBackend.ready(plans),
        FakeStore(),
        _provider(("c0", "accept"), ("c1", "reject")),
        make_policy(),
    )
    assert result.state is not None
    assert result.state.critic_result[1].verdict is CriticVerdict.REJECT


def test_every_guard_code_has_a_finding_somewhere() -> None:
    """The guard enum and the guard function cannot drift apart silently."""
    seen: set[Guard] = set()
    base = make_plan(0)
    cases = [
        (make_plan(1, anchor_text="here"), []),
        (make_plan(2, target_id=base.target_page_id), [base]),
        (make_plan(3, anchor_text=base.anchor_text), [base]),
        (make_plan(4).model_copy(update={"locator": base.locator}), [base]),
    ]
    for plan, earlier in cases:
        finding = guard_for(plan, earlier)
        assert finding is not None
        seen.add(finding.guard)
    assert seen == set(Guard)


# --------------------------------------------------------------------------- never permissive
@pytest.mark.parametrize(
    "reason",
    [
        FallbackReason.MODEL_UNAVAILABLE,
        FallbackReason.MODEL_TIMEOUT,
        FallbackReason.SCHEMA_VIOLATION,
    ],
)
async def test_a_degraded_critic_never_accepts_however_confident_the_engine_was(
    reason: FallbackReason,
) -> None:
    from ken_agents.nodes import deterministic_review

    plan = make_plan(0, confidence=1.0)
    review = deterministic_review(plan, "c0", 0.0, reason)
    assert review.verdict is CriticVerdict.UNCERTAIN
    assert review.outcome is CriticOutcome.NO_ACTION
    assert review.fallback_reason is reason and review.model is None


async def test_the_configured_rule_with_no_model_is_the_only_accepting_fallback() -> None:
    """``critic.use_model: false`` is a deliberate policy, not a failure: it keeps the
    documented threshold rule, recorded as ``model_disabled``. Every guard still applies."""
    plans = [make_plan(0, confidence=0.9), make_plan(1, confidence=0.2)]
    backend = FakeBackend.ready(plans)
    result = await run_source(backend, FakeStore(), None, make_policy(critic=False))
    sent = {r.plan_id: r for r in backend.handoffs[0][1]}
    assert sent[plans[0].id].verdict is CriticVerdict.ACCEPT
    assert sent[plans[1].id].verdict is CriticVerdict.UNCERTAIN
    assert all(r.fallback_reason is FallbackReason.MODEL_DISABLED for r in sent.values())
    assert result.outcome is AgentOutcome.RECOMMEND


async def test_the_critic_output_records_the_outcome_and_guard_per_plan() -> None:
    plans = [make_plan(0, anchor_text="read more"), make_plan(1)]
    result = await run_source(
        FakeBackend.ready(plans),
        FakeStore(),
        _provider(("c0", "accept"), ("c1", "uncertain")),
        make_policy(),
    )
    reviews = _step(result, AgentNode.CRITIQUE).output["reviews"]
    assert [(r["outcome"], r["guard"]) for r in reviews] == [
        ("reject", "generic_anchor"),
        ("no_action", None),
    ]


# --------------------------------------------------------------------------- independence
def test_the_critic_layer_imports_no_deterministic_safety_module() -> None:
    """The critic cannot override a block because it cannot reach one: nothing in the agent
    runtime imports the autonomy package (blocks, engine, facts) or the CMS."""
    forbidden = ("ken_services.autonomy", "ken_services.deployment", "ken_contracts.cms")
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "worker.py":  # the Phase 0 heartbeat is not an agent (ADR-0005)
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(forbidden), (path.name, name)
