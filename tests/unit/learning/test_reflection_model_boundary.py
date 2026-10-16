"""The bounded model role: the strongest safety property of the phase (ADR-0016 15.2d).

The claim being tested is exact: **the model chooses and explains; it never emits a number that
is stored, never names a field, and cannot reach a frozen path.** It is a mechanism and not a
promise because

* the option set is enumerated by deterministic code and persisted in the evidence (15.7.7),
* ``choice_index`` is re-checked against the LIVE option count -- not the count the model
  claims -- so under-reporting ``option_count`` buys nothing,
* every model problem (disabled, unreachable, timed out, invalid, out of range) falls back to
  the DETERMINISTIC choice and records ``fallback_used`` with a ``FallbackReason``, exactly as
  the agent nodes do, and
* a stored proposal can therefore be reconstructed without the model: the options, the window
  and every count are in the evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from ken_contracts.agents import FallbackReason
from ken_contracts.learning import ProposalDraft, ProposalEvidence, TargetKind
from ken_contracts.model import (
    GenerationOptions,
    Message,
    ModelRole,
    ModelTimeoutError,
    ModelUnavailableError,
    StructuredResult,
)
from ken_core.providers.model.mock import MockModelProvider
from ken_services.learning.aggregate import build_evidence
from ken_services.learning.prompt import KEEP_CURRENT
from ken_services.learning.reflect import _choose, enumerate_options
from tests.unit.learning.reflection_support import documents, observation, policy, report

DOCUMENTS, CROSS = documents()
POLICY = policy()


def _setup() -> tuple[Any, Any, Any]:
    aggregate = report(observation())
    options, specs = enumerate_options(
        target_kind=TargetKind.RANKING,
        base_document=DOCUMENTS[TargetKind.RANKING],
        observations=aggregate.observations,
        policy=POLICY,
        cross_check=CROSS,
    )
    assert len(options) == 3, [option.label for option in options]
    return aggregate, options, specs


async def _choice(provider: Any, *, use_model: bool = True) -> tuple[Any, int]:
    aggregate, options, specs = _setup()
    return await _choose(
        provider=provider,
        use_model=use_model,
        report=aggregate,
        target_kind=TargetKind.RANKING,
        options=options,
        specs=specs,
        policy=POLICY,
    )


class _Unavailable:
    """A provider whose REASONING role cannot answer."""

    name = "unavailable"

    async def complete_structured[S: BaseModel](
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        schema: type[S],
        options: GenerationOptions | None = None,
        max_attempts: int = 2,
    ) -> StructuredResult[S]:
        raise ModelUnavailableError("no model server")


class _TimesOut(_Unavailable):
    name = "timeout"

    async def complete_structured[S: BaseModel](
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        schema: type[S],
        options: GenerationOptions | None = None,
        max_attempts: int = 2,
    ) -> StructuredResult[S]:
        raise ModelTimeoutError("too slow")


class _Explodes(_Unavailable):
    name = "explodes"

    async def complete_structured[S: BaseModel](
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        schema: type[S],
        options: GenerationOptions | None = None,
        max_attempts: int = 2,
    ) -> StructuredResult[S]:
        raise RuntimeError("a transport nobody anticipated")


# --------------------------------------------------------------------------- #
# The happy path: the model picks an index and nothing else
# --------------------------------------------------------------------------- #
async def test_a_valid_draft_selects_an_option_and_records_the_model() -> None:
    provider = MockModelProvider()
    provider.register(
        ProposalDraft,
        {
            "title": "Favour the explainable relationship",
            "rationale": "Destination reasons dominate the rejections in the one class above "
            "the sample floor.",
            "choice_index": 1,
            "option_count": 3,
        },
    )
    choice, calls = await _choice(provider)
    assert (choice.index, calls) == (1, 1)
    assert choice.fallback_reason is None
    assert choice.model == "mock-reasoning"
    assert choice.title == "Favour the explainable relationship"


async def test_the_model_is_asked_only_for_a_draft_and_only_under_the_reasoning_role() -> None:
    provider = MockModelProvider()
    provider.register(
        ProposalDraft,
        {"title": "t", "rationale": "r", "choice_index": 0, "option_count": 3},
    )
    await _choice(provider)
    structured = [call for call in provider.calls if call.method == "complete_structured"]
    assert len(structured) == 1
    assert structured[0].role is ModelRole.REASONING
    assert structured[0].schema == "ProposalDraft"


async def test_choosing_the_last_option_means_no_proposal() -> None:
    """``keep_current`` is a real answer: a model that reads the table as inconclusive says so
    and nothing is written. The caller checks ``changes``, which is empty."""
    provider = MockModelProvider()
    provider.register(
        ProposalDraft,
        {"title": "t", "rationale": "r", "choice_index": 2, "option_count": 3},
    )
    choice, _calls = await _choice(provider)
    _aggregate, options, _specs = _setup()
    assert options[choice.index].label == KEEP_CURRENT
    assert options[choice.index].changes == ()


# --------------------------------------------------------------------------- #
# Every failure mode falls back to the DETERMINISTIC choice
# --------------------------------------------------------------------------- #
async def test_a_disabled_model_falls_back_to_the_deterministic_choice() -> None:
    """``--no-model`` must still produce the deterministic rule's own answer, not inaction:
    option 0 is what the code prefers, which is why ``keep_current`` is last."""
    choice, calls = await _choice(MockModelProvider(), use_model=False)
    assert (choice.index, calls) == (0, 0)
    assert choice.fallback_reason is FallbackReason.MODEL_DISABLED
    assert choice.model is None
    assert "deterministic rule" in choice.rationale


async def test_no_provider_at_all_falls_back() -> None:
    choice, calls = await _choice(None)
    assert (choice.index, calls) == (0, 0)
    assert choice.fallback_reason is FallbackReason.MODEL_UNAVAILABLE


@pytest.mark.parametrize(
    ("provider", "reason"),
    [
        (_Unavailable(), FallbackReason.MODEL_UNAVAILABLE),
        (_TimesOut(), FallbackReason.MODEL_TIMEOUT),
        (_Explodes(), FallbackReason.MODEL_UNAVAILABLE),
    ],
    ids=["unavailable", "timeout", "unexpected"],
)
async def test_a_model_problem_never_fails_the_run(provider: Any, reason: FallbackReason) -> None:
    choice, calls = await _choice(provider)
    assert (choice.index, calls) == (0, 1)
    assert choice.fallback_reason is reason


async def test_invalid_output_falls_back_with_a_schema_violation() -> None:
    provider = MockModelProvider()
    provider.script_violation(ProposalDraft, raw_text="not json at all")
    choice, calls = await _choice(provider)
    assert (choice.index, calls) == (0, 1)
    assert choice.fallback_reason is FallbackReason.SCHEMA_VIOLATION


async def test_an_unregistered_schema_falls_back_rather_than_fabricating() -> None:
    """``MockModelProvider`` raises for an unregistered schema precisely so a test cannot pass
    on a fabricated value. The fallback is what the real path does too."""
    choice, calls = await _choice(MockModelProvider())
    assert (choice.index, calls) == (0, 1)
    assert choice.fallback_reason is FallbackReason.SCHEMA_VIOLATION


# --------------------------------------------------------------------------- #
# The model cannot widen its own reach (15.7.7)
# --------------------------------------------------------------------------- #
async def test_an_index_outside_the_live_option_set_is_refused() -> None:
    """A draft claiming ``option_count = 9`` validates against ITSELF (9 > choice_index), so
    only the LIVE count can refuse it. Without this check the model could name an index the
    server never offered."""
    provider = MockModelProvider()
    provider.register(
        ProposalDraft,
        {"title": "t", "rationale": "r", "choice_index": 7, "option_count": 9},
    )
    choice, calls = await _choice(provider)
    assert (choice.index, calls) == (0, 1)
    assert choice.fallback_reason is FallbackReason.SCHEMA_VIOLATION


async def test_under_reporting_the_option_count_is_refused_too() -> None:
    """The mirror case: ``option_count = 1`` with ``choice_index = 0`` is self-consistent and
    still a lie about what was offered, so it is refused rather than quietly accepted."""
    provider = MockModelProvider()
    provider.register(
        ProposalDraft,
        {"title": "t", "rationale": "r", "choice_index": 0, "option_count": 1},
    )
    choice, _calls = await _choice(provider)
    assert choice.fallback_reason is FallbackReason.SCHEMA_VIOLATION


# --------------------------------------------------------------------------- #
# Evidence: no model-produced number, and the choice is reconstructable
# --------------------------------------------------------------------------- #
def test_evidence_records_the_whole_option_set_and_the_chosen_index() -> None:
    aggregate, options, _specs = _setup()
    evidence = build_evidence(
        report=aggregate,
        options=options,
        chosen_index=1,
        generated_at=datetime(2026, 10, 16, 9, 0, tzinfo=UTC),
        model="mock-reasoning",
    )
    assert isinstance(evidence, ProposalEvidence)
    assert len(evidence.options) == len(options)
    assert evidence.chosen_index == 1
    assert evidence.sample_count_total == aggregate.sample_count_total
    assert evidence.fallback_used is False
    # Every number in the evidence came from the aggregate report, not from the draft.
    assert evidence.observations == aggregate.observations
    assert evidence.policy_versions == aggregate.policy_versions


def test_a_fallback_is_recorded_without_a_model() -> None:
    """``ProposalEvidence`` refuses a fallback that also names a model, and the DB CHECK
    ``NOT fallback_used OR model IS NULL`` says the same thing. A reader can therefore always
    tell whether a model answered."""
    aggregate, options, _specs = _setup()
    evidence = build_evidence(
        report=aggregate,
        options=options,
        chosen_index=0,
        generated_at=datetime(2026, 10, 16, 9, 0, tzinfo=UTC),
        model=None,
        fallback_reason=FallbackReason.MODEL_DISABLED,
    )
    assert evidence.fallback_used is True
    assert evidence.model is None
    assert evidence.fallback_reason is FallbackReason.MODEL_DISABLED
