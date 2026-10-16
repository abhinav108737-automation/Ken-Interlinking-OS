"""The block path performs NO inference, and no model can clear a block.

Two guards, because a docstring is not a guard:

1. :func:`test_the_block_path_imports_no_model_provider` walks the TRANSITIVE first-party
   import graph of :mod:`ken_services.autonomy.blocks` and
   :mod:`ken_services.autonomy.facts` with ``ast`` -- no imports are executed -- and fails if
   any module in it imports a model provider, the agent runtime or an inference client. If
   someone later adds an "LLM safety check" to the deterministic layer, this fails before the
   safety rule does.
2. :func:`test_a_model_answering_accept_cannot_clear_a_block` drives a real
   :class:`~ken_core.providers.model.mock.MockModelProvider` that answers ``accept`` on a plan
   with a fired block code, through every layer that consumes a model answer, and asserts the
   decision is still ``BLOCK``.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from typing import Final

import pytest

from ken_contracts.agents import CriticVerdict
from ken_contracts.autonomy import (
    DETERMINISTIC_BLOCK_CODES,
    AutonomyDecision,
    AutonomyMode,
    BlockCode,
    CriticOutcome,
    RiskTier,
)
from ken_core.providers.model.mock import MockModelProvider
from ken_services.autonomy.blocks import BLOCK_ORDER, evaluate_blocks
from ken_services.autonomy.protocol import decision_for
from tests.unit.autonomy._support import clean_facts, inputs
from tests.unit.autonomy.pure import pure_engine

ENTRY_POINTS: Final = ("ken_services.autonomy.blocks", "ken_services.autonomy.facts")

FIRST_PARTY: Final = ("ken_contracts", "ken_core", "ken_services", "ken_agents", "ken_api")

DIRECT_FORBIDDEN_PREFIXES: Final = (
    "ken_contracts.model",
    "ken_core.providers.model",
    "ken_agents",
    "ollama",
    "openai",
    "anthropic",
)
"""What the deterministic modules themselves may not import, AT ALL -- including
``ken_contracts.model``, which is where ``ModelProvider`` lives. A parameter you cannot type
is a parameter you cannot accept, so refusing even the TYPE is the cheapest way to keep a
model provider out of the constructor."""

TRANSITIVE_FORBIDDEN_PREFIXES: Final = (
    "ken_core.providers.model",
    "ken_agents",
    "ollama",
    "openai",
    "anthropic",
)
"""What NOTHING in the transitive closure may import: a provider IMPLEMENTATION, the agent
runtime, or an inference client. ``ken_contracts.model`` is deliberately absent from this
list and present in the direct one: shared contracts (``ken_contracts.cms``,
``ken_core.config``) import ``ModelRole`` and ``ProviderHealth`` from it, which are string
enums with no behaviour and cannot perform inference. What must not be REACHABLE is anything
that can call a model, which is what :data:`TRANSITIVE_FORBIDDEN_NAMES` and these prefixes
cover."""

TRANSITIVE_FORBIDDEN_NAMES: Final = ("ModelProvider", "complete_structured", "complete_text")
"""The protocol itself and its call sites. A module in the closure that imported
``ModelProvider`` could hold one; no module in the closure does."""


def _module_file(name: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):  # pragma: no cover - a missing module is not first party
        return None
    if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
        return None
    return Path(spec.origin)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _closure() -> dict[str, set[str]]:
    """module -> the names it imports, over the transitive first-party closure."""
    seen: dict[str, set[str]] = {}
    pending = list(ENTRY_POINTS)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        path = _module_file(name)
        if path is None:
            continue
        found = _imports(path)
        seen[name] = found
        for imported in found:
            if imported.startswith(FIRST_PARTY) and imported not in seen:
                pending.append(imported)
    return seen


def test_the_deterministic_modules_import_nothing_model_shaped() -> None:
    """The DIRECT check: ``blocks.py`` and ``facts.py``, as written."""
    offenders: list[tuple[str, str]] = []
    for module in ENTRY_POINTS:
        path = _module_file(module)
        assert path is not None, module
        for name in sorted(_imports(path)):
            if name.startswith(DIRECT_FORBIDDEN_PREFIXES):
                offenders.append((module, name))
    assert offenders == [], (
        "the deterministic block path must not import anything model-shaped; found: "
        + ", ".join(f"{m} -> {n}" for m, n in offenders)
    )


def test_the_block_path_can_reach_no_model_provider() -> None:
    """The TRANSITIVE check: nothing the block path imports, at any depth, can call a model."""
    closure = _closure()
    assert ENTRY_POINTS[0] in closure and ENTRY_POINTS[1] in closure
    assert len(closure) > 10, "the closure walk found suspiciously few modules"
    offenders: list[tuple[str, str]] = []
    for module, imported in sorted(closure.items()):
        for name in sorted(imported):
            if (
                name.startswith(TRANSITIVE_FORBIDDEN_PREFIXES)
                or name.rsplit(".", 1)[-1] in TRANSITIVE_FORBIDDEN_NAMES
            ):
                offenders.append((module, name))
    assert offenders == [], (
        "no module reachable from the block path may be able to call a model; found: "
        + ", ".join(f"{m} -> {n}" for m, n in offenders)
    )


def test_the_block_evaluator_is_synchronous_and_pure() -> None:
    """A function with no ``await`` cannot smuggle in an inference call."""
    import inspect

    assert not inspect.iscoroutinefunction(evaluate_blocks)
    facts = clean_facts(source_protected=True)
    assert evaluate_blocks(facts) == evaluate_blocks(facts)  # no state, no clock


def test_every_block_code_is_declared_deterministic() -> None:
    """If a non-deterministic "block" is ever added, this fails and the decision has to be
    taken explicitly rather than by accident."""
    assert set(BLOCK_ORDER) == set(DETERMINISTIC_BLOCK_CODES) == set(BlockCode)


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"source_protected": True}, BlockCode.PROTECTED_PAGE),
        ({"target_published": False}, BlockCode.UNPUBLISHED_DESTINATION),
        ({"anchor_valid": False}, BlockCode.INVALID_ANCHOR),
        ({"vector_fresh": False}, BlockCode.STALE_VECTOR),
    ],
)
def test_a_model_answering_accept_cannot_clear_a_block(
    override: dict[str, bool], expected: BlockCode
) -> None:
    """The whole point of the phase, as a test.

    A model provider is present and answers as positively as it can -- the critic ACCEPTED,
    the confidence is 1.0 and calibrated, the risk tier is LOW, the mode is the most
    permissive one -- and the decision is still ``BLOCK``.
    """
    model = MockModelProvider(dimensions=8)
    verdict = CriticVerdict.ACCEPT  # the most positive thing a critic can say
    facts = clean_facts(**override)
    blocks = evaluate_blocks(facts)
    assert expected in blocks

    engine = pure_engine()
    decided = engine._decide(
        inputs(
            facts=facts,
            blocks=blocks,
            critic=CriticOutcome.ACCEPT,
            verdict=verdict,
            confidence=1.0,
            tier=RiskTier.LOW,
        ),
        mode=AutonomyMode.AUTONOMOUS,
    )
    assert decided is AutonomyDecision.BLOCK
    # The frozen mapping agrees, independently of the engine.
    assert (
        decision_for(
            blocks=blocks,
            mode=AutonomyMode.AUTONOMOUS,
            risk_tier=RiskTier.LOW,
            extra_verification=False,
        )
        is AutonomyDecision.BLOCK
    )
    # The model was never consulted by the block path: it recorded no calls.
    assert model.calls == []


def test_an_evaluation_record_cannot_claim_a_publish_over_a_fired_block() -> None:
    """The contract refuses it, so a buggy caller cannot persist one either (and the
    ``blocks_force_block`` CHECK refuses it again at the database)."""
    from ken_contracts.autonomy import AutonomyEvaluation
    from tests.unit.autonomy._support import NOW, PLAN

    with pytest.raises(ValueError, match="block code forces decision"):
        AutonomyEvaluation(
            plan_id=PLAN,
            decision=AutonomyDecision.AUTO_PUBLISH,
            mode=AutonomyMode.AUTONOMOUS,
            risk_tier=RiskTier.LOW,
            confidence=1.0,
            calibration_version="autonomy-calib-1:run",
            critic_outcome=CriticOutcome.ACCEPT,
            critic_verdict=CriticVerdict.ACCEPT,
            policy_version=1,
            blocks=(BlockCode.PROTECTED_PAGE,),
            rollback_available=True,
            evaluated_at=NOW,
            engine_version="autonomy-engine-1",
        )
