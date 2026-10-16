"""Deterministic option enumeration and the candidate boundary (ADR-0016 15.2c / 15.7.5).

What these tests are really asserting: **no number a proposal stores was chosen by a model**.
Options come from the aggregate table through pure functions, so the same table always gives the
same options in the same order with the same fingerprint, and every refusal the frozen contract
owes us is produced HERE, with a code, before any model is consulted.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ken_contracts.learning import (
    ALLOWED_CHANGE_PATHS,
    CROSS_CHECKED_PATHS,
    FROZEN_POLICY_PATHS,
    CrossPolicyContext,
    DecisionClassObservation,
    PolicyChange,
    ProposalDraft,
    ProposalOption,
    TargetKind,
    is_frozen_path,
)
from ken_contracts.recommendations import RankingPolicy
from ken_services.learning.prompt import KEEP_CURRENT
from ken_services.learning.proposals import (
    AGENTS_PER_RUN_PATH,
    RECOMMENDATION_PER_SOURCE_PATH,
    CandidateRefusal,
    CandidateRefusedError,
    apply_changes,
    policy_change,
    prepare_policy_proposal,
)
from ken_services.learning.reflect import enumerate_options
from tests.unit.learning.reflection_support import (
    SUBMARKET,
    documents,
    observation,
    policy,
)

DOCUMENTS, CROSS = documents()
POLICY = policy()


def _options(
    target_kind: TargetKind, *observations: DecisionClassObservation
) -> tuple[ProposalOption, ...]:
    options, _specs = enumerate_options(
        target_kind=target_kind,
        base_document=DOCUMENTS[target_kind],
        observations=list(observations),
        policy=POLICY,
        cross_check=CROSS,
    )
    return options


# --------------------------------------------------------------------------- #
# Shape of the option set
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("target_kind", list(TargetKind))
def test_the_no_change_option_is_always_last_and_indices_are_dense(
    target_kind: TargetKind,
) -> None:
    """ADR-0016 15.7.7 persists the option set, and ``ProposalEvidence`` requires indices
    ``0..n-1`` in order. ``keep_current`` is LAST because option 0 is the DETERMINISTIC rule's
    own preference: a disabled model must fall back to deterministic code, not to inaction."""
    options = _options(target_kind, observation())
    assert [option.index for option in options] == list(range(len(options)))
    assert options[-1].label == KEEP_CURRENT
    assert options[-1].changes == ()
    assert all(option.changes for option in options[:-1])


@pytest.mark.parametrize("target_kind", list(TargetKind))
def test_a_corpus_below_the_sample_floor_offers_nothing_but_keep_current(
    target_kind: TargetKind,
) -> None:
    """THE honesty property of this phase. 65 synthetic pages cannot clear 30 samples per
    decision class, so reflection must produce one option, consult no model and propose
    nothing -- ``inconclusive``, never a confident proposal (ADR-0016 15.4 / 16.1)."""
    below = observation(sample_count=12, approvals=6, rejections=4, edits=3, sufficient=False)
    options = _options(target_kind, below)
    assert len(options) == 1
    assert options[0].label == KEEP_CURRENT


def test_only_sufficient_classes_contribute_pressure() -> None:
    """An insufficient class's reason codes must not conjure an option, even beside a
    sufficient one: the floor is per class, so a class below it carries no weight at all."""
    quiet = observation(
        sample_count=40,
        approvals=40,
        rejections=0,
        edits=0,
        rollbacks=0,
        reasons={"good_as_is": 40},
        anchor_edit_char_delta=0,
    )
    noisy_but_small = observation(
        decision_class=SUBMARKET,
        sample_count=4,
        approvals=0,
        rejections=4,
        edits=4,
        rollbacks=0,
        positive_outcomes=0,
        reasons={"irrelevant_destination": 9, "over_linking": 9},
        sufficient=False,
    )
    assert len(_options(TargetKind.RANKING, quiet, noisy_but_small)) == 1


def test_enumeration_is_deterministic() -> None:
    first = _options(TargetKind.RANKING, observation())
    second = _options(TargetKind.RANKING, observation())
    assert [(o.index, o.label, o.changes) for o in first] == [
        (o.index, o.label, o.changes) for o in second
    ]


def test_options_are_ordered_by_a_comparable_strength() -> None:
    """Destination pressure dominates, so the taxonomy shift must be option 0 -- the one a
    fallback would take. Ordering by raw counts against rates would make that arbitrary."""
    destination_heavy = observation(
        reasons={"irrelevant_destination": 20, "reader_journey_mismatch": 1}
    )
    options = _options(TargetKind.RANKING, destination_heavy)
    assert options[0].label == "shift_weight_to_taxonomy"
    assert options[1].label == "shift_weight_to_intent"


# --------------------------------------------------------------------------- #
# The candidate documents the options produce
# --------------------------------------------------------------------------- #
def test_a_ranking_candidate_still_loads_as_a_ranking_policy() -> None:
    """A shift that broke "weights sum to 1" would be a candidate that cannot LOAD. Rounding
    both ends of the shift independently drifts by up to 2e-6 against the policy's own 1e-6
    tolerance, which is why the donor is computed as ``1 - sum(everything else)``."""
    options = _options(TargetKind.RANKING, observation())
    prepared = prepare_policy_proposal(
        target_kind=TargetKind.RANKING,
        base_document=DOCUMENTS[TargetKind.RANKING],
        changes=options[0].changes,
    )
    candidate = RankingPolicy.model_validate(prepared.candidate_document)
    assert abs(sum(candidate.weights.values()) - 1.0) <= 1e-9
    assert candidate.version == int(prepared.base_version) + 1
    assert (
        prepared.candidate_document["weights"]["taxonomy"]
        > (DOCUMENTS[TargetKind.RANKING]["weights"]["taxonomy"])
    )


def test_the_candidate_document_is_stored_whole_so_an_offline_test_needs_no_file() -> None:
    """ADR-0016 section 5: ``policy_candidates.document`` holds the FULL validated document."""
    options = _options(TargetKind.RECOMMENDATION, observation())
    prepared = prepare_policy_proposal(
        target_kind=TargetKind.RECOMMENDATION,
        base_document=DOCUMENTS[TargetKind.RECOMMENDATION],
        changes=options[0].changes,
    )
    document = prepared.candidate_document
    assert set(document) >= {"policy", "version", "selection", "retrieval", "placement"}
    assert len(prepared.candidate_sha256) == 64


def test_only_the_named_paths_and_the_version_move() -> None:
    base = DOCUMENTS[TargetKind.RECOMMENDATION]
    options = _options(TargetKind.RECOMMENDATION, observation())
    prepared = prepare_policy_proposal(
        target_kind=TargetKind.RECOMMENDATION,
        base_document=base,
        changes=options[0].changes,
    )
    changed = {change.path for change in prepared.body.changes}
    assert changed == {"recommendation.selection.min_score"}
    candidate = prepared.candidate_document
    for key in base:
        if key == "version":
            continue
        if key == "selection":
            moved = {
                field
                for field in base["selection"]
                if base["selection"][field] != candidate["selection"][field]
            }
            assert moved == {"min_score"}
            continue
        assert base[key] == candidate[key], key


# --------------------------------------------------------------------------- #
# Refusals at the boundary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [
        "recommendation.placement.forbidden_anchors",
        "recommendation.selection.rejection_cooldown_days",
        "recommendation.selection.max_internal_links_per_100_words",
        "recommendation.excluded_target_paths",
        "taxonomy.protected_paths",
        "analytics.windows.pre_days",
        "settings.auto_approve",
        "auto_approve",
        "autonomy.level",
        "learning.gates.min_sample_count",
        "model.reasoning",
    ],
)
def test_a_frozen_path_is_refused_at_the_boundary(path: str) -> None:
    """A proposal may NEVER touch these, in this phase or any later one. The refusal is a
    typed code, so a human sees ``frozen_field`` and not a validation traceback."""
    assert is_frozen_path(path) or path not in ALLOWED_CHANGE_PATHS
    with pytest.raises(CandidateRefusedError) as info:
        policy_change(
            path=path,
            base_document=DOCUMENTS[TargetKind.RECOMMENDATION],
            to_value=999,
            target_kind=TargetKind.RECOMMENDATION,
            policy=POLICY,
        )
    assert info.value.refusal in {
        CandidateRefusal.FROZEN_FIELD,
        CandidateRefusal.NOT_ALLOWLISTED,
        CandidateRefusal.WRONG_TARGET,
    }


def test_every_frozen_prefix_is_actually_refused() -> None:
    """Not one entry of ``FROZEN_POLICY_PATHS`` may slip through, nor anything beneath it."""
    for frozen in FROZEN_POLICY_PATHS:
        assert is_frozen_path(frozen)
        assert is_frozen_path(f"{frozen}.anything.deeper")


def test_a_path_the_policy_file_removed_is_refused() -> None:
    """The learning policy may NARROW the allowlist. A path the contract permits but the
    operator took away must be refused exactly as a frozen one is -- otherwise narrowing is
    decoration."""
    narrowed = POLICY.model_copy(
        update={
            "changes": POLICY.changes.model_copy(
                update={
                    "allowed_paths": tuple(
                        path
                        for path in POLICY.changes.allowed_paths
                        if path != "recommendation.selection.min_score"
                    )
                }
            )
        }
    )
    with pytest.raises(CandidateRefusedError) as info:
        policy_change(
            path="recommendation.selection.min_score",
            base_document=DOCUMENTS[TargetKind.RECOMMENDATION],
            to_value=0.4,
            target_kind=TargetKind.RECOMMENDATION,
            policy=narrowed,
        )
    assert info.value.refusal is CandidateRefusal.NOT_ALLOWLISTED


def test_the_cross_check_refuses_a_candidate_that_would_unload_the_agents_policy() -> None:
    """ADR-0012 section 10 cross-checks ``agents.max_plans_per_run <=
    recommendation.selection.max_plans_per_source`` AT LOAD and raises. A candidate that lowers
    the cap below the live per-run value would therefore make the agents policy unloadable, so
    it is refused where it is built and not discovered at the next start-up."""
    assert RECOMMENDATION_PER_SOURCE_PATH in CROSS_CHECKED_PATHS
    change = policy_change(
        path=RECOMMENDATION_PER_SOURCE_PATH,
        base_document=DOCUMENTS[TargetKind.RECOMMENDATION],
        to_value=1,
        target_kind=TargetKind.RECOMMENDATION,
        policy=POLICY,
    )
    with pytest.raises(CandidateRefusedError) as info:
        prepare_policy_proposal(
            target_kind=TargetKind.RECOMMENDATION,
            base_document=DOCUMENTS[TargetKind.RECOMMENDATION],
            changes=(change,),
            cross_check=CrossPolicyContext(
                agents_max_plans_per_run=3, recommendation_max_plans_per_source=3
            ),
        )
    assert info.value.refusal is CandidateRefusal.CROSS_CHECK_VIOLATED


def test_a_cross_checked_change_without_its_context_is_refused() -> None:
    """Omitting the context must not be a way around the cross-check."""
    change = policy_change(
        path=AGENTS_PER_RUN_PATH,
        base_document=DOCUMENTS[TargetKind.AGENTS],
        to_value=2,
        target_kind=TargetKind.AGENTS,
        policy=POLICY,
    )
    with pytest.raises(CandidateRefusedError) as info:
        prepare_policy_proposal(
            target_kind=TargetKind.AGENTS,
            base_document=DOCUMENTS[TargetKind.AGENTS],
            changes=(change,),
            cross_check=None,
        )
    assert info.value.refusal is CandidateRefusal.CROSS_CHECK_VIOLATED


def test_reflection_never_offers_a_candidate_the_cross_check_would_refuse() -> None:
    """With the live policies at ``3 == 3``, lowering ``max_plans_per_source`` is impossible,
    so the option must not be offered at all -- an option a human could never promote is noise
    on the review screen."""
    over_linking = observation(reasons={"over_linking": 9})
    labels = {option.label for option in _options(TargetKind.RECOMMENDATION, over_linking)}
    assert "lower_max_plans_per_source" not in labels


def test_an_unknown_path_is_refused_rather_than_introduced() -> None:
    """A change REPLACES a value; it never introduces a key. A document whose shape has moved
    under the proposal must fail loudly."""
    with pytest.raises(CandidateRefusedError) as info:
        policy_change(
            path="recommendation.selection.min_score",
            base_document={"version": 1, "selection": {}},
            to_value=0.5,
            target_kind=TargetKind.RECOMMENDATION,
            policy=POLICY,
        )
    assert info.value.refusal is CandidateRefusal.UNKNOWN_PATH


def test_a_change_that_changes_nothing_is_refused() -> None:
    live = DOCUMENTS[TargetKind.RECOMMENDATION]["selection"]["min_score"]
    with pytest.raises(CandidateRefusedError) as info:
        policy_change(
            path="recommendation.selection.min_score",
            base_document=DOCUMENTS[TargetKind.RECOMMENDATION],
            to_value=live,
            target_kind=TargetKind.RECOMMENDATION,
            policy=POLICY,
        )
    assert info.value.refusal is CandidateRefusal.NO_CHANGE


def test_a_stale_base_value_is_refused_when_the_candidate_is_applied() -> None:
    """``from_value`` is part of the change, so applying it to a document that has since moved
    must refuse rather than silently overwrite a value nobody reviewed."""
    change = PolicyChange(path="recommendation.selection.min_score", from_value=0.01, to_value=0.5)
    with pytest.raises(CandidateRefusedError):
        apply_changes(
            DOCUMENTS[TargetKind.RECOMMENDATION],
            target_kind=TargetKind.RECOMMENDATION,
            changes=(change,),
        )


def test_an_invalid_candidate_document_is_refused() -> None:
    """The real policy model is the last gate: weights that no longer sum to 1 are refused
    even though each individual path is allowlisted."""
    change = PolicyChange(
        path="ranking.weights.taxonomy",
        from_value=DOCUMENTS[TargetKind.RANKING]["weights"]["taxonomy"],
        to_value=0.9,
    )
    with pytest.raises(CandidateRefusedError) as info:
        prepare_policy_proposal(
            target_kind=TargetKind.RANKING,
            base_document=DOCUMENTS[TargetKind.RANKING],
            changes=(change,),
        )
    assert info.value.refusal is CandidateRefusal.INVALID_CANDIDATE


def test_an_empty_change_set_is_refused() -> None:
    with pytest.raises(CandidateRefusedError) as info:
        prepare_policy_proposal(
            target_kind=TargetKind.RANKING,
            base_document=DOCUMENTS[TargetKind.RANKING],
            changes=(),
        )
    assert info.value.refusal is CandidateRefusal.NO_CHANGE


# --------------------------------------------------------------------------- #
# The model may not widen its own reach
# --------------------------------------------------------------------------- #
def test_a_draft_cannot_name_an_index_outside_the_options_it_was_offered() -> None:
    """ADR-0016 15.7.7, at the contract level: an out-of-range index is a SCHEMA violation."""
    with pytest.raises(ValidationError):
        ProposalDraft(title="t", rationale="r", choice_index=3, option_count=3)
    assert ProposalDraft(title="t", rationale="r", choice_index=2, option_count=3).choice_index == 2
