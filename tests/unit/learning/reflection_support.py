"""Builders for the W1 reflection tests (no database, no network).

Everything here is deliberately explicit: an observation is written out number by number so a
test asserts against hand-computed values rather than against another implementation of the
same arithmetic.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Final

from ken_contracts.intelligence import RelationshipType
from ken_contracts.learning import (
    DecisionClass,
    DecisionClassObservation,
    EvidenceWindow,
    LearningPolicy,
    TargetKind,
)
from ken_contracts.recommendations import RetrievalChannel
from ken_services.learning.aggregate import AggregateReport, ClassAggregate
from ken_services.learning.policy import load_learning_policy
from ken_services.learning.reflect import _base_documents
from tests.conftest import make_settings

REPO: Final = Path(__file__).resolve().parents[3]
LEARNING_POLICY_PATH: Final = REPO / "policies" / "learning" / "learning.v1.yaml"
WINDOW: Final = EvidenceWindow(start=date(2026, 7, 1), end=date(2026, 9, 28))

SAME_MARKET: Final = DecisionClass(
    relationship_type=RelationshipType.SAME_MARKET,
    anchor_source="target_topic",
    channels=(RetrievalChannel.SEMANTIC,),
)
SUBMARKET: Final = DecisionClass(
    relationship_type=RelationshipType.SUBMARKET,
    anchor_source="target_title",
    channels=(RetrievalChannel.LEXICAL,),
)


def policy() -> LearningPolicy:
    return load_learning_policy(LEARNING_POLICY_PATH)


def documents() -> tuple[dict[TargetKind, dict[str, Any]], Any]:
    """The live ranking / recommendation / agents documents plus the cross-check context."""
    return _base_documents(make_settings())


def observation(
    *,
    decision_class: DecisionClass = SAME_MARKET,
    sample_count: int = 40,
    approvals: int = 20,
    edits: int = 10,
    rejections: int = 15,
    rollbacks: int = 1,
    positive_outcomes: int = 5,
    reasons: dict[str, int] | None = None,
    anchor_edit_char_delta: int = -12,
    sufficient: bool = True,
) -> DecisionClassObservation:
    """One aggregate row. Rates are derived here exactly as ``aggregate.py`` derives them."""
    histogram = (
        reasons
        if reasons is not None
        else {
            "irrelevant_destination": 7,
            "reader_journey_mismatch": 4,
            "over_linking": 3,
            "duplicate_of_existing_link": 2,
        }
    )

    def rate(value: int) -> float | None:
        return None if sample_count == 0 else value / sample_count

    return DecisionClassObservation(
        decision_class=decision_class.canonical,
        sample_count=sample_count,
        approvals=approvals,
        edits=edits,
        rejections=rejections,
        rollbacks=rollbacks,
        positive_outcomes=positive_outcomes,
        approval_rate=rate(approvals),
        edit_rate=rate(edits),
        reject_rate=rate(rejections),
        rollback_rate=rate(rollbacks),
        positive_outcome_rate=rate(positive_outcomes),
        reason_histogram=histogram,
        anchor_edit_char_delta=anchor_edit_char_delta,
        sufficient=sufficient,
    )


def report(*observations: DecisionClassObservation) -> AggregateReport:
    rows = tuple(
        ClassAggregate(
            decision_class=DecisionClass.parse(item.decision_class), observation=item, plans=()
        )
        for item in observations
    )
    return AggregateReport(
        window=WINDOW,
        rows=rows,
        sample_count_total=sum(item.sample_count for item in observations),
        classes_below_floor=sum(1 for item in observations if not item.sufficient),
        plans_skipped_no_channel=0,
        search_impressions=0,
        search_clicks=0,
        engine_version="deterministic-engine-1",
        analysis_version="tax1-alg1",
        policy_versions={"ranking": "1", "recommendation": "1", "agents": "1"},
    )
