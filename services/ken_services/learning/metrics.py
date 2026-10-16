"""Scoring a run's case outcomes into :class:`~ken_contracts.learning.EvaluationMetrics`.

Pure, and deliberately built from ``math``/``statistics`` only: counts, means, ratios and
reliability bins. ADR-0016 section 11 states the reason plainly -- the design makes **no
significance claims**, and significance testing is the only thing that would need numpy or
scipy. If a later phase wants a confidence interval, that is a dependency decision for the
user, not something to smuggle in behind a metric.

What is measured, and what each number is honestly about:

* **per suite** -- ``pass_rate``, ``must_not_violations`` (zero tolerance) and, against a
  recorded baseline run, that suite's baseline pass rate;
* **ranking quality** -- ``precision_at_1``, ``recall_at_3`` and Kendall tau, computed ONLY
  over cases that recorded a human-preferred destination order. A ranking metric averaged over
  cases with no recorded preference would be a number about nothing, so such cases are excluded
  and the suite metric is ``None`` when none exists;
* **critic precision and recall** -- against the ``human_verdict`` a critic case records, with
  ``accept`` as the positive class (:func:`critic_scores`);
* **calibration** -- reliability bins and expected calibration error per decision class,
  from the RAW confidence a case produced against whether it was actually right. **Reported,
  never gating, in Phase 8** (ADR-0016 section 7 / 15.6): a calibration computed on ~65
  synthetic pages is a demonstration of the mechanism and not a calibration of anything.

``by_decision_class`` is keyed by the canonical ``DecisionClass`` string, which the database
CHECK on ``policy_evaluations.metrics`` requires, so Phase 9 reads per-class numbers without a
schema change. Its editorial fields (``approval_rate``, ``edit_rate``, ``reject_rate``,
``rollback_rate``, ``positive_outcome_rate``) stay ``None`` here **on purpose**: those come from
the editorial and behavioural streams via ``aggregate.py``, and an offline eval has no review
actions to count. Filling them with eval pass rates would be the exact category error this
phase exists to prevent.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from ken_contracts.learning import (
    DecisionClassMetrics,
    EvalSuite,
    EvaluationMetrics,
    SuiteMetrics,
)
from ken_services.learning.scorers import CaseOutcome, RankingObservation

__all__ = [
    "RELIABILITY_BINS",
    "CriticScores",
    "build_metrics",
    "critic_scores",
    "decision_class_metrics",
    "kendall_tau",
    "precision_at_1",
    "recall_at_k",
    "reliability",
    "suite_metrics",
]

RELIABILITY_BINS: Final = 5
"""Equal-width bins over [0, 1]. Five keeps each bin populated on a corpus this size; the
bin edges are reported with the numbers so a later run is comparable."""
_ROUND: Final = 6


def _rate(part: int, whole: int) -> float | None:
    return None if whole == 0 else round(part / whole, _ROUND)


# --------------------------------------------------------------------------- #
# Ranking metrics (destination-selection, policy-regression)
# --------------------------------------------------------------------------- #
def precision_at_1(observations: Sequence[RankingObservation]) -> float | None:
    """Share of cases whose top-ranked destination is the human's first preference."""
    usable = [item for item in observations if item.preferred]
    if not usable:
        return None
    hits = sum(1 for item in usable if item.predicted and item.predicted[0] == item.preferred[0])
    return round(hits / len(usable), _ROUND)


def recall_at_k(observations: Sequence[RankingObservation], k: int = 3) -> float | None:
    """Mean share of the human-preferred destinations that appear in the top ``k``.

    Normalised by ``min(k, len(preferred))``, so a case with one preferred destination is not
    penalised for the two slots it could never fill.
    """
    usable = [item for item in observations if item.preferred]
    if not usable:
        return None
    scores: list[float] = []
    for item in usable:
        wanted = set(item.preferred)
        found = len(wanted & set(item.predicted[:k]))
        scores.append(found / min(k, len(wanted)))
    return round(math.fsum(scores) / len(scores), _ROUND)


def kendall_tau(observations: Sequence[RankingObservation]) -> float | None:
    """Mean Kendall tau between the predicted and the human-preferred order.

    Computed over the destinations present in BOTH orders, pair by pair: a pair ordered the
    same way in both is concordant, a pair ordered the other way is discordant, and
    ``tau = (concordant - discordant) / pairs``. Cases with fewer than two shared
    destinations carry no ordering information and are skipped. No tie correction is applied
    because both orders here are strict.
    """
    taus: list[float] = []
    for item in observations:
        shared = [key for key in item.preferred if key in item.predicted]
        if len(shared) < 2:
            continue
        rank_a = {key: index for index, key in enumerate(item.predicted)}
        rank_b = {key: index for index, key in enumerate(item.preferred)}
        concordant = discordant = 0
        for i in range(len(shared)):
            for j in range(i + 1, len(shared)):
                left, right = shared[i], shared[j]
                a = rank_a[left] - rank_a[right]
                b = rank_b[left] - rank_b[right]
                if a * b > 0:
                    concordant += 1
                elif a * b < 0:
                    discordant += 1
        pairs = concordant + discordant
        if pairs:
            taus.append((concordant - discordant) / pairs)
    if not taus:
        return None
    return round(math.fsum(taus) / len(taus), _ROUND)


# --------------------------------------------------------------------------- #
# Critic precision and recall
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CriticScores:
    """``accept`` as the positive class, against the recorded human verdict."""

    precision: float | None
    recall: float | None
    support: int

    def as_detail(self) -> str:
        return f"precision={self.precision} recall={self.recall} n={self.support}"


def critic_scores(outcomes: Iterable[CaseOutcome]) -> CriticScores:
    """Precision and recall of the critic's ``accept`` verdict over the cases that record one.

    A critic that rubber-stamps everything has recall 1 and poor precision; a critic that
    starves the queue has precision 1 and poor recall. Both failure modes the suite README
    names are therefore visible in one pair of numbers.
    """
    pairs = [item.critic_observation for item in outcomes if item.critic_observation]
    if not pairs:
        return CriticScores(precision=None, recall=None, support=0)
    true_positive = sum(1 for predicted, human in pairs if predicted == "accept" == human)
    predicted_positive = sum(1 for predicted, _human in pairs if predicted == "accept")
    actual_positive = sum(1 for _predicted, human in pairs if human == "accept")
    return CriticScores(
        precision=_rate(true_positive, predicted_positive),
        recall=_rate(true_positive, actual_positive),
        support=len(pairs),
    )


# --------------------------------------------------------------------------- #
# Calibration (reported, never gating in Phase 8)
# --------------------------------------------------------------------------- #
def reliability(
    samples: Sequence[tuple[float, bool]], *, bins: int = RELIABILITY_BINS
) -> tuple[tuple[tuple[float, float, int], ...], float | None]:
    """Reliability bins and the expected calibration error of ``(confidence, correct)`` pairs.

    A bin is ``(bin_upper, observed_correct_rate, n)``. ECE is the sample-weighted mean
    absolute gap between a bin's mean predicted confidence and its observed correctness, which
    is the standard definition and needs nothing beyond ``math``.
    """
    if not samples or bins < 1:
        return (), None
    width = 1.0 / bins
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, correct in samples:
        index = min(bins - 1, max(0, int(confidence / width)))
        buckets[index].append((confidence, correct))
    rows: list[tuple[float, float, int]] = []
    gap_total = 0.0
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        upper = round((index + 1) * width, _ROUND)
        observed = math.fsum(1.0 for _c, correct in bucket if correct) / len(bucket)
        predicted = math.fsum(confidence for confidence, _correct in bucket) / len(bucket)
        rows.append((upper, round(observed, _ROUND), len(bucket)))
        gap_total += len(bucket) * abs(observed - predicted)
    return tuple(rows), round(gap_total / len(samples), _ROUND)


def decision_class_metrics(
    outcomes: Sequence[CaseOutcome], *, min_sample_count: int
) -> dict[str, DecisionClassMetrics]:
    """Per-decision-class rows, keyed by the canonical class string.

    ``sample_count`` is the number of EVAL CASES in the class, not a number of production
    decisions -- which is why ``sufficient`` is almost always false on this corpus and why a
    gate over a candidate policy is ``inconclusive`` rather than ``pass``. That is the
    mechanism ADR-0016 15.4 asks for, working as intended.
    """
    grouped: dict[str, list[CaseOutcome]] = {}
    for item in outcomes:
        if item.decision_class is None:
            continue
        grouped.setdefault(item.decision_class, []).append(item)
    rows: dict[str, DecisionClassMetrics] = {}
    for key in sorted(grouped):
        members = grouped[key]
        confidences = [
            (item.confidence, item.passed) for item in members if item.confidence is not None
        ]
        bins, ece = reliability(confidences)
        rows[key] = DecisionClassMetrics(
            decision_class=key,
            sample_count=len(members),
            mean_raw_confidence=(
                None
                if not confidences
                else round(math.fsum(value for value, _ in confidences) / len(confidences), _ROUND)
            ),
            calibrated_confidence=None,
            expected_calibration_error=ece,
            reliability_bins=bins,
            sufficient=len(members) >= min_sample_count,
        )
    return rows


# --------------------------------------------------------------------------- #
# Suite and run metrics
# --------------------------------------------------------------------------- #
def suite_metrics(
    suite: EvalSuite,
    outcomes: Sequence[CaseOutcome],
    *,
    baseline_pass_rate: float | None = None,
) -> SuiteMetrics:
    passed = sum(1 for item in outcomes if item.passed)
    rankings = [item.ranking for item in outcomes if item.ranking is not None]
    return SuiteMetrics(
        suite=suite,
        cases=len(outcomes),
        passed=passed,
        failed=len(outcomes) - passed,
        must_not_violations=sum(1 for item in outcomes if item.must_not_hit),
        pass_rate=None if not outcomes else round(passed / len(outcomes), _ROUND),
        baseline_pass_rate=baseline_pass_rate,
        precision_at_1=precision_at_1(rankings),
        recall_at_3=recall_at_k(rankings, 3),
        kendall_tau=kendall_tau(rankings),
    )


def build_metrics(
    outcomes: Sequence[CaseOutcome],
    *,
    suites: Sequence[EvalSuite],
    min_sample_count: int,
    baseline: Mapping[EvalSuite, float] | None = None,
) -> EvaluationMetrics:
    """Assemble the run's :class:`EvaluationMetrics`, one suite row per requested suite."""
    by_suite: list[SuiteMetrics] = []
    for suite in suites:
        members = [item for item in outcomes if item.suite is suite]
        by_suite.append(
            suite_metrics(
                suite,
                members,
                baseline_pass_rate=None if baseline is None else baseline.get(suite),
            )
        )
    return EvaluationMetrics(
        cases_total=sum(row.cases for row in by_suite),
        cases_passed=sum(row.passed for row in by_suite),
        by_suite=tuple(by_suite),
        by_decision_class=decision_class_metrics(outcomes, min_sample_count=min_sample_count),
    )
