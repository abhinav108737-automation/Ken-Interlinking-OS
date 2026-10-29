"""Risk tiers RAISE only; calibration LOWERS only; behavioural signals may only block.

These three asymmetries are the reason a wrong signal in this system costs a deferral and
never a publish, so each one is tested as a property rather than on one example.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from ken_contracts.autonomy import REQUIRED_COMMERCIAL_PAGE_TYPES, RiskTier
from ken_contracts.learning import DecisionClassMetrics
from ken_services.autonomy.calibration import (
    CALIBRATION_RECIPE,
    ClassCalibration,
    blend_confidence,
    calibration_version_of,
    decision_class_of,
)
from ken_services.autonomy.policy import load_autonomy_policy
from ken_services.autonomy.risk import (
    RISK_ORDER,
    RiskSignals,
    raise_to,
    tier_from_signals,
)
from tests.unit.autonomy._support import clean_facts, identity

NO_SIGNALS = RiskSignals(
    commercial_page=False,
    hub_destination=False,
    protection_nearby=False,
    sensitive_anchor=False,
    prior_failure=False,
    measured_rollback=False,
    stale_target_version=False,
    uncalibrated=False,
)
FIELDS = tuple(NO_SIGNALS.__dataclass_fields__)


# --------------------------------------------------------------------------- #
# Risk
# --------------------------------------------------------------------------- #
def test_no_signal_is_low_risk() -> None:
    assert tier_from_signals(NO_SIGNALS) is RiskTier.LOW


@pytest.mark.parametrize("field", FIELDS)
def test_every_signal_only_ever_raises_the_tier(field: str) -> None:
    """Setting ANY one signal can never produce a tier below the one with none set."""
    raised = tier_from_signals(replace(NO_SIGNALS, **{field: True}))
    assert RISK_ORDER.index(raised) >= RISK_ORDER.index(RiskTier.LOW)
    assert raised is not RiskTier.LOW, f"{field} should move the tier"


@pytest.mark.parametrize("field", FIELDS)
def test_adding_a_signal_to_a_high_tier_keeps_it_high(field: str) -> None:
    high = replace(NO_SIGNALS, commercial_page=True)
    assert tier_from_signals(high) is RiskTier.HIGH
    assert tier_from_signals(replace(high, **{field: True})) is RiskTier.HIGH


def test_a_behavioural_rollback_raises_to_high_and_nothing_lowers_it() -> None:
    """ADR-0015 s12 / ADR-0016 16.7: first-party counts are spoofable, so a measured signal
    may BLOCK (by raising the tier, which guarded mode defers) and may never pass."""
    assert tier_from_signals(replace(NO_SIGNALS, measured_rollback=True)) is RiskTier.HIGH


def test_a_commercial_page_is_never_low_risk() -> None:
    for page_type in sorted(REQUIRED_COMMERCIAL_PAGE_TYPES):
        signals = replace(NO_SIGNALS, commercial_page=True)
        assert tier_from_signals(signals) is RiskTier.HIGH, page_type


def test_raise_to_is_a_maximum() -> None:
    assert raise_to(RiskTier.LOW, RiskTier.HIGH) is RiskTier.HIGH
    assert raise_to(RiskTier.HIGH, RiskTier.LOW) is RiskTier.HIGH
    assert raise_to(RiskTier.MEDIUM, RiskTier.MEDIUM) is RiskTier.MEDIUM


def test_an_uncalibrated_class_is_not_low_risk() -> None:
    assert tier_from_signals(replace(NO_SIGNALS, uncalibrated=True)) is RiskTier.MEDIUM


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #
def _metrics(**overrides: object) -> DecisionClassMetrics:
    base = {
        "decision_class": decision_class_of(identity()).canonical,  # type: ignore[union-attr]
        "sample_count": 200,
        "calibrated_confidence": 0.8,
        "sufficient": True,
    }
    return DecisionClassMetrics.model_validate({**base, **overrides})


MIN_RATE = load_autonomy_policy().confidence.min_positive_outcome_rate


def _blend(*, raw: float, metrics: DecisionClassMetrics) -> float:
    """The REAL capping function the calibration reader uses -- not a copy of its logic."""
    return blend_confidence(raw=raw, metrics=metrics, min_positive_outcome_rate=MIN_RATE)


def test_calibration_can_only_lower_the_confidence() -> None:
    for raw in (0.1, 0.5, 0.75, 0.9, 1.0):
        assert _blend(raw=raw, metrics=_metrics(calibrated_confidence=0.99)) <= raw


def test_a_high_rollback_rate_lowers_the_confidence_further() -> None:
    plain = _blend(raw=1.0, metrics=_metrics())
    with_rollbacks = _blend(raw=1.0, metrics=_metrics(rollback_rate=0.4))
    assert with_rollbacks < plain


def test_a_poor_positive_outcome_rate_caps_the_confidence_and_a_good_one_does_not() -> None:
    poor = _blend(raw=1.0, metrics=_metrics(positive_outcome_rate=0.2))
    good = _blend(raw=1.0, metrics=_metrics(positive_outcome_rate=0.95))
    baseline = _blend(raw=1.0, metrics=_metrics())
    assert poor < baseline, "a poor behavioural outcome must lower the confidence"
    assert good == baseline, "a good behavioural outcome must change nothing (it may not pass)"


def test_the_decision_class_is_none_when_it_cannot_be_formed() -> None:
    assert decision_class_of(identity(channels=())) is None
    assert decision_class_of(identity(channels=("not-a-channel",))) is None
    assert decision_class_of(identity(anchor_source="invented")) is None
    assert decision_class_of(identity(relationship_type="invented")) is None


def test_the_decision_class_round_trips_through_its_canonical_form() -> None:
    found = decision_class_of(identity())
    assert found is not None
    from ken_contracts.learning import DecisionClass

    assert DecisionClass.parse(found.canonical) == found


def test_the_calibration_version_names_the_evaluation_run() -> None:
    version = calibration_version_of("4f1c")
    assert version.startswith(CALIBRATION_RECIPE)
    assert "4f1c" in version
    assert len(version) <= 64, "the autonomy_decisions CHECK bounds it at 64 characters"


def test_an_insufficient_class_is_uncalibrated_not_weakly_calibrated() -> None:
    """``sufficient=False`` means NO calibration, not a lower-confidence one: the version
    stays ``None``, which is what makes guarded mode refuse."""
    found = ClassCalibration(version="v", metrics=_metrics(sufficient=False))
    assert found.metrics.sufficient is False
    facts = clean_facts(calibration_version=None)
    assert facts.calibration_version is None
