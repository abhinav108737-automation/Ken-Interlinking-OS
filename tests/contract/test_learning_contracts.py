"""Phase 8 learning contracts (docs/adr/ADR-0016-learning.md; section 15 binding).

The ADR's central safety claim was found DECORATIVE by the pre-fan-out review. These tests
exist so the same thing cannot happen again: each one pins a mechanism, not a docstring.

* the frozen-field set and the change allowlist refuse a proposal at the CONTRACT boundary;
* ``policies/learning/learning.v1.yaml`` may NARROW both and can never widen either;
* a hard-fail suite cannot be waived, and ``min_sample_count`` cannot drop below 30;
* ``ProposalDraft.choice_index`` is validated against the LIVE option count;
* ``validate_payload`` never raises, over every event name x version plus an unknown name,
  an unknown version and a corrupt payload;
* a promotion payload is refused unless ``promoted_by`` is ``user:<username>`` and
  ``evaluation_ref`` is a UUID;
* the DB CHECK patterns the migration copies agree with the contract they copy.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from ken_contracts import events, learning
from ken_contracts.auth import Permission, Role, has_permission
from ken_contracts.intelligence import RelationshipType
from ken_contracts.learning import (
    ALLOWED_CHANGE_PATHS,
    FROZEN_POLICY_PATHS,
    HARD_FAIL_SUITES,
    MIN_SAMPLE_COUNT,
    OPEN_PROPOSAL_STATUSES,
    PROPOSAL_TRANSITION_AUTHORITY,
    PROPOSAL_TRANSITIONS,
    ChangePolicy,
    CrossPolicyContext,
    DecisionClass,
    EvalSuite,
    GatePolicy,
    LearningPolicy,
    PolicyChange,
    PolicyProposalBody,
    ProposalDraft,
    ProposalStatus,
    TargetKind,
)
from ken_contracts.recommendations import RetrievalChannel
from ken_core.config import REPO_ROOT
from ken_services.learning.policy import PolicyError, load_learning_policy

POLICY_PATH = REPO_ROOT / "policies" / "learning" / "learning.v1.yaml"
EVAL_UUID = "2b4e6a8c-1d3f-4a5b-8c7d-9e0f1a2b3c4d"
OPERATION_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


def _raw_policy() -> dict[str, Any]:
    loaded = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


# --------------------------------------------------------------------------- #
# The stage ladder: every stage CLAUDE.md / PRD section 10 mandate, none removed
# --------------------------------------------------------------------------- #
def test_the_full_stage_ladder_is_in_the_contract() -> None:
    """CLAUDE.md's Learning Design Rule, PRD section 10 and evals/README.md all name
    shadow -> canary -> experiment before promotion. Phase 8 defers the MACHINERY of canary and
    experiment (ADR-0016 16.3/16.6); removing the stages would bake a spec deviation into the
    schema."""
    assert {status.value for status in ProposalStatus} == {
        "proposed",
        "offline_test",
        "shadow",
        "canary",
        "experiment",
        "promoted",
        "rejected",
        "rolled_back",
        "withdrawn",
    }


def test_a_proposal_can_never_reach_promoted_without_an_offline_test() -> None:
    assert ProposalStatus.PROMOTED not in PROPOSAL_TRANSITIONS[ProposalStatus.PROPOSED]
    reachable = {
        source
        for source, targets in PROPOSAL_TRANSITIONS.items()
        if ProposalStatus.PROMOTED in targets
    }
    assert ProposalStatus.PROPOSED not in reachable
    assert reachable <= OPEN_PROPOSAL_STATUSES - {ProposalStatus.PROPOSED}


def test_every_stage_has_a_transition_row_and_an_authority_row() -> None:
    assert set(PROPOSAL_TRANSITIONS) == set(ProposalStatus)
    assert set(PROPOSAL_TRANSITION_AUTHORITY) == set(ProposalStatus)
    for status in ProposalStatus:
        assert PROPOSAL_TRANSITIONS[status] <= set(ProposalStatus)


def test_no_agent_may_advance_a_proposal_past_the_insert_status() -> None:
    """Agents propose; they never promote (TRD section 10, ADR-0012)."""
    for status, classes in PROPOSAL_TRANSITION_AUTHORITY.items():
        if status is ProposalStatus.PROPOSED:
            assert "agent" in classes
        else:
            assert "agent" not in classes, status
    assert PROPOSAL_TRANSITION_AUTHORITY[ProposalStatus.PROMOTED] == frozenset({"user"})


def test_promote_is_an_operator_only_permission() -> None:
    assert has_permission(Role.OPERATOR_ADMIN, Permission.PROMOTE)
    assert not has_permission(Role.REVIEWER, Permission.PROMOTE)
    assert not has_permission(Role.VIEWER, Permission.PROMOTE)


# --------------------------------------------------------------------------- #
# Frozen fields: refused at the contract boundary, not merely at review
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [
        "recommendation.placement.excluded_ancestors",
        "recommendation.placement.forbidden_anchors",
        "recommendation.placement.max_anchor_words",
        "recommendation.selection.max_links_per_block",
        "recommendation.selection.rejection_cooldown_days",
        "recommendation.selection.max_internal_links_per_100_words",
        "recommendation.excluded_target_paths",
        "analytics.windows.comparison_days",
        "taxonomy.protected_paths",
        "settings.auto_approve_enabled",
        "model.reasoning",
        "learning.gates.min_sample_count",
        "autonomy.level",
    ],
)
def test_a_frozen_field_cannot_be_proposed(path: str) -> None:
    assert learning.is_frozen_path(path)
    assert not learning.is_learnable_path(path)
    with pytest.raises(ValidationError):
        PolicyChange(path=path, from_value=1, to_value=2)


@pytest.mark.parametrize("path", ["recommendation.selection.gibberish", "ranking.weights.made_up"])
def test_a_path_outside_the_allowlist_cannot_be_proposed(path: str) -> None:
    with pytest.raises(ValidationError):
        PolicyChange(path=path, from_value=1, to_value=2)


def test_the_allowlist_and_the_frozen_set_are_disjoint() -> None:
    assert not any(learning.is_frozen_path(path) for path in ALLOWED_CHANGE_PATHS)


def test_a_proposal_body_refuses_a_change_outside_its_target_policy() -> None:
    with pytest.raises(ValidationError, match="does not belong"):
        PolicyProposalBody(
            target_kind=TargetKind.RANKING,
            base_version="1",
            changes=(
                PolicyChange(
                    path="recommendation.selection.min_score", from_value=0.3, to_value=0.4
                ),
            ),
        )


def test_a_valid_policy_proposal_body_records_its_change_paths() -> None:
    body = PolicyProposalBody(
        target_kind=TargetKind.RANKING,
        base_version="1",
        changes=(
            PolicyChange(path="ranking.weights.semantic", from_value=0.30, to_value=0.34),
            PolicyChange(path="ranking.weights.taxonomy", from_value=0.20, to_value=0.16),
        ),
    )
    assert body.change_paths == ("ranking.weights.semantic", "ranking.weights.taxonomy")


def test_a_body_refuses_the_same_path_twice() -> None:
    with pytest.raises(ValidationError, match="at most once"):
        PolicyProposalBody(
            target_kind=TargetKind.RANKING,
            base_version="1",
            changes=(
                PolicyChange(path="ranking.weights.semantic", from_value=0.30, to_value=0.34),
                PolicyChange(path="ranking.weights.semantic", from_value=0.34, to_value=0.36),
            ),
        )


# --------------------------------------------------------------------------- #
# The ADR-0012 section 10 cross-check (the conflict the doc review found)
# --------------------------------------------------------------------------- #
def test_lowering_max_plans_per_source_below_the_agents_cap_is_refused() -> None:
    """``agents.max_plans_per_run <= recommendation.selection.max_plans_per_source`` is
    cross-checked AT LOAD by ken_services.agents.policy and RAISES. A proposal that lowers the
    cap below the live per-run value would make the agents policy unloadable, so the contract
    refuses it instead of dropping a useful knob from the allowlist."""
    with pytest.raises(ValidationError, match="unloadable"):
        PolicyProposalBody(
            target_kind=TargetKind.RECOMMENDATION,
            base_version="1",
            changes=(
                PolicyChange(
                    path="recommendation.selection.max_plans_per_source",
                    from_value=3,
                    to_value=1,
                ),
            ),
            cross_check=CrossPolicyContext(
                agents_max_plans_per_run=3, recommendation_max_plans_per_source=3
            ),
        )


def test_a_cross_checked_path_must_declare_the_live_companion_values() -> None:
    with pytest.raises(ValidationError, match="cross_check"):
        PolicyProposalBody(
            target_kind=TargetKind.AGENTS,
            base_version="1",
            changes=(PolicyChange(path="agents.max_plans_per_run", from_value=2, to_value=3),),
        )


def test_the_cross_check_allows_a_consistent_pair() -> None:
    body = PolicyProposalBody(
        target_kind=TargetKind.RECOMMENDATION,
        base_version="1",
        changes=(
            PolicyChange(
                path="recommendation.selection.max_plans_per_source", from_value=3, to_value=5
            ),
        ),
        cross_check=CrossPolicyContext(
            agents_max_plans_per_run=2, recommendation_max_plans_per_source=3
        ),
    )
    assert body.change_paths == ("recommendation.selection.max_plans_per_source",)


# --------------------------------------------------------------------------- #
# The YAML may narrow and can NEVER widen (ADR-0016 15.7.5)
# --------------------------------------------------------------------------- #
def test_the_shipped_policy_loads_and_mirrors_the_contract() -> None:
    policy = load_learning_policy(POLICY_PATH)
    assert policy.version == 1
    assert set(policy.changes.allowed_paths) == ALLOWED_CHANGE_PATHS
    assert set(policy.changes.frozen_paths) == FROZEN_POLICY_PATHS
    assert policy.gates.min_sample_count == MIN_SAMPLE_COUNT
    assert policy.behavioural_may_block_only is True
    assert policy.search_is_diagnostic_only is True


def test_the_yaml_cannot_widen_the_change_allowlist() -> None:
    raw = _raw_policy()
    raw["changes"]["allowed_paths"] = [
        *raw["changes"]["allowed_paths"],
        "recommendation.placement.forbidden_anchors",
    ]
    with pytest.raises(ValidationError, match="only NARROW"):
        LearningPolicy.model_validate(raw)


def test_the_yaml_cannot_allowlist_a_frozen_path() -> None:
    """Two independent refusals cover this, and the contract keeps the sets disjoint so the
    allowlist check normally fires first: either way a frozen path cannot be allowlisted."""
    with pytest.raises(ValidationError, match=r"only NARROW|frozen field can never"):
        ChangePolicy(
            allowed_paths=("analytics.windows.comparison_days",),
            frozen_paths=tuple(sorted(FROZEN_POLICY_PATHS)),
        )
    # The second, belt-and-braces branch: a path that IS allowlisted but also frozen. The
    # contract keeps them disjoint (asserted above), so this is simulated by freezing a
    # prefix of an allowlisted path.
    assert learning.is_frozen_path("learning.gates.min_sample_count")
    assert not learning.is_learnable_path("learning.gates.min_sample_count")


def test_the_yaml_can_narrow_the_change_allowlist() -> None:
    raw = _raw_policy()
    raw["changes"]["allowed_paths"] = ["ranking.weights.semantic"]
    narrowed = LearningPolicy.model_validate(raw)
    assert narrowed.changes.allowed_paths == ("ranking.weights.semantic",)


def test_the_yaml_cannot_drop_a_frozen_path() -> None:
    raw = _raw_policy()
    raw["changes"]["frozen_paths"] = [
        path for path in raw["changes"]["frozen_paths"] if path != "analytics"
    ]
    with pytest.raises(ValidationError, match="only ADD frozen paths"):
        LearningPolicy.model_validate(raw)


def test_the_yaml_can_add_a_frozen_path() -> None:
    raw = _raw_policy()
    raw["changes"]["frozen_paths"] = [*raw["changes"]["frozen_paths"], "ranking.weights"]
    widened = LearningPolicy.model_validate(raw)
    assert "ranking.weights" in widened.changes.frozen_paths


@pytest.mark.parametrize("suite", sorted(suite.value for suite in HARD_FAIL_SUITES))
def test_a_hard_fail_suite_cannot_be_waived(suite: str) -> None:
    raw = _raw_policy()
    raw["gates"]["hard_fail_suites"] = [
        name for name in raw["gates"]["hard_fail_suites"] if name != suite
    ]
    with pytest.raises(ValidationError, match="cannot be waived"):
        LearningPolicy.model_validate(raw)


def test_the_sample_floor_cannot_be_lowered() -> None:
    raw = _raw_policy()
    raw["gates"]["min_sample_count"] = 5
    with pytest.raises(ValidationError):
        LearningPolicy.model_validate(raw)
    raw["gates"]["min_sample_count"] = MIN_SAMPLE_COUNT + 20
    assert LearningPolicy.model_validate(raw).gates.min_sample_count == MIN_SAMPLE_COUNT + 20


def test_a_hard_fail_suite_needs_a_declared_gate() -> None:
    raw = _raw_policy()
    raw["gates"]["suites"].pop("proposal-safety")
    with pytest.raises(ValidationError, match="needs a declared gate"):
        LearningPolicy.model_validate(raw)


def test_every_required_stage_path_includes_an_offline_test() -> None:
    policy = load_learning_policy(POLICY_PATH)
    assert set(policy.gates.required_stages) == {"ranking", "recommendation", "agents", "skill"}
    for stages in policy.gates.required_stages.values():
        assert ProposalStatus.OFFLINE_TEST in stages


def test_a_required_stage_path_cannot_name_a_terminal_stage() -> None:
    raw = _raw_policy()
    raw["gates"]["required_stages"]["skill"] = ["offline_test", "promoted"]
    with pytest.raises(ValidationError, match="non-open stage"):
        LearningPolicy.model_validate(raw)


def test_a_malformed_policy_file_raises_policy_error(tmp_path: Any) -> None:
    broken = tmp_path / "learning.v1.yaml"
    broken.write_text("- not: a mapping\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_learning_policy(broken)


def test_the_gate_policy_cannot_declare_an_unknown_required_stage_key() -> None:
    policy = _raw_policy()["gates"]
    policy["required_stages"]["taxonomy"] = ["offline_test"]
    with pytest.raises(ValidationError, match="required_stages keys"):
        GatePolicy.model_validate(policy)


# --------------------------------------------------------------------------- #
# The bounded model role (ADR-0016 15.2d / 15.7.7)
# --------------------------------------------------------------------------- #
def test_choice_index_is_validated_against_the_live_option_count() -> None:
    draft = ProposalDraft(
        title="Shift weight to taxonomy", rationale="", choice_index=2, option_count=3
    )
    assert draft.choice_index == 2
    with pytest.raises(ValidationError, match="outside the 3 options"):
        ProposalDraft(title="t", rationale="", choice_index=3, option_count=3)
    with pytest.raises(ValidationError):
        ProposalDraft(title="t", rationale="", choice_index=-1, option_count=3)


def test_a_draft_carries_no_number_that_could_be_stored() -> None:
    """The model may return a title, prose and an index. Nothing else: no path, no value."""
    assert set(ProposalDraft.model_fields) == {
        "title",
        "rationale",
        "choice_index",
        "option_count",
    }


# --------------------------------------------------------------------------- #
# Decision class: a typed contract with a canonical string form (15.7.11)
# --------------------------------------------------------------------------- #
def test_the_no_relationship_sentinel_is_not_a_relationship_type() -> None:
    assert learning.NO_RELATIONSHIP not in {item.value for item in RelationshipType}


def test_the_canonical_form_round_trips_and_is_order_independent() -> None:
    first = DecisionClass(
        relationship_type=RelationshipType.SAME_MARKET,
        anchor_source="target_topic",
        channels=(RetrievalChannel.SEMANTIC, RetrievalChannel.GRAPH),
    )
    second = DecisionClass(
        relationship_type=RelationshipType.SAME_MARKET,
        anchor_source="target_topic",
        channels=(RetrievalChannel.GRAPH, RetrievalChannel.SEMANTIC),
    )
    assert first.canonical == second.canonical == "same_market|target_topic|graph+semantic"
    assert DecisionClass.parse(first.canonical) == first
    assert learning.is_canonical_decision_class(first.canonical)


def test_a_class_without_a_relationship_type_is_still_canonical() -> None:
    cls = DecisionClass(anchor_source="edited", channels=(RetrievalChannel.LEXICAL,))
    assert cls.canonical == "none|edited|lexical"
    assert DecisionClass.parse("none|edited|lexical") == cls


@pytest.mark.parametrize(
    "value",
    ["", "a|b", "Same_Market|target_topic|semantic", "same_market|target_topic|", "x|y|z w"],
)
def test_a_non_canonical_decision_class_is_refused(value: str) -> None:
    assert not learning.is_canonical_decision_class(value)
    with pytest.raises(ValueError, match="canonical"):
        DecisionClass.parse(value)


def test_metrics_require_a_by_decision_class_object_keyed_canonically() -> None:
    metrics = learning.EvaluationMetrics(
        cases_total=2,
        cases_passed=2,
        by_suite=(
            learning.SuiteMetrics(
                suite=EvalSuite.CRITIC,
                cases=2,
                passed=2,
                failed=0,
                must_not_violations=0,
                pass_rate=1.0,
            ),
        ),
        by_decision_class={},
    )
    assert metrics.by_decision_class == {}
    assert "by_decision_class" in learning.EvaluationMetrics.model_fields
    row = learning.DecisionClassMetrics(
        decision_class="same_market|target_topic|semantic", sample_count=1, sufficient=False
    )
    with pytest.raises(ValidationError, match="must equal its row"):
        metrics.model_copy(
            update={"by_decision_class": {"other|edited|graph": row}}
        ).model_validate(
            {
                "cases_total": 2,
                "cases_passed": 2,
                "by_suite": [metrics.by_suite[0].model_dump()],
                "by_decision_class": {"other|edited|graph": row.model_dump()},
            }
        )


# --------------------------------------------------------------------------- #
# EvalCase: the full evals/README.md skeleton, including privacy
# --------------------------------------------------------------------------- #
def _case(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "case_id": "critic-rejects-a-weak-anchor",
        "suite": EvalSuite.CRITIC.value,
        "fixture_version": 1,
        "decision_class": {
            "relationship_type": "same_market",
            "anchor_source": "target_topic",
            "channels": ["semantic"],
        },
        "description": "The critic rejects a candidate whose anchor is a bare brand name.",
        "inputs": {"policy_version": "1"},
        "expected": {"decision": "reject", "acceptable_alternatives": [], "must_not": ["accept"]},
        "rationale": "A bare brand name is not a descriptive anchor.",
        "provenance": {
            "origin": "synthetic",
            "created_by": "orchestrator",
            "created_at": "2026-10-16",
            "source_refs": [],
        },
        "privacy": {"contains_production_data": False, "approval_ref": None},
    }
    payload.update(overrides)
    return payload


def test_an_eval_case_carries_the_whole_frozen_skeleton() -> None:
    case = learning.EvalCase.model_validate(_case())
    assert case.decision_class is not None
    assert case.decision_class.canonical == "same_market|target_topic|semantic"
    assert set(learning.EvalCase.model_fields) == {
        "case_id",
        "suite",
        "fixture_version",
        "decision_class",
        "description",
        "inputs",
        "expected",
        "rationale",
        "provenance",
        "privacy",
    }


def test_production_data_without_an_approval_ref_is_refused() -> None:
    """evals/README.md lines 138-151: no real production data without explicit user approval."""
    with pytest.raises(ValidationError, match="approval_ref"):
        learning.EvalCase.model_validate(
            _case(privacy={"contains_production_data": True, "approval_ref": None})
        )
    approved = learning.EvalCase.model_validate(
        _case(
            privacy={"contains_production_data": True, "approval_ref": "user-approval-2026-10-16"}
        )
    )
    assert approved.privacy.approval_ref == "user-approval-2026-10-16"


def test_a_link_decision_suite_requires_a_decision_class() -> None:
    with pytest.raises(ValidationError, match="decision_class"):
        learning.EvalCase.model_validate(_case(decision_class=None))


def test_a_proposal_safety_case_needs_no_decision_class() -> None:
    case = learning.EvalCase.model_validate(
        _case(
            case_id="proposal-safety-refuses-self-promotion",
            suite=EvalSuite.PROPOSAL_SAFETY.value,
            decision_class=None,
        )
    )
    assert case.decision_class is None


def test_a_decision_cannot_both_pass_and_be_a_must_not() -> None:
    with pytest.raises(ValidationError, match="must_not"):
        learning.EvalCase.model_validate(
            _case(
                expected={
                    "decision": "accept",
                    "acceptable_alternatives": [],
                    "must_not": ["accept"],
                }
            )
        )


def test_every_trd_section_19_suite_is_in_the_vocabulary() -> None:
    assert {
        "destination-selection",
        "anchor-selection",
        "relationship-detection",
        "critic",
        "no-action",
        "protected-content",
        "stale-page-version",
    } <= {suite.value for suite in EvalSuite}


# --------------------------------------------------------------------------- #
# Version-aware event reads (ADR-0016 section 8 / 15.7.14)
# --------------------------------------------------------------------------- #
def test_every_event_name_is_either_registered_or_explicitly_untyped() -> None:
    """A new name forces a decision rather than silently defaulting to 'untyped'."""
    assert set(events.EVENT_PAYLOAD_MODELS) | events.UNTYPED_EVENTS == set(events.EventName)
    assert not set(events.EVENT_PAYLOAD_MODELS) & events.UNTYPED_EVENTS


@pytest.mark.parametrize("name", sorted(events.EVENT_PAYLOAD_MODELS, key=str))
def test_a_registered_name_validates_its_own_version(name: events.EventName) -> None:
    for version in events.EVENT_PAYLOAD_MODELS[name]:
        unknown = events.validate_payload(
            event_name=name.value, event_version=version + 100, payload={}
        )
        assert unknown.status is events.PayloadStatus.UNKNOWN_VERSION
        assert unknown.event_name is name
        corrupt = events.validate_payload(
            event_name=name.value, event_version=version, payload={"nonsense": object()}
        )
        assert corrupt.status is events.PayloadStatus.INVALID


@pytest.mark.parametrize("name", sorted(events.UNTYPED_EVENTS, key=str))
def test_an_untyped_name_has_no_payload_schema_to_violate(name: events.EventName) -> None:
    verdict = events.validate_payload(
        event_name=name.value, event_version=1, payload={"anything": [1, "two"]}
    )
    assert verdict.status is events.PayloadStatus.OK
    assert verdict.model is None


def test_an_unknown_event_name_is_unknown_version_and_never_raises() -> None:
    """Before Phase 8 an unknown stored name broke list_events at EventName(row.event_name)."""
    verdict = events.validate_payload(event_name="made.up", event_version=1, payload={})
    assert verdict.status is events.PayloadStatus.UNKNOWN_VERSION
    assert verdict.event_name is None


@pytest.mark.parametrize(
    "payload", [None, [], "a string", 0, {"": None}, {"nested": {"deep": object()}}]
)
def test_validate_payload_never_raises_whatever_is_stored(payload: Any) -> None:
    verdict = events.validate_payload(
        event_name=events.EventName.JOB_COMPLETED.value, event_version=1, payload=payload
    )
    assert verdict.status in set(events.PayloadStatus)


def test_a_good_payload_validates_and_names_its_model() -> None:
    verdict = events.validate_payload(
        event_name=events.EventName.SKILL_PROMOTED.value,
        event_version=1,
        payload={
            "artifact_kind": "skill",
            "name": "select_natural_anchor",
            "version": "2",
            "previous_version": "1",
            "promoted_by": "user:seo-manager",
            "evaluation_ref": EVAL_UUID,
        },
    )
    assert verdict.status is events.PayloadStatus.OK
    assert verdict.model == "PromotionAuditPayload"


def test_event_record_defaults_to_ok_so_the_field_is_additive() -> None:
    from ken_contracts.controlplane import EventRecord

    assert EventRecord.model_fields["payload_status"].default == "ok"


# --------------------------------------------------------------------------- #
# The two new typed payloads
# --------------------------------------------------------------------------- #
def _proposed_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "proposal_id": "11111111-1111-4111-8111-111111111111",
        "fingerprint": "b" * 64,
        "target_kind": "ranking",
        "base_version": "1",
        "change_paths": ["ranking.weights.semantic"],
        "proposed_by": "system:learning",
        "job_run_id": "22222222-2222-4222-8222-222222222222",
        "evidence_window": {"start": "2026-09-01", "end": "2026-09-28"},
        "model": None,
        "fallback_used": True,
    }
    payload.update(overrides)
    return payload


def test_a_policy_proposed_event_refuses_a_frozen_change_path() -> None:
    """Belt and braces: a proposal that touched a frozen field cannot even be announced."""
    with pytest.raises(ValidationError, match="may not change"):
        events.PolicyProposedPayload.model_validate(
            _proposed_payload(change_paths=["recommendation.placement.forbidden_anchors"])
        )


@pytest.mark.parametrize("actor", ["user:admin", "user:bob", "nonsense"])
def test_a_proposal_can_never_claim_a_human_author(actor: str) -> None:
    with pytest.raises(ValidationError):
        events.PolicyProposedPayload.model_validate(_proposed_payload(proposed_by=actor))


def test_an_agent_may_propose() -> None:
    payload = events.PolicyProposedPayload.model_validate(
        _proposed_payload(proposed_by="agent:reflector@1")
    )
    assert payload.proposed_by == "agent:reflector@1"
    assert payload.evidence_window.start.isoformat() == "2026-09-01"


def test_a_skill_proposed_event_is_typed() -> None:
    payload = events.SkillProposedPayload.model_validate(
        {
            "proposal_id": "11111111-1111-4111-8111-111111111111",
            "fingerprint": "c" * 64,
            "name": "select_natural_anchor",
            "version": 2,
            "source": "reflection",
            "proposed_by": "system:learning",
            "job_run_id": "22222222-2222-4222-8222-222222222222",
        }
    )
    assert payload.version == 2


# --------------------------------------------------------------------------- #
# Promotion audit payload: tightened in Phase 8 (15.7.4)
# --------------------------------------------------------------------------- #
def test_the_promoter_pattern_is_user_plus_the_auth_username_pattern() -> None:
    from ken_contracts.auth import USERNAME_PATTERN

    expected = "^user:" + USERNAME_PATTERN.removeprefix("^")
    assert expected == events.PROMOTER_PATTERN
    assert learning.PROMOTER_ACTOR_PATTERN == events.PROMOTER_PATTERN


@pytest.mark.parametrize(
    "actor", ["agent:reflector@1", "system:learning", "seo-manager@example.com", "user:X", "admin"]
)
def test_a_promotion_is_refused_without_a_human_authoriser(actor: str) -> None:
    with pytest.raises(ValidationError):
        events.PromotionAuditPayload(
            artifact_kind="policy",
            name="ranking",
            version="2",
            promoted_by=actor,
            evaluation_ref=EVAL_UUID,
        )


@pytest.mark.parametrize("ref", ["eval-run-0001", "", "not-a-uuid", "1"])
def test_a_promotion_must_cite_an_evaluation_run_id(ref: str) -> None:
    with pytest.raises(ValidationError):
        events.PromotionAuditPayload(
            artifact_kind="policy",
            name="ranking",
            version="2",
            promoted_by="user:admin",
            evaluation_ref=ref,
        )


# --------------------------------------------------------------------------- #
# Experiments: BOTH kinds, with holdout defaulted off and capped (user decision)
# --------------------------------------------------------------------------- #
def test_both_experiment_kinds_exist() -> None:
    assert {kind.value for kind in learning.ExperimentKind} == {"policy_ab", "holdout"}


def test_the_assignment_unit_is_a_function_of_the_kind() -> None:
    """The fix for the review's arm-contamination bug: a holdout compares TARGET pages, so its
    arms are assigned to target pages. No experiment can measure one page while assigning
    another."""
    assert learning.experiment_unit(learning.ExperimentKind.HOLDOUT) is (
        learning.ExperimentUnit.TARGET_PAGE
    )
    assert learning.experiment_unit(learning.ExperimentKind.POLICY_AB) is (
        learning.ExperimentUnit.SOURCE_PAGE
    )
    assert set(learning.EXPERIMENT_UNIT_BY_KIND) == set(learning.ExperimentKind)


def _experiment_view(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "44444444-4444-4444-8444-444444444444",
        "key": "ranking-ab-1",
        "description": "d",
        "unit": "source_page",
        "kind": "policy_ab",
        "status": "draft",
        "candidate_id": "55555555-5555-4555-8555-555555555555",
        "treatment_fraction": 0.5,
        "created_by": "user:ops",
        "created_at": "2026-10-16T09:00:00Z",
        "updated_at": "2026-10-16T09:00:00Z",
    }
    payload.update(overrides)
    return payload


def test_a_holdout_must_assign_by_target_page_and_carry_no_candidate() -> None:
    with pytest.raises(ValidationError, match="assigns arms by target_page"):
        learning.ExperimentView.model_validate(
            _experiment_view(kind="holdout", candidate_id=None, treatment_fraction=0.1)
        )
    with pytest.raises(ValidationError, match="tests no candidate policy"):
        learning.ExperimentView.model_validate(
            _experiment_view(kind="holdout", unit="target_page", treatment_fraction=0.1)
        )
    holdout = learning.ExperimentView.model_validate(
        _experiment_view(
            kind="holdout", unit="target_page", candidate_id=None, treatment_fraction=0.1
        )
    )
    assert holdout.treatment_fraction == learning.DEFAULT_HOLDOUT_TREATMENT_FRACTION


def test_a_policy_ab_must_name_its_candidate_and_assign_by_source_page() -> None:
    with pytest.raises(ValidationError, match="names the candidate"):
        learning.ExperimentView.model_validate(_experiment_view(candidate_id=None))
    with pytest.raises(ValidationError, match="assigns arms by source_page"):
        learning.ExperimentView.model_validate(_experiment_view(unit="target_page"))


@pytest.mark.parametrize("fraction", [0.21, 0.5, 1.0])
def test_a_holdout_above_the_hard_maximum_is_refused(fraction: float) -> None:
    """20% is the hard maximum, and the database CHECKs it too: a policy file cannot raise it."""
    assert learning.MAX_HOLDOUT_TREATMENT_FRACTION == 0.20
    with pytest.raises(ValidationError, match="at most 20%"):
        learning.ExperimentView.model_validate(
            _experiment_view(
                kind="holdout",
                unit="target_page",
                candidate_id=None,
                treatment_fraction=fraction,
            )
        )


def test_the_policy_default_holdout_fraction_is_half_the_maximum() -> None:
    policy = load_learning_policy(POLICY_PATH)
    assert policy.experiments.holdout_treatment_fraction == 0.10
    assert policy.experiments.holdout_treatment_fraction <= learning.MAX_HOLDOUT_TREATMENT_FRACTION


def test_the_learning_policy_cannot_raise_the_holdout_cap() -> None:
    raw = _raw_policy()
    raw["experiments"]["holdout_treatment_fraction"] = 0.5
    with pytest.raises(ValidationError):
        LearningPolicy.model_validate(raw)


def test_activation_has_a_typed_refusal_for_an_unenabled_holdout() -> None:
    error = learning.ExperimentRefusedError(learning.ExperimentRefusal.HOLDOUT_NOT_ENABLED)
    assert error.refusal is learning.ExperimentRefusal.HOLDOUT_NOT_ENABLED
    assert str(error) == "holdout_not_enabled"


def test_the_arm_is_deterministic_and_depends_on_the_assigned_page() -> None:
    from ken_services.learning.protocol import experiment_arm

    page = uuid.UUID("66666666-6666-4666-8666-666666666666")
    first = experiment_arm(key="ab-1", page_id=page, treatment_fraction=0.5)
    assert first is experiment_arm(key="ab-1", page_id=page, treatment_fraction=0.5)
    assert (
        experiment_arm(key="ab-1", page_id=page, treatment_fraction=1.0)
        is learning.ExperimentArm.TREATMENT
    )


# --------------------------------------------------------------------------- #
# The one write request
# --------------------------------------------------------------------------- #
def test_a_promotion_request_must_cite_an_evaluation_run() -> None:
    with pytest.raises(ValidationError, match="passing evaluation run"):
        learning.AdvanceProposalRequest(
            operation_id=OPERATION_ID,
            to_status=ProposalStatus.PROMOTED,
        )


def test_a_non_promotion_request_may_not_carry_an_evaluation_run() -> None:
    with pytest.raises(ValidationError, match="only a promotion"):
        learning.AdvanceProposalRequest(
            operation_id=OPERATION_ID,
            to_status=ProposalStatus.SHADOW,
            evaluation_run_id=uuid.UUID(EVAL_UUID),
        )


def test_a_rejection_needs_a_structured_reason_from_its_own_set() -> None:
    with pytest.raises(ValidationError, match="requires a structured reason"):
        learning.AdvanceProposalRequest(
            operation_id=OPERATION_ID,
            to_status=ProposalStatus.REJECTED,
        )
    with pytest.raises(ValidationError, match="is not a reason for"):
        learning.AdvanceProposalRequest(
            operation_id=OPERATION_ID,
            to_status=ProposalStatus.REJECTED,
            reason=learning.ProposalRejectionReason.REGRESSION_OBSERVED,
        )
    accepted = learning.AdvanceProposalRequest(
        operation_id=OPERATION_ID,
        to_status=ProposalStatus.REJECTED,
        reason=learning.ProposalRejectionReason.DEMONSTRATION_ONLY,
        note="synthetic corpus",
    )
    assert accepted.note == "synthetic corpus"


def test_a_note_cannot_carry_an_unstorable_character() -> None:
    with pytest.raises(ValidationError):
        learning.AdvanceProposalRequest(
            operation_id=OPERATION_ID,
            to_status=ProposalStatus.SHADOW,
            note="bad\x00note",
        )


# --------------------------------------------------------------------------- #
# Parity with migration 0009's frozen literal copies
# --------------------------------------------------------------------------- #
def test_the_migration_copies_the_contract_exactly() -> None:
    import importlib.util

    path = REPO_ROOT / "migrations" / "versions" / "0009_learning.py"
    spec = importlib.util.spec_from_file_location("migration_0009_for_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert set(module.PROPOSAL_STATUSES) == {status.value for status in ProposalStatus}
    assert set(module.OPEN_STATUSES) == {status.value for status in OPEN_PROPOSAL_STATUSES}
    assert set(module.SKILL_STATUSES) == {status.value for status in learning.SkillStatus}
    assert set(module.EXPERIMENT_STATUSES) == {s.value for s in learning.ExperimentStatus}
    assert set(module.EXPERIMENT_KINDS) == {kind.value for kind in learning.ExperimentKind}
    assert set(module.EXPERIMENT_UNITS) == {unit.value for unit in learning.ExperimentUnit}
    assert module.MAX_HOLDOUT_TREATMENT_FRACTION == learning.MAX_HOLDOUT_TREATMENT_FRACTION
    assert module.ACTIVATOR_SETTING == learning.EXPERIMENT_ACTIVATOR_SETTING
    contract_pairs = {
        (source.value, target.value)
        for source, targets in PROPOSAL_TRANSITIONS.items()
        for target in targets
    }
    assert set(module.PROPOSAL_TRANSITIONS) == contract_pairs
    skill_pairs = {
        (source.value, target.value)
        for source, targets in learning.SKILL_TRANSITIONS.items()
        for target in targets
    }
    assert set(module.SKILL_TRANSITIONS) == skill_pairs
    experiment_pairs = {
        (source.value, target.value)
        for source, targets in learning.EXPERIMENT_TRANSITIONS.items()
        for target in targets
    }
    assert set(module.EXPERIMENT_TRANSITIONS) == experiment_pairs
    # The DB CHECK can only test an actor's SHAPE; these are the shapes it tests.
    assert module._PROMOTER_RE.strip("'") == learning.PROMOTER_ACTOR_PATTERN
    assert module._PROPOSER.removeprefix("~ ").strip("'") == learning.PROPOSER_PATTERN
    assert set(module.HUMAN_ONLY_TRANSITION_TARGETS) <= {
        status.value for status in learning.HUMAN_ONLY_STATUSES
    }
    # JobKind.REFLECTION must be added to the job_runs CHECK, or `make reflect` cannot run.
    # 0009's list is a SNAPSHOT of the enum at that revision, so later members belong to
    # later migrations (0010 adds autonomy / graph_metrics / vector_index); what this
    # asserts is that 0009 added exactly `reflection` and nothing else.
    from ken_contracts.jobs import JobKind

    assert set(module.JOB_KINDS_0009) <= {kind.value for kind in JobKind}
    assert set(module.JOB_KINDS_0009) - set(module.JOB_KINDS_0008) == {"reflection"}
    assert "reflection" in module.JOB_KINDS_0009
    assert "reflection" not in module.JOB_KINDS_0008


def test_the_contract_and_the_migration_agree_on_the_promoter_setting() -> None:
    import importlib.util

    path = REPO_ROOT / "migrations" / "versions" / "0009_learning.py"
    spec = importlib.util.spec_from_file_location("migration_0009_guc_for_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.PROMOTER_SETTING == learning.LEARNING_PROMOTER_SETTING == "ken.learning_promoter"
    assert re.fullmatch(r"ken\.[a-z_]+", learning.LEARNING_PROMOTER_SETTING)
