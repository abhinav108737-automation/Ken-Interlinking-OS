"""The eval runner and the gate (ADR-0016 §7 / §15).

The load-bearing tests here are:

* **determinism** -- the whole runner over every suite, twice, byte-diffed. Without it the
  default-provider claim ("the same datasets and seed give a byte-identical report, so CI can
  gate on it") is a hope;
* **a hard-fail suite cannot be waived by any policy value** -- asserted at both ends: the
  contract refuses a policy document that drops one, and the runner unions the policy's list
  with the contract's own so a document loaded from anywhere else cannot waive one either;
* **a must_not hit fails the gate**, at zero tolerance;
* **below the sample floor the result is `inconclusive`, never `pass`** -- which is what makes
  "Phase 8 promotes nothing" a mechanism;
* **a live provider never gates**;
* every suite's scorer really runs, and the suites really can fail -- a mutated expectation
  must turn into a failing case.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ken_contracts.learning import (
    HARD_FAIL_SUITES,
    DecisionClass,
    DecisionClassMetrics,
    DecisionClassObservation,
    EvalSuite,
    EvaluationMetrics,
    EvaluationMode,
    GateReasonCode,
    GateResult,
    LearningPolicy,
    SuiteMetrics,
)
from ken_core.config import Settings
from ken_core.providers.model.mock import MockModelProvider
from ken_services.learning import datasets as ds
from ken_services.learning.evaluate import (
    BehaviouralBlockOnly,
    EvaluationReport,
    evaluate_gate,
    report_json,
    run_suites,
)
from ken_services.learning.metrics import kendall_tau, precision_at_1, recall_at_k, reliability
from ken_services.learning.policy import load_configured_learning_policy
from ken_services.learning.protocol import BEHAVIOURAL_REASON_CODES, EvaluationRequest
from ken_services.learning.scorers import RankingObservation

ALL_SUITES = tuple(ds.available_suites())
CLASS = "same_market|target_topic|semantic"


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture(scope="module")
def policy(settings: Settings) -> LearningPolicy:
    return load_configured_learning_policy(settings)


async def _run(settings: Settings, policy: LearningPolicy, *suites: EvalSuite) -> EvaluationReport:
    provider = MockModelProvider(settings)
    try:
        return await run_suites(
            settings=settings,
            policy=policy,
            request=EvaluationRequest(suites=suites or ALL_SUITES, seed=1234),
            model_provider=provider,
        )
    finally:
        await provider.aclose()


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
async def test_the_same_datasets_and_seed_give_a_byte_identical_report(
    settings: Settings, policy: LearningPolicy
) -> None:
    first = report_json(await _run(settings, policy))
    second = report_json(await _run(settings, policy))
    assert first == second
    payload = json.loads(first)
    assert payload["seed"] == 1234
    assert payload["provider"] == "mock"
    assert payload["runner_version"]
    assert payload["dataset_manifests"]
    assert set(payload["policy_shas"]) == {
        "taxonomy",
        "recommendation",
        "ranking",
        "agents",
        "learning",
    }
    assert payload["model_role_models"] == {
        "embedding": "mock-embedding",
        "fast": "mock-fast",
        "reasoning": "mock-reasoning",
    }


async def test_the_report_carries_no_wall_clock(settings: Settings, policy: LearningPolicy) -> None:
    """A timestamp would break the byte-identity above, so the report deliberately has none:
    the run's times live on the job run and the policy_evaluations row."""
    report = await _run(settings, policy, EvalSuite.PROPOSAL_SAFETY)
    payload = json.loads(report_json(report))
    text = json.dumps(payload)
    assert "started_at" not in text
    assert "finished_at" not in text


# --------------------------------------------------------------------------- #
# Every suite runs, and every suite can fail
# --------------------------------------------------------------------------- #
async def test_every_populated_suite_scores_every_case(
    settings: Settings, policy: LearningPolicy
) -> None:
    report = await _run(settings, policy)
    rows = {row.suite: row for row in report.metrics.by_suite}
    assert set(rows) == set(ALL_SUITES)
    for suite, row in rows.items():
        assert row.cases > 0, suite
        assert row.pass_rate == 1.0, (suite, row.failed)
        assert row.must_not_violations == 0, suite
        decisions = {item.decision for item in report.outcomes if item.suite is suite}
        assert "fixture_error" not in decisions
        assert "suite_has_no_scorer" not in decisions


@pytest.mark.parametrize("suite", ALL_SUITES, ids=lambda suite: suite.value)
async def test_a_suite_fails_when_an_expectation_no_longer_holds(
    suite: EvalSuite, settings: Settings, policy: LearningPolicy, tmp_path: Path
) -> None:
    """A suite that cannot fail is not a gate. Rewrite the first case's expected decision to
    something the engine will never produce and assert the suite goes red."""
    source = ds.default_dataset_root() / suite.value
    target = tmp_path / suite.value
    target.mkdir(parents=True)
    case_file_name = ""
    for path in source.iterdir():
        if path.is_file():
            (target / path.name).write_bytes(path.read_bytes())
            if path.suffix == ".jsonl":
                case_file_name = path.name
    case_file = target / case_file_name
    lines = case_file.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["expected"] = {
        "decision": "a-decision-the-engine-never-produces",
        "acceptable_alternatives": [],
        "must_not": [],
    }
    lines[0] = json.dumps(first, sort_keys=True, ensure_ascii=False)
    case_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _rehash(target / "MANIFEST.yaml", case_file)

    provider = MockModelProvider(settings)
    ds._clear_caches()
    try:
        report = await run_suites(
            settings=settings,
            policy=policy,
            request=EvaluationRequest(suites=(suite,), seed=1),
            model_provider=provider,
            dataset_root=tmp_path,
        )
    finally:
        await provider.aclose()
        ds._clear_caches()
    row = report.metrics.by_suite[0]
    assert row.failed == 1, f"{suite.value} did not fail on a mutated expectation"
    assert row.pass_rate is not None and row.pass_rate < 1.0


def _rehash(manifest: Path, case_file: Path) -> None:
    import hashlib
    import re

    digest = hashlib.sha256(case_file.read_bytes()).hexdigest()
    text = re.sub(
        rf"^  {re.escape(case_file.name)}: [0-9a-f]{{64}}$",
        f"  {case_file.name}: {digest}",
        manifest.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    manifest.write_text(text, encoding="utf-8")


async def test_a_must_not_hit_fails_the_gate(
    settings: Settings, policy: LearningPolicy, tmp_path: Path
) -> None:
    """Zero tolerance: one forbidden decision fails the gate whatever the pass rate."""
    suite = EvalSuite.PROTECTED_CONTENT
    source = ds.default_dataset_root() / suite.value
    target = tmp_path / suite.value
    target.mkdir(parents=True)
    for path in source.iterdir():
        if path.is_file():
            (target / path.name).write_bytes(path.read_bytes())
    case_file = next(path for path in target.iterdir() if path.suffix == ".jsonl")
    lines = case_file.read_text(encoding="utf-8").splitlines()
    patched: list[str] = []
    for line in lines:
        payload = json.loads(line)
        if payload["case_id"] == "pc-eligible-pair-is-allowed":
            # The control case: forbid the outcome the engine correctly produces.
            payload["expected"] = {
                "decision": "reject:target_protected",
                "acceptable_alternatives": [],
                "must_not": ["allowed"],
            }
        patched.append(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    case_file.write_text("\n".join(patched) + "\n", encoding="utf-8")
    _rehash(target / "MANIFEST.yaml", case_file)

    provider = MockModelProvider(settings)
    ds._clear_caches()
    try:
        report = await run_suites(
            settings=settings,
            policy=policy,
            request=EvaluationRequest(suites=(suite,), seed=1),
            model_provider=provider,
            dataset_root=tmp_path,
        )
    finally:
        await provider.aclose()
        ds._clear_caches()
    assert report.metrics.by_suite[0].must_not_violations == 1
    assert report.gate_result is GateResult.FAIL
    assert GateReasonCode.MUST_NOT_VIOLATION in {reason.code for reason in report.gate_reasons}


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def _suite_row(suite: EvalSuite, *, cases: int, passed: int, must_not: int = 0) -> SuiteMetrics:
    return SuiteMetrics(
        suite=suite,
        cases=cases,
        passed=passed,
        failed=cases - passed,
        must_not_violations=must_not,
        pass_rate=None if cases == 0 else passed / cases,
    )


def _metrics(*rows: SuiteMetrics, sufficient: bool = True) -> EvaluationMetrics:
    return EvaluationMetrics(
        cases_total=sum(row.cases for row in rows),
        cases_passed=sum(row.passed for row in rows),
        by_suite=rows,
        by_decision_class={
            CLASS: DecisionClassMetrics(
                decision_class=CLASS, sample_count=40 if sufficient else 3, sufficient=sufficient
            )
        },
    )


def _all_clean(policy: LearningPolicy, **override: SuiteMetrics) -> EvaluationMetrics:
    rows = [
        override.get(suite.value.replace("-", "_"), _suite_row(suite, cases=10, passed=10))
        for suite in policy.gates.suites
    ]
    return _metrics(*rows)


def test_a_fully_clean_run_passes(policy: LearningPolicy) -> None:
    result, reasons = evaluate_gate(metrics=_all_clean(policy), policy=policy)
    assert result is GateResult.PASS
    assert reasons == ()


def test_below_the_sample_floor_the_result_is_inconclusive_never_pass(
    policy: LearningPolicy,
) -> None:
    """ADR-0016 15.4 / 16.1. This is why Phase 8 exits with zero promotions by construction."""
    rows = [_suite_row(suite, cases=10, passed=10) for suite in policy.gates.suites]
    result, reasons = evaluate_gate(metrics=_metrics(*rows, sufficient=False), policy=policy)
    assert result is not GateResult.PASS
    assert result is GateResult.INCONCLUSIVE
    assert GateReasonCode.SAMPLE_TOO_SMALL in {reason.code for reason in reasons}


def test_a_declared_suite_that_did_not_run_is_inconclusive(policy: LearningPolicy) -> None:
    """Absence of evidence is not evidence of safety, and inconclusive is not promotable."""
    rows = [_suite_row(EvalSuite.CRITIC, cases=4, passed=4)]
    result, reasons = evaluate_gate(metrics=_metrics(*rows), policy=policy)
    assert result is GateResult.INCONCLUSIVE
    not_run = {reason.suite for reason in reasons if reason.code is GateReasonCode.SUITE_NOT_RUN}
    assert not_run >= HARD_FAIL_SUITES


@pytest.mark.parametrize("suite", sorted(HARD_FAIL_SUITES, key=str), ids=str)
def test_one_failing_case_in_a_hard_fail_suite_fails_the_gate(
    suite: EvalSuite, policy: LearningPolicy
) -> None:
    rows = [
        _suite_row(other, cases=10, passed=9 if other is suite else 10)
        for other in policy.gates.suites
    ]
    result, reasons = evaluate_gate(metrics=_metrics(*rows), policy=policy)
    assert result is GateResult.FAIL
    codes = {(reason.code, reason.suite) for reason in reasons}
    assert (GateReasonCode.HARD_FAIL_SUITE, suite) in codes


def test_a_policy_document_cannot_waive_a_hard_fail_suite(policy: LearningPolicy) -> None:
    """The contract end: ken_contracts.learning.GatePolicy refuses the document outright."""
    raw = policy.model_dump(mode="json")
    raw["gates"]["hard_fail_suites"] = ["protected-content"]
    with pytest.raises(ValidationError, match="cannot be waived"):
        LearningPolicy.model_validate(raw)


def test_the_runner_honours_a_hard_fail_suite_the_policy_omits(policy: LearningPolicy) -> None:
    """The runner end: even handed a policy object whose hard_fail_suites is narrower than the
    contract's, the gate still treats every HARD_FAIL_SUITES member as unwaivable. The contract
    refuses such a document at load, so this can only happen to a policy constructed in code --
    and it must not be a way through."""
    narrowed_gates = policy.gates.model_copy(update={"hard_fail_suites": (EvalSuite.CRITIC,)})
    narrowed = policy.model_copy(update={"gates": narrowed_gates})
    assert set(narrowed.gates.hard_fail_suites) & HARD_FAIL_SUITES != HARD_FAIL_SUITES
    rows = [
        _suite_row(suite, cases=10, passed=9 if suite is EvalSuite.PROPOSAL_SAFETY else 10)
        for suite in policy.gates.suites
    ]
    result, reasons = evaluate_gate(metrics=_metrics(*rows), policy=narrowed)
    assert result is GateResult.FAIL
    assert (GateReasonCode.HARD_FAIL_SUITE, EvalSuite.PROPOSAL_SAFETY) in {
        (reason.code, reason.suite) for reason in reasons
    }


def test_a_policy_cannot_lower_the_sample_floor(policy: LearningPolicy) -> None:
    raw = policy.model_dump(mode="json")
    raw["gates"]["min_sample_count"] = 5
    with pytest.raises(ValidationError):
        LearningPolicy.model_validate(raw)


def test_below_min_pass_rate_fails(policy: LearningPolicy) -> None:
    rows = [
        _suite_row(
            suite,
            cases=10,
            passed=5 if suite is EvalSuite.DESTINATION_SELECTION else 10,
        )
        for suite in policy.gates.suites
    ]
    result, reasons = evaluate_gate(metrics=_metrics(*rows), policy=policy)
    assert result is GateResult.FAIL
    assert (GateReasonCode.BELOW_MIN_PASS_RATE, EvalSuite.DESTINATION_SELECTION) in {
        (reason.code, reason.suite) for reason in reasons
    }


def test_a_regression_beyond_the_tolerance_fails(policy: LearningPolicy) -> None:
    current = [
        _suite_row(suite, cases=10, passed=8 if suite is EvalSuite.CRITIC else 10)
        for suite in policy.gates.suites
    ]
    baseline = [_suite_row(suite, cases=10, passed=10) for suite in policy.gates.suites]
    result, reasons = evaluate_gate(
        metrics=_metrics(*current), policy=policy, baseline=_metrics(*baseline)
    )
    assert result is GateResult.FAIL
    assert (GateReasonCode.REGRESSION_VS_BASELINE, EvalSuite.CRITIC) in {
        (reason.code, reason.suite) for reason in reasons
    }


def test_a_regression_inside_the_tolerance_still_passes(policy: LearningPolicy) -> None:
    current = [
        _suite_row(suite, cases=100, passed=97 if suite is EvalSuite.CRITIC else 100)
        for suite in policy.gates.suites
    ]
    baseline = [_suite_row(suite, cases=100, passed=100) for suite in policy.gates.suites]
    result, _reasons = evaluate_gate(
        metrics=_metrics(*current), policy=policy, baseline=_metrics(*baseline)
    )
    assert result is GateResult.PASS


# --------------------------------------------------------------------------- #
# Behavioural signals may only block
# --------------------------------------------------------------------------- #
def _observation(*, positive: int, samples: int, sufficient: bool) -> DecisionClassObservation:
    return DecisionClassObservation(
        decision_class=CLASS,
        sample_count=samples,
        approvals=samples,
        edits=0,
        rejections=0,
        rollbacks=0,
        positive_outcomes=positive,
        positive_outcome_rate=positive / samples,
        sufficient=sufficient,
    )


def test_a_negative_behavioural_signal_blocks_an_otherwise_clean_run(
    policy: LearningPolicy,
) -> None:
    blocker = BehaviouralBlockOnly(
        observations=(_observation(positive=1, samples=40, sufficient=True),)
    )
    result, reasons = evaluate_gate(metrics=_all_clean(policy), policy=policy, behavioural=blocker)
    assert result is GateResult.FAIL
    assert GateReasonCode.BEHAVIOURAL_SIGNAL_NEGATIVE in {reason.code for reason in reasons}


def test_a_positive_behavioural_signal_can_never_pass_a_failing_run(
    policy: LearningPolicy,
) -> None:
    """The asymmetry is the point: first-party counts are spoofable, so they may block and may
    never graduate anything."""
    blocker = BehaviouralBlockOnly(
        observations=(_observation(positive=40, samples=40, sufficient=True),)
    )
    rows = [
        _suite_row(suite, cases=10, passed=9 if suite is EvalSuite.PROTECTED_CONTENT else 10)
        for suite in policy.gates.suites
    ]
    result, _reasons = evaluate_gate(metrics=_metrics(*rows), policy=policy, behavioural=blocker)
    assert result is GateResult.FAIL
    assert blocker.blocking_reasons() == ()


def test_block_only_emits_no_other_reason_code() -> None:
    blocker = BehaviouralBlockOnly(
        observations=(
            _observation(positive=1, samples=40, sufficient=True),
            _observation(positive=3, samples=3, sufficient=False),
        )
    )
    assert {reason.code for reason in blocker.blocking_reasons()} <= BEHAVIOURAL_REASON_CODES


# --------------------------------------------------------------------------- #
# A live provider never gates
# --------------------------------------------------------------------------- #
async def test_a_live_provider_can_never_pass_a_gate(
    settings: Settings, policy: LearningPolicy
) -> None:
    """--provider ollama is opt-in and informative only: a gate that depended on whichever
    model happened to be installed would not be a gate. The provider here is still the mock
    (no network); what is asserted is that NAMING a live provider forces inconclusive."""
    provider = MockModelProvider(settings)
    try:
        report = await run_suites(
            settings=settings,
            policy=policy,
            request=EvaluationRequest(
                suites=ALL_SUITES, seed=1, provider_name="ollama", mode=EvaluationMode.OFFLINE
            ),
            model_provider=provider,
        )
    finally:
        await provider.aclose()
    assert report.live_provider is True
    assert report.gate_result is GateResult.INCONCLUSIVE


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def test_ranking_metrics_are_none_without_a_recorded_preference() -> None:
    assert precision_at_1([RankingObservation(predicted=("a", "b"), preferred=())]) is None
    assert recall_at_k([RankingObservation(predicted=("a",), preferred=())]) is None
    assert kendall_tau([RankingObservation(predicted=("a",), preferred=("a",))]) is None


def test_precision_recall_and_tau_on_a_known_ranking() -> None:
    perfect = RankingObservation(predicted=("a", "b", "c"), preferred=("a", "b", "c"))
    reversed_ = RankingObservation(predicted=("c", "b", "a"), preferred=("a", "b", "c"))
    assert precision_at_1([perfect]) == 1.0
    assert precision_at_1([reversed_]) == 0.0
    assert precision_at_1([perfect, reversed_]) == 0.5
    assert recall_at_k([perfect], 3) == 1.0
    assert recall_at_k([RankingObservation(predicted=("x", "y", "z"), preferred=("a",))], 3) == 0.0
    assert kendall_tau([perfect]) == 1.0
    assert kendall_tau([reversed_]) == -1.0


def test_reliability_bins_and_ece_on_hand_computed_values() -> None:
    """Two samples at 0.9 confidence, one right: the bin's observed rate is 0.5 and the gap is
    0.4, so ECE is 0.4. A perfectly calibrated pair has ECE 0."""
    bins, ece = reliability([(0.9, True), (0.9, False)], bins=5)
    assert bins == ((1.0, 0.5, 2),)
    assert ece == pytest.approx(0.4)
    _bins, perfect = reliability([(1.0, True), (0.0, False)], bins=2)
    assert perfect == pytest.approx(0.0)


async def test_by_decision_class_is_keyed_canonically(
    settings: Settings, policy: LearningPolicy
) -> None:
    """The DB CHECK on policy_evaluations.metrics requires the key, and Phase 9 reads
    calibration from it without a schema change."""
    report = await _run(settings, policy)
    for key, row in report.metrics.by_decision_class.items():
        assert DecisionClass.parse(key).canonical == key
        assert row.decision_class == key
        # Editorial and behavioural rates belong to the reflection streams, not to an eval.
        assert row.approval_rate is None
        assert row.rollback_rate is None
        assert row.positive_outcome_rate is None
        assert row.calibrated_confidence is None
    assert CLASS in report.metrics.by_decision_class
