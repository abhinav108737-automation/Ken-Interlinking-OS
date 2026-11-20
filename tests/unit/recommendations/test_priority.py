"""Governed priority designations and shadow reporting (Business Phase 2A-1).

The thirteen cases owner decision 4 (2026-11-19) requires, plus the LOAD-BEARING test: a
designation cannot make a gate-failing pair survive. That one is asserted against the REAL
gates in :mod:`ken_services.recommendations.gates`, not against a stub and not by observing an
absence, because "priority never overrides relevance" is the whole premise of the slice.

These fixtures build their own pairs and do not touch the synthetic evaluation corpus, which
is inert under ADR-0027 (NEXT_STEPS item 34). Recording expectations against that corpus would
write empty ones -- the defect item 34 refused to commit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ken_contracts.intelligence import IneligibilityReason, LinkEligibility, PageAnalysis
from ken_contracts.inventory import CanonicalStatus, PublicationState
from ken_contracts.recommendations import CandidateRejection
from ken_core.priority import MAX_TIER, MIN_TIER, PriorityType, tier_weight
from ken_services.intelligence.eligibility import EligibilityState, compute_eligibility
from ken_services.intelligence.taxonomy import taxonomy_index
from ken_services.recommendations.gates import geography_gate, market_gate
from ken_services.recommendations.priority import (
    SHADOW_PRIORITY_WEIGHT,
    PriorityDesignation,
    designated_value,
    effective_designations,
)
from ken_services.recommendations.priority_shadow import (
    CAPACITY_REJECTIONS,
    SHADOW_FORMULA,
    ShadowCandidate,
    shadow_report,
)
from tests.unit.recommendations._support import analysis, taxonomy

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
NO_MENTION = "A paragraph about the market with no place name in it at all."


def _designation(
    *,
    page_id: uuid.UUID,
    priority_type: PriorityType = PriorityType.HIGH_PRIORITY_REPORT,
    tier: int = 1,
    effective_from: datetime = NOW - DAY,
    effective_to: datetime | None = None,
    retired_at: datetime | None = None,
    designation_id: uuid.UUID | None = None,
    created_at: datetime = NOW - DAY,
    updated_at: datetime = NOW - DAY,
) -> PriorityDesignation:
    return PriorityDesignation(
        id=designation_id or uuid.uuid4(),
        page_id=page_id,
        priority_type=priority_type,
        tier=tier,
        reason="owner designated this report a commercial priority",
        owner="abhinav.kumar",
        effective_from=effective_from,
        effective_to=effective_to,
        retired_at=retired_at,
        created_at=created_at,
        created_by="abhinav.kumar",
        updated_at=updated_at,
        updated_by="abhinav.kumar",
    )


def _eligible() -> LinkEligibility:
    return LinkEligibility(as_source=True, as_target=True, source_reasons=[], target_reasons=[])


def _state(**overrides: object) -> EligibilityState:
    base: dict[str, object] = {
        "publication_state": PublicationState.PUBLISHED,
        "gone": False,
        "protected": False,
        "robots_directives": (),
        "canonical_status": CanonicalStatus.SELF,
        "http_status": 200,
        "word_count": 2000,
        "page_type": "report",
        "has_analysis": True,
        "analysis_fresh": True,
    }
    base.update(overrides)
    return EligibilityState(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# THE LOAD-BEARING TESTS (owner decision 1)
# --------------------------------------------------------------------------- #
def _post_gate_candidates(
    source: PageAnalysis,
    targets: dict[uuid.UUID, PageAnalysis],
) -> list[ShadowCandidate]:
    """The pipeline's order of operations, reduced to what matters here: GATES FIRST.

    A target becomes a candidate only if BOTH real gates admit the pair. Designations are not
    an input to this function at all -- which is the property under test: there is no
    argument through which priority could reach the admission decision.
    """
    source_id = uuid.uuid4()
    admitted: list[ShadowCandidate] = []
    for target_id, target in sorted(targets.items(), key=lambda kv: str(kv[0])):
        if geography_gate(source, target, taxonomy=taxonomy(), source_text=NO_MENTION) is None:
            continue
        if market_gate(source, target, taxonomy=taxonomy()) == ():
            continue
        admitted.append(
            ShadowCandidate(
                source_page_id=source_id,
                target_page_id=target_id,
                target_url=f"/{target_id}",
                live_score=0.5,
                live_selected=True,
            )
        )
    return admitted


def test_a_designation_cannot_make_a_geography_gate_failure_survive() -> None:
    """India -> Germany is refused (different regions) however strongly Germany is designated,
    while India -> India, the positive control, survives. Without the control this test would
    pass vacuously if the gate refused everything."""
    source = analysis(geographies=["india"], market_id="solar-power")
    refused_id, control_id = uuid.UUID(int=101), uuid.UUID(int=102)
    targets = {
        refused_id: analysis(geographies=["germany"], market_id="solar-power"),
        control_id: analysis(geographies=["india"], market_id="solar-power"),
    }
    designations = {
        refused_id: [_designation(page_id=refused_id, tier=MIN_TIER)],
        control_id: [_designation(page_id=control_id, tier=MAX_TIER)],
    }

    candidates = _post_gate_candidates(source, targets)
    report = shadow_report(
        candidates,
        designations=designations,
        eligibility={refused_id: _eligible(), control_id: _eligible()},
        at=NOW,
    )

    reported = {row.target_page_id for row in report.rows}
    assert control_id in reported, "positive control: a same-geography pair must survive"
    assert refused_id not in reported, "a tier-1 designation must not admit a refused pair"
    # The designation was real and maximal -- it simply had no way in.
    assert (
        designated_value(
            designations[refused_id], page_id=refused_id, at=NOW, eligibility=_eligible()
        ).value
        == 1.0
    )


def test_a_designation_cannot_make_a_market_gate_failure_survive() -> None:
    """Solar power -> pharmaceuticals is refused (no declared relation) however strongly the
    target is designated; solar power -> solar power, the control, survives."""
    source = analysis(market_id="solar-power", geographies=["india"])
    refused_id, control_id = uuid.UUID(int=201), uuid.UUID(int=202)
    targets = {
        refused_id: analysis(market_id="pharmaceuticals", geographies=["india"]),
        control_id: analysis(market_id="solar-power", geographies=["india"]),
    }
    designations = {
        refused_id: [_designation(page_id=refused_id, tier=MIN_TIER)],
        control_id: [_designation(page_id=control_id, tier=MAX_TIER)],
    }

    candidates = _post_gate_candidates(source, targets)
    report = shadow_report(
        candidates,
        designations=designations,
        eligibility={refused_id: _eligible(), control_id: _eligible()},
        at=NOW,
    )

    reported = {row.target_page_id for row in report.rows}
    assert control_id in reported, "positive control: a same-market pair must survive"
    assert refused_id not in reported, "a tier-1 designation must not admit a refused pair"


def test_the_gate_markets_and_geographies_used_above_exist() -> None:
    """Guards the two tests above against passing vacuously: an UNKNOWN market or geography is
    also refused, so a typo would make them green while testing nothing. (This happened while
    writing them: a first draft used a market absent from taxonomy v1.)"""
    index = taxonomy_index(taxonomy())
    for market in ("solar-power", "pharmaceuticals"):
        assert index.has_market(market), market
    geographies = {g.id for g in taxonomy().geographies}
    assert {"india", "germany"} <= geographies


def test_page_family_failure_is_refused_by_eligibility_whatever_the_designation() -> None:
    """A page type with ``linkable_as_target: false`` earns nothing (owner decision 4)."""
    # 'corporate' carries linkable_as_target: false in taxonomy v3 and v1.
    verdict = compute_eligibility(_state(page_type="corporate"), taxonomy=taxonomy())
    assert not verdict.as_target
    assert IneligibilityReason.PAGE_TYPE_EXCLUDED in verdict.target_reasons

    page_id = uuid.uuid4()
    value = designated_value(
        [_designation(page_id=page_id, tier=MIN_TIER)],
        page_id=page_id,
        at=NOW,
        eligibility=verdict,
    )
    assert value.value == 0.0
    assert value.contribution == 0.0
    assert "page_type_excluded" in value.ineligible_reasons


# --------------------------------------------------------------------------- #
# The time window: active, expired, inactive, future-dated
# --------------------------------------------------------------------------- #
def test_an_active_in_window_designation_resolves() -> None:
    page_id = uuid.uuid4()
    value = designated_value(
        [_designation(page_id=page_id, tier=2)],
        page_id=page_id,
        at=NOW,
        eligibility=_eligible(),
    )
    assert value.applies
    assert value.tier == 2
    assert value.value == pytest.approx(0.8)
    assert value.contribution == pytest.approx(0.8 * SHADOW_PRIORITY_WEIGHT)
    assert value.types == (PriorityType.HIGH_PRIORITY_REPORT,)


def test_an_expired_designation_does_not_resolve() -> None:
    page_id = uuid.uuid4()
    expired = _designation(page_id=page_id, effective_from=NOW - 10 * DAY, effective_to=NOW - DAY)
    value = designated_value([expired], page_id=page_id, at=NOW, eligibility=_eligible())
    assert not value.applies
    assert value.types == ()
    assert value.ineligible_reasons == ()


def test_a_retired_designation_does_not_resolve_after_its_retirement() -> None:
    page_id = uuid.uuid4()
    value = designated_value(
        [_designation(page_id=page_id, retired_at=NOW - timedelta(hours=1))],
        page_id=page_id,
        at=NOW,
        eligibility=_eligible(),
    )
    assert not value.applies


def test_a_retired_designation_still_resolves_for_a_date_when_it_was_in_force() -> None:
    """DEFECT 1 regression. Retirement must truncate history, never erase it.

    The previous model stored a boolean ``active``, which answered "is it live now" and so
    gave every historical question today's answer: a designation retired today could not be
    reproduced for a date on which it was genuinely in force, making a past shadow report
    wrong. This asserts both sides of the retirement instant.
    """
    page_id = uuid.uuid4()
    retired = _designation(page_id=page_id, effective_from=NOW - 10 * DAY, retired_at=NOW - 2 * DAY)

    before = designated_value([retired], page_id=page_id, at=NOW - 5 * DAY, eligibility=_eligible())
    at_instant = designated_value(
        [retired], page_id=page_id, at=NOW - 2 * DAY, eligibility=_eligible()
    )
    after = designated_value([retired], page_id=page_id, at=NOW, eligibility=_eligible())

    assert before.applies, "it was in force five days ago and must still resolve then"
    assert before.tier == retired.tier
    assert not at_instant.applies, "the interval is half-open: [from, retired_at)"
    assert not after.applies


def test_a_future_dated_designation_does_not_resolve_yet() -> None:
    page_id = uuid.uuid4()
    future = _designation(page_id=page_id, effective_from=NOW + DAY)
    assert not designated_value([future], page_id=page_id, at=NOW, eligibility=_eligible()).applies
    # ...and does resolve once its window opens.
    assert designated_value(
        [future], page_id=page_id, at=NOW + 2 * DAY, eligibility=_eligible()
    ).applies


def test_retiring_at_or_before_the_start_means_it_was_never_in_force() -> None:
    page_id = uuid.uuid4()
    cancelled = _designation(page_id=page_id, effective_from=NOW, retired_at=NOW - DAY)
    at_start = _designation(page_id=page_id, effective_from=NOW, retired_at=NOW)

    assert cancelled.is_empty
    assert at_start.is_empty
    for designation in (cancelled, at_start):
        for moment in (NOW - 2 * DAY, NOW, NOW + DAY):
            assert not designation.is_effective_at(moment)


def test_validity_end_is_the_earlier_of_expiry_and_retirement() -> None:
    page_id = uuid.uuid4()
    expires_first = _designation(page_id=page_id, effective_to=NOW + DAY, retired_at=NOW + 5 * DAY)
    retired_first = _designation(page_id=page_id, effective_to=NOW + 5 * DAY, retired_at=NOW + DAY)
    open_ended = _designation(page_id=page_id)

    assert expires_first.validity_end == NOW + DAY
    assert retired_first.validity_end == NOW + DAY
    assert open_ended.validity_end is None


def test_a_scheduled_future_retirement_is_still_in_force_until_it_arrives() -> None:
    page_id = uuid.uuid4()
    scheduled = _designation(page_id=page_id, retired_at=NOW + 7 * DAY)

    assert scheduled.is_retired, "the retirement is recorded"
    assert scheduled.is_effective_at(NOW), "...but it has not happened yet"
    assert not scheduled.is_effective_at(NOW + 7 * DAY)


@pytest.mark.parametrize(
    "field",
    ["effective_from", "effective_to", "retired_at", "created_at", "updated_at"],
)
def test_a_timezone_naive_instant_is_refused(field: str) -> None:
    naive: Any = datetime(2026, 11, 19, 12, 0)
    overrides: dict[str, Any] = {field: naive}
    with pytest.raises(ValueError, match=field):
        _designation(page_id=uuid.uuid4(), **overrides)


def test_asking_whether_a_naive_instant_is_covered_is_refused() -> None:
    with pytest.raises(ValueError, match="at"):
        _designation(page_id=uuid.uuid4()).is_effective_at(datetime(2026, 11, 19, 12, 0))


def test_the_window_is_half_open_so_a_replacement_starts_with_no_overlap() -> None:
    page_id = uuid.uuid4()
    boundary = NOW + DAY
    ending = _designation(page_id=page_id, effective_from=NOW - DAY, effective_to=boundary)
    starting = _designation(page_id=page_id, effective_from=boundary, tier=3)

    at_boundary = effective_designations([ending, starting], at=boundary)

    assert at_boundary == (starting,), "the expiry instant belongs to the successor alone"


# --------------------------------------------------------------------------- #
# Protected, gone, stale (owner decision 4)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"protected": True}, IneligibilityReason.PROTECTED),
        ({"gone": True}, IneligibilityReason.GONE),
        ({"analysis_fresh": False}, IneligibilityReason.STALE_ANALYSIS),
        ({"canonical_status": CanonicalStatus.DECLARED}, IneligibilityReason.NOT_CANONICAL),
        ({"publication_state": PublicationState.DRAFT}, IneligibilityReason.NOT_PUBLISHED),
        ({"robots_directives": ("noindex",)}, IneligibilityReason.NOINDEX),
        ({"http_status": 404}, IneligibilityReason.HTTP_NOT_OK),
    ],
)
def test_an_ineligible_page_earns_no_priority_and_says_why(
    overrides: dict[str, object], reason: IneligibilityReason
) -> None:
    verdict = compute_eligibility(_state(**overrides), taxonomy=taxonomy())
    assert reason in verdict.target_reasons

    page_id = uuid.uuid4()
    value = designated_value(
        [_designation(page_id=page_id, tier=MIN_TIER)],
        page_id=page_id,
        at=NOW,
        eligibility=verdict,
    )

    assert value.value == 0.0
    assert value.contribution == 0.0
    assert str(reason) in value.ineligible_reasons, "a refusal must name its rule"


# --------------------------------------------------------------------------- #
# Ties and overlapping designations
# --------------------------------------------------------------------------- #
def test_ties_resolve_deterministically_by_type_then_id() -> None:
    page_id = uuid.uuid4()
    low_id = uuid.UUID(int=1)
    high_id = uuid.UUID(int=2)
    a = _designation(
        page_id=page_id,
        tier=2,
        priority_type=PriorityType.SURVEY_PRIORITY,
        designation_id=high_id,
    )
    b = _designation(
        page_id=page_id,
        tier=2,
        priority_type=PriorityType.CONSULTING_PRIORITY,
        designation_id=low_id,
    )

    forward = effective_designations([a, b], at=NOW)
    reversed_order = effective_designations([b, a], at=NOW)

    assert forward == reversed_order, "input order must not decide the winner"
    assert forward[0].priority_type is PriorityType.CONSULTING_PRIORITY


def test_overlapping_designations_take_the_strongest_and_report_every_type() -> None:
    page_id = uuid.uuid4()
    weak = _designation(page_id=page_id, tier=4, priority_type=PriorityType.TRENDING_MARKET)
    strong = _designation(page_id=page_id, tier=1, priority_type=PriorityType.HIGH_REVENUE_REPORT)

    value = designated_value([weak, strong], page_id=page_id, at=NOW, eligibility=_eligible())

    assert value.tier == 1, "the strongest designation sets the value"
    assert value.value == 1.0
    assert set(value.types) == {
        PriorityType.HIGH_REVENUE_REPORT,
        PriorityType.TRENDING_MARKET,
    }, "every effective type is reported, for explainability"


def test_a_conflicting_expired_designation_is_ignored_in_favour_of_the_live_one() -> None:
    page_id = uuid.uuid4()
    expired_strong = _designation(
        page_id=page_id, tier=1, effective_from=NOW - 10 * DAY, effective_to=NOW - DAY
    )
    active_weak = _designation(page_id=page_id, tier=5, priority_type=PriorityType.SURVEY_PRIORITY)

    value = designated_value(
        [expired_strong, active_weak], page_id=page_id, at=NOW, eligibility=_eligible()
    )

    assert value.tier == 5, "an expired tier-1 must not outrank a live tier-5"
    assert value.types == (PriorityType.SURVEY_PRIORITY,)


# --------------------------------------------------------------------------- #
# Tier arithmetic
# --------------------------------------------------------------------------- #
def test_tier_weight_is_linear_and_bounded() -> None:
    assert tier_weight(MIN_TIER) == 1.0
    assert tier_weight(MAX_TIER) == pytest.approx(0.2)
    weights = [tier_weight(t) for t in range(MIN_TIER, MAX_TIER + 1)]
    assert weights == sorted(weights, reverse=True), "a stronger tier is never worth less"


@pytest.mark.parametrize("tier", [0, MAX_TIER + 1, -1])
def test_a_tier_outside_the_range_is_refused(tier: int) -> None:
    with pytest.raises(ValueError, match="tier"):
        tier_weight(tier)
    with pytest.raises(ValueError, match="tier"):
        _designation(page_id=uuid.uuid4(), tier=tier)


@pytest.mark.parametrize(
    ("field", "value"),
    [("reason", "   "), ("owner", "")],
)
def test_a_designation_without_a_reason_or_owner_is_refused(field: str, value: str) -> None:
    kwargs: dict[str, object] = {
        "id": uuid.uuid4(),
        "page_id": uuid.uuid4(),
        "priority_type": PriorityType.HIGH_PRIORITY_REPORT,
        "tier": 1,
        "reason": "a reason",
        "owner": "an owner",
        "effective_from": NOW,
        "effective_to": None,
        "retired_at": None,
        "created_at": NOW,
        "created_by": "x",
        "updated_at": NOW,
        "updated_by": "x",
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match=field):
        PriorityDesignation(**kwargs)  # type: ignore[arg-type]


def test_an_inverted_window_is_refused() -> None:
    with pytest.raises(ValueError, match="effective_to"):
        _designation(page_id=uuid.uuid4(), effective_from=NOW, effective_to=NOW - DAY)


# --------------------------------------------------------------------------- #
# Shadow reporting: changes ranking only, and only in shadow
# --------------------------------------------------------------------------- #
def _candidates() -> tuple[ShadowCandidate, ShadowCandidate]:
    source = uuid.uuid4()
    leader = ShadowCandidate(
        source_page_id=source,
        target_page_id=uuid.UUID(int=10),
        target_url="/a-leader",
        live_score=0.60,
        live_selected=True,
    )
    challenger = ShadowCandidate(
        source_page_id=source,
        target_page_id=uuid.UUID(int=11),
        target_url="/b-challenger",
        live_score=0.55,
        live_selected=False,
    )
    return leader, challenger


def test_priority_reorders_an_eligible_pair_in_shadow_only() -> None:
    leader, challenger = _candidates()
    eligibility = {leader.target_page_id: _eligible(), challenger.target_page_id: _eligible()}
    designations = {challenger.target_page_id: [_designation(page_id=challenger.target_page_id)]}

    report = shadow_report(
        [leader, challenger], designations=designations, eligibility=eligibility, at=NOW
    )

    by_target = {r.target_page_id: r for r in report.rows}
    challenger_row = by_target[challenger.target_page_id]
    leader_row = by_target[leader.target_page_id]

    # Live order is unchanged -- the live score is untouched.
    assert leader_row.live_score == 0.60
    assert challenger_row.live_score == 0.55
    assert leader_row.live_rank == 1
    assert challenger_row.live_rank == 2

    # In shadow the designated challenger overtakes: 0.55 + 0.1 = 0.65 > 0.60.
    assert challenger_row.shadow_rank == 1
    assert challenger_row.delta_rank == 1
    assert challenger_row.moved
    assert leader_row.delta_rank == -1
    assert report.moved


def test_a_designation_too_weak_to_overtake_changes_no_rank() -> None:
    leader, challenger = _candidates()
    eligibility = {leader.target_page_id: _eligible(), challenger.target_page_id: _eligible()}
    # Tier 5 is worth 0.2 -> contribution 0.02, short of the 0.05 gap.
    designations = {
        challenger.target_page_id: [_designation(page_id=challenger.target_page_id, tier=5)]
    }

    report = shadow_report(
        [leader, challenger], designations=designations, eligibility=eligibility, at=NOW
    )

    assert report.moved == (), "priority must not promote what it cannot outscore"
    assert report.designated, "the designation still resolved and is reported"


def test_an_ineligible_target_is_reported_as_refused_not_as_undesignated() -> None:
    leader, challenger = _candidates()
    protected = compute_eligibility(_state(protected=True), taxonomy=taxonomy())
    eligibility = {leader.target_page_id: _eligible(), challenger.target_page_id: protected}
    designations = {challenger.target_page_id: [_designation(page_id=challenger.target_page_id)]}

    report = shadow_report(
        [leader, challenger], designations=designations, eligibility=eligibility, at=NOW
    )

    refused = report.refused
    assert len(refused) == 1
    assert refused[0].target_page_id == challenger.target_page_id
    assert "protected" in refused[0].ineligible_reasons
    assert report.moved == ()


def test_a_target_with_no_eligibility_verdict_is_treated_as_ineligible() -> None:
    """Silence is not a verdict: an unknown target earns nothing."""
    leader, challenger = _candidates()
    designations = {challenger.target_page_id: [_designation(page_id=challenger.target_page_id)]}

    report = shadow_report(
        [leader, challenger],
        designations=designations,
        eligibility={},  # nothing known about either target
        at=NOW,
    )

    assert report.designated == ()
    assert report.moved == ()


def test_the_report_states_the_weight_and_instant_it_assumed() -> None:
    leader, challenger = _candidates()
    report = shadow_report(
        [leader, challenger],
        designations={},
        eligibility={leader.target_page_id: _eligible(), challenger.target_page_id: _eligible()},
        at=NOW,
    )

    assert report.assumed_weight == SHADOW_PRIORITY_WEIGHT
    assert report.at == NOW
    assert all(row.assumed_weight == SHADOW_PRIORITY_WEIGHT for row in report.rows)
    assert "assumed weight" in report.summary()


def test_the_shadow_report_is_deterministic_across_repeated_runs() -> None:
    leader, challenger = _candidates()
    eligibility = {leader.target_page_id: _eligible(), challenger.target_page_id: _eligible()}
    designations = {
        challenger.target_page_id: [
            _designation(
                page_id=challenger.target_page_id,
                tier=2,
                priority_type=PriorityType.SURVEY_PRIORITY,
                designation_id=uuid.UUID(int=1),
            ),
            _designation(
                page_id=challenger.target_page_id,
                tier=2,
                priority_type=PriorityType.TRENDING_MARKET,
                designation_id=uuid.UUID(int=2),
            ),
        ]
    }

    first = shadow_report(
        [leader, challenger], designations=designations, eligibility=eligibility, at=NOW
    )
    # Same inputs, reversed candidate order and reversed designation order.
    second = shadow_report(
        [challenger, leader],
        designations={
            challenger.target_page_id: list(reversed(designations[challenger.target_page_id]))
        },
        eligibility=eligibility,
        at=NOW,
    )

    assert first.rows == second.rows, "row order and content must not depend on input order"


def test_priority_share_measures_priority_against_the_live_score() -> None:
    """Evidence for the ADR's "priority must not dominate relevance" limit."""
    _, challenger = _candidates()
    report = shadow_report(
        [challenger],
        designations={challenger.target_page_id: [_designation(page_id=challenger.target_page_id)]},
        eligibility={challenger.target_page_id: _eligible()},
        at=NOW,
    )

    row = report.rows[0]
    assert row.shadow_score == pytest.approx(0.65)
    assert row.priority_share == pytest.approx(0.1 / 0.65, rel=1e-6)
    assert row.priority_share < 0.5, "priority is a minority of the score at the §5.5 weight"


def test_the_shadow_score_is_clamped_to_one() -> None:
    source = uuid.uuid4()
    already_maximal = ShadowCandidate(
        source_page_id=source,
        target_page_id=uuid.UUID(int=12),
        target_url="/maximal",
        live_score=1.0,
        live_selected=True,
    )
    report = shadow_report(
        [already_maximal],
        designations={
            already_maximal.target_page_id: [_designation(page_id=already_maximal.target_page_id)]
        },
        eligibility={already_maximal.target_page_id: _eligible()},
        at=NOW,
    )

    assert report.rows[0].shadow_score == 1.0


def test_a_candidate_score_outside_the_unit_range_is_refused() -> None:
    with pytest.raises(ValueError, match="live_score"):
        ShadowCandidate(
            source_page_id=uuid.uuid4(),
            target_page_id=uuid.uuid4(),
            target_url="/x",
            live_score=1.5,
            live_selected=True,
        )


# --------------------------------------------------------------------------- #
# Which candidates may enter a shadow report, per source, and plan-set swaps
# --------------------------------------------------------------------------- #
def test_only_capacity_cuts_may_join_survivors_in_the_shadow_population() -> None:
    """Priority may change WHICH qualified candidate wins a slot, never admit one below a
    floor or past a safety guard (owner decision 1)."""
    assert (
        frozenset({CandidateRejection.PER_SOURCE_LIMIT, CandidateRejection.DIVERSITY_LIMIT})
        == CAPACITY_REJECTIONS
    )
    for never in (
        CandidateRejection.BELOW_MIN_RELEVANCE,  # relevance floor
        CandidateRejection.BELOW_MIN_SCORE,  # score floor
        CandidateRejection.LINK_DENSITY_EXCEEDED,  # over-linking SAFETY guard
        CandidateRejection.GEOGRAPHY_MISMATCH,
        CandidateRejection.MARKET_UNRELATED,
        CandidateRejection.PAGE_TYPE_INCOMPATIBLE,
        CandidateRejection.TARGET_PROTECTED,
        CandidateRejection.TARGET_INELIGIBLE,
        CandidateRejection.NO_NATURAL_ANCHOR,
        CandidateRejection.NO_MARKET_KEYWORD_ANCHOR,
    ):
        assert never not in CAPACITY_REJECTIONS, never


def test_priority_would_swap_a_capacity_cut_candidate_into_the_plan_set() -> None:
    """One slot (the leader took it live). A designated challenger would take it in shadow."""
    leader, challenger = _candidates()
    eligibility = {leader.target_page_id: _eligible(), challenger.target_page_id: _eligible()}
    designations = {challenger.target_page_id: [_designation(page_id=challenger.target_page_id)]}

    report = shadow_report(
        [leader, challenger], designations=designations, eligibility=eligibility, at=NOW
    )
    by_target = {r.target_page_id: r for r in report.rows}

    assert by_target[challenger.target_page_id].shadow_selected
    assert not by_target[leader.target_page_id].shadow_selected
    assert {r.target_page_id for r in report.selection_changes} == {
        leader.target_page_id,
        challenger.target_page_id,
    }
    # The live outcome is recorded unchanged.
    assert by_target[leader.target_page_id].live_selected
    assert not by_target[challenger.target_page_id].live_selected


def test_the_plan_set_size_per_source_is_preserved_in_shadow() -> None:
    leader, challenger = _candidates()
    report = shadow_report(
        [leader, challenger],
        designations={challenger.target_page_id: [_designation(page_id=challenger.target_page_id)]},
        eligibility={leader.target_page_id: _eligible(), challenger.target_page_id: _eligible()},
        at=NOW,
    )
    assert sum(r.live_selected for r in report.rows) == sum(r.shadow_selected for r in report.rows)


def test_ranks_are_computed_within_each_source_not_across_sources() -> None:
    """max_plans_per_source is per source, so a candidate competes only with its siblings."""
    first, second = uuid.UUID(int=1), uuid.UUID(int=2)
    a = ShadowCandidate(
        source_page_id=first,
        target_page_id=uuid.UUID(int=20),
        target_url="/a",
        live_score=0.9,
        live_selected=True,
    )
    b = ShadowCandidate(
        source_page_id=second,
        target_page_id=uuid.UUID(int=21),
        target_url="/b",
        live_score=0.4,
        live_selected=True,
    )
    report = shadow_report(
        [a, b],
        designations={},
        eligibility={a.target_page_id: _eligible(), b.target_page_id: _eligible()},
        at=NOW,
    )
    assert {r.target_page_id: r.live_rank for r in report.rows} == {
        a.target_page_id: 1,
        b.target_page_id: 1,
    }, "each source's best candidate is rank 1 in its own source"


def test_the_report_states_its_formula() -> None:
    leader, challenger = _candidates()
    report = shadow_report(
        [leader, challenger],
        designations={},
        eligibility={leader.target_page_id: _eligible(), challenger.target_page_id: _eligible()},
        at=NOW,
    )
    assert report.formula == SHADOW_FORMULA
    assert SHADOW_FORMULA in report.summary()
