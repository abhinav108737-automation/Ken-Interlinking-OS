"""Contract tests: the frozen W0 shapes, asserted.

A freeze without tests is a convention. These tests fail the build if a
frozen contract drifts, which is what lets parallel workstreams rely on it.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from ken_contracts import (
    agents,
    analytics,
    auth,
    cms,
    common,
    controlplane,
    deployment,
    errors,
    events,
    health,
    intelligence,
    inventory,
    jobs,
    model,
    recommendations,
    review,
)
from ken_contracts.common import AsyncClosable, ContractModel

CONTRACT_MODULES = [
    agents,
    analytics,
    auth,
    cms,
    common,
    controlplane,
    deployment,
    errors,
    events,
    health,
    intelligence,
    inventory,
    jobs,
    model,
    recommendations,
    review,
]


def _contract_models() -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []
    for module in CONTRACT_MODULES:
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, BaseModel)
                and obj.__module__ == module.__name__
                and obj is not ContractModel
            ):
                found.append(obj)
    return found


@pytest.mark.parametrize("model_cls", _contract_models(), ids=lambda c: c.__name__)
def test_every_contract_model_forbids_extra_keys(model_cls: type[BaseModel]) -> None:
    """TRD.md section 6: unknown keys must fail at the boundary, never be dropped."""
    assert model_cls.model_config.get("extra") == "forbid", (
        f"{model_cls.__name__} must inherit ContractModel/FrozenModel"
    )


def test_trd_section_15_event_vocabulary_is_complete() -> None:
    trd_names = {
        "page.created", "page.updated", "page.deleted", "crawl.completed",
        "link.proposed", "link.approved", "link.rejected", "link.deployed",
        "link.verified", "link.failed", "link.rolled_back", "analytics.updated",
        "skill.proposed", "policy.proposed", "policy.promoted",
    }  # fmt: skip
    assert trd_names <= {e.value for e in events.EventName}


def test_event_envelope_carries_audit_fields() -> None:
    env = events.EventEnvelope(event_name=events.EventName.WORKER_HEARTBEAT, emitted_by="t")
    assert env.event_id is not None
    assert env.event_version == 1
    with pytest.raises(ValidationError):
        events.EventEnvelope(event_name="not.a.real.event", emitted_by="t")


@pytest.mark.parametrize(
    "protocol",
    [
        model.ModelProvider,
        cms.CMSProvider,
        analytics.AnalyticsProvider,
        inventory.ContentSourceProvider,
        inventory.CMSInventoryReader,
    ],
)
def test_provider_protocols_are_closable(protocol: type) -> None:
    assert issubclass(protocol, AsyncClosable)
    assert hasattr(protocol, "aclose")


@pytest.mark.parametrize(
    ("protocol", "method"),
    [
        (model.ModelProvider, "complete"),
        (model.ModelProvider, "complete_structured"),
        (model.ModelProvider, "embed"),
        (cms.CMSProvider, "get_page"),
        (cms.CMSProvider, "validate_link_changes"),
        (cms.CMSProvider, "apply_link_changes"),
        (cms.CMSProvider, "rollback"),
        (analytics.AnalyticsProvider, "fetch_link_metrics"),
        (inventory.CMSInventoryReader, "list_pages"),
        (inventory.CMSInventoryReader, "get_page"),
        (inventory.InventoryReader, "list_pages"),
        (inventory.InventoryReader, "get_page"),
        (inventory.InventoryReader, "list_page_versions"),
        (inventory.InventoryReader, "get_page_version"),
        (inventory.InventoryReader, "list_page_links"),
        (inventory.InventoryReader, "list_links"),
        (inventory.InventoryReader, "list_runs"),
        (inventory.InventoryReader, "get_run"),
        (inventory.InventoryReader, "list_run_issues"),
        (intelligence.IntelligenceReader, "get_page_intelligence"),
        (intelligence.IntelligenceReader, "list_similar"),
        (intelligence.IntelligenceReader, "list_relationships"),
        (intelligence.IntelligenceReader, "list_topic_pages"),
        (intelligence.IntelligenceReader, "semantic_search"),
        (jobs.JobReader, "list_runs"),
        (jobs.JobReader, "get_run"),
        (recommendations.RecommendationReader, "list_plans"),
        (recommendations.RecommendationReader, "get_plan"),
        (recommendations.RecommendationReader, "list_candidates"),
        (recommendations.RecommendationReader, "list_opportunities"),
    ],
)
def test_protocol_methods_are_async_and_keyword_only(protocol: type, method: str) -> None:
    """Keyword-only means a new parameter can never shift an existing position."""
    fn = getattr(protocol, method)
    assert inspect.iscoroutinefunction(fn)
    params = [p for p in inspect.signature(fn).parameters.values() if p.name != "self"]
    assert params, f"{method} has no parameters"
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params)


def test_rollback_requires_actor() -> None:
    """ROLLBACK_PRODUCTION is ASK-level; its authoriser must be recorded."""
    params = inspect.signature(cms.CMSProvider.rollback).parameters
    assert "actor" in params
    assert params["actor"].default is inspect.Parameter.empty


def test_deployment_result_carries_rollback_metadata() -> None:
    assert "previous_version" in cms.DeploymentResult.model_fields
    assert cms.DeploymentResult.model_fields["previous_version"].is_required()


def test_link_change_request_requires_idempotency_and_version() -> None:
    """CLAUDE.md constraint 12: idempotent and version-aware by construction."""
    fields = cms.LinkChangeRequest.model_fields
    assert fields["operation_id"].is_required()
    assert fields["expected_version"].is_required()
    with pytest.raises(ValidationError):  # at least one change
        cms.LinkChangeRequest(
            operation_id=uuid4(), source_page_id="p", expected_version="1", changes=[], actor="a"
        )


def test_structured_result_round_trips_a_generic_schema() -> None:
    class Verdict(BaseModel):
        ok: bool

    completion = model.Completion(
        text='{"ok": true}', model="m", role=model.ModelRole.FAST, latency_ms=1.0, provider="p"
    )
    result = model.StructuredResult[Verdict](
        value=Verdict(ok=True), completion=completion, attempts=1
    )
    assert result.value.ok is True


def test_error_response_rejects_unknown_codes() -> None:
    with pytest.raises(ValidationError):
        errors.ErrorResponse(code="made_up", message="x")


def test_readiness_status_values_are_frozen() -> None:
    assert {s.value for s in health.ReadinessStatus} == {"ready", "degraded", "not_ready"}


# --------------------------------------------------------------------------- #
# Governed promotion events (skill.promoted added post-Phase-0, 2026-10-13)
# --------------------------------------------------------------------------- #
def _promotion(kind: str = "skill", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "artifact_kind": kind,
        "name": "select_natural_anchor",
        "version": "2",
        "previous_version": "1",
        "promoted_by": "seo-manager@example.com",
        "evaluation_ref": "eval-run-0001",
    }
    payload.update(overrides)
    return payload


def test_skill_lifecycle_events_exist() -> None:
    """skill.proposed -> offline/human evaluation -> skill.promoted."""
    names = {e.value for e in events.EventName}
    assert {"skill.proposed", "skill.promoted"} <= names
    assert {"policy.proposed", "policy.promoted"} <= names


@pytest.mark.parametrize(
    ("event_name", "kind"),
    [(events.EventName.SKILL_PROMOTED, "skill"), (events.EventName.POLICY_PROMOTED, "policy")],
)
def test_a_complete_promotion_event_is_accepted(event_name: events.EventName, kind: str) -> None:
    envelope = events.EventEnvelope(
        event_name=event_name,
        emitted_by="ken-api",
        subject_type=kind,
        subject_id="select_natural_anchor",
        payload=_promotion(kind),
    )
    assert envelope.payload["promoted_by"] == "seo-manager@example.com"


@pytest.mark.parametrize(
    "payload",
    [
        {},  # no audit trail at all
        _promotion(evaluation_ref=""),  # promotion must cite a passing evaluation
        _promotion(promoted_by=""),  # promotion must name its human authoriser
        _promotion(version=""),
        {k: v for k, v in _promotion().items() if k != "evaluation_ref"},
        _promotion(unexpected="field"),  # extra keys are a contract violation
    ],
    ids=["empty", "no-evaluation", "no-authoriser", "no-version", "missing-eval-key", "extra-key"],
)
def test_an_ungoverned_skill_promotion_is_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        events.EventEnvelope(
            event_name=events.EventName.SKILL_PROMOTED,
            emitted_by="ken-api",
            subject_type="skill",
            subject_id="select_natural_anchor",
            payload=payload,
        )


def test_promotion_payload_must_match_the_event_kind() -> None:
    with pytest.raises(ValidationError, match="must describe a skill"):
        events.EventEnvelope(
            event_name=events.EventName.SKILL_PROMOTED,
            emitted_by="ken-api",
            subject_type="skill",
            subject_id="select_natural_anchor",
            payload=_promotion("policy"),
        )


@pytest.mark.parametrize(
    ("subject_type", "subject_id"),
    [(None, None), ("policy", "select_natural_anchor"), ("skill", "some_other_skill")],
)
def test_promotion_subject_must_identify_the_promoted_artifact(
    subject_type: str | None, subject_id: str | None
) -> None:
    with pytest.raises(ValidationError, match="subject_type"):
        events.EventEnvelope(
            event_name=events.EventName.SKILL_PROMOTED,
            emitted_by="ken-api",
            subject_type=subject_type,
            subject_id=subject_id,
            payload=_promotion(),
        )


def test_non_promotion_events_are_not_constrained_by_promotion_rules() -> None:
    """A proposal needs no human authoriser -- agents may propose (ALLOW)."""
    envelope = events.EventEnvelope(
        event_name=events.EventName.SKILL_PROPOSED, emitted_by="ken-learning-agent"
    )
    assert envelope.payload == {}


def test_list_response_round_trips_through_json() -> None:
    """A typed client must be able to validate what the API emits (has_more is computed)."""
    page = errors.ListResponse[int](items=[1, 2], page=errors.PageInfo(total=5, limit=2, offset=0))
    data = page.model_dump(mode="json")
    assert data["page"]["has_more"] is True
    again = errors.ListResponse[int].model_validate(data)
    assert again == page
    with pytest.raises(ValidationError):
        errors.PageInfo.model_validate({"total": 1, "limit": 1, "offset": 0, "surprise": 1})


def test_event_vocabulary_beyond_trd_is_exactly_the_governed_additions() -> None:
    """CONTRACTS rule 12: every name outside TRD section 15 is a recorded decision."""
    trd_names = {
        "page.created", "page.updated", "page.deleted", "crawl.completed",
        "link.proposed", "link.approved", "link.rejected", "link.deployed",
        "link.verified", "link.failed", "link.rolled_back", "analytics.updated",
        "skill.proposed", "policy.proposed", "policy.promoted",
    }  # fmt: skip
    governed = {"worker.heartbeat", "skill.promoted", "job.completed"}
    assert {e.value for e in events.EventName} == trd_names | governed


def test_taxonomy_policy_file_validates() -> None:
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parents[2] / "policies" / "taxonomy" / "taxonomy.v1.yaml"
    policy = intelligence.TaxonomyPolicy.model_validate(yaml.safe_load(path.read_text()))
    assert policy.version == 1
    assert intelligence.analysis_version(policy.version) == "tax1-alg1"


def test_taxonomy_rejects_dangling_references() -> None:
    base = {
        "policy": "taxonomy",
        "version": 1,
        "status": "t",
        "source": "t",
        "geographies": [],
        "page_types": [
            {"id": "other", "label": "o", "path_patterns": ["^/"], "intent": "unknown",
             "commercial_importance": 0}
        ],
        "default_page_type": "other",
    }  # fmt: skip
    with pytest.raises(ValidationError):
        intelligence.TaxonomyPolicy.model_validate(
            {**base, "markets": [{"id": "a", "label": "A", "parent": "missing"}]}
        )
    with pytest.raises(ValidationError):
        intelligence.TaxonomyPolicy.model_validate(
            {**base, "markets": [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]}
        )


def test_taxonomy_rejects_parent_cycles() -> None:
    base = {
        "policy": "taxonomy",
        "version": 1,
        "status": "t",
        "source": "t",
        "geographies": [],
        "page_types": [
            {"id": "other", "label": "o", "path_patterns": ["^/"], "intent": "unknown",
             "commercial_importance": 0}
        ],
        "default_page_type": "other",
    }  # fmt: skip
    cycle = [{"id": "a", "label": "A", "parent": "b"}, {"id": "b", "label": "B", "parent": "a"}]
    with pytest.raises(ValidationError, match="cycle"):
        intelligence.TaxonomyPolicy.model_validate({**base, "markets": cycle})


def test_link_plan_transitions_are_total_and_terminal_states_are_final() -> None:
    from ken_contracts.recommendations import ALLOWED_TRANSITIONS, LinkPlanStatus

    assert set(ALLOWED_TRANSITIONS) == set(LinkPlanStatus)
    for terminal in ("rejected", "superseded", "expired", "rolled_back"):
        assert ALLOWED_TRANSITIONS[LinkPlanStatus(terminal)] == frozenset()
    # An approval is never resurrected: nothing leads back to approved except awaiting_approval.
    sources = {
        s for s, targets in ALLOWED_TRANSITIONS.items() if LinkPlanStatus.APPROVED in targets
    }
    assert sources == {LinkPlanStatus.AWAITING_APPROVAL}


def test_score_breakdown_identities_are_enforced() -> None:
    from ken_contracts.recommendations import ScoreBreakdown, ScoreComponent

    comp = ScoreComponent(name="semantic", value=0.5, weight=0.3, contribution=0.15)
    ok = ScoreBreakdown(
        components=(comp,), relevance=0.15, factors={"placement_quality": 0.8}, total=0.12,
        policy_name="ranking", policy_version=1,
    )  # fmt: skip
    assert ok.total == 0.12
    with pytest.raises(ValidationError):
        ScoreComponent(name="vibes", value=0.5, weight=0.3, contribution=0.15)
    with pytest.raises(ValidationError):
        ScoreComponent(name="semantic", value=0.5, weight=0.3, contribution=0.2)
    with pytest.raises(ValidationError):
        ScoreBreakdown(
            components=(comp,), relevance=0.15, factors={}, total=0.5,
            policy_name="ranking", policy_version=1,
        )  # fmt: skip


def test_recommendation_policy_file_validates() -> None:
    from pathlib import Path

    import yaml

    from ken_contracts.recommendations import RankingPolicy, RecommendationPolicy

    root = Path(__file__).resolve().parents[2]
    policy = RecommendationPolicy.model_validate(
        yaml.safe_load((root / "policies/recommendation/recommendation.v1.yaml").read_text())
    )
    ranking = RankingPolicy.model_validate(
        yaml.safe_load((root / policy.ranking_policy).read_text())
    )
    assert policy.selection.min_relevance <= policy.selection.min_score
    assert ranking.version == 1


def test_agent_policy_file_validates_and_tools_are_allowlisted() -> None:
    import yaml

    from ken_contracts.agents import TOOL_PERMISSIONS, AgentPolicy, ToolName, ToolPermission

    path = Path(__file__).resolve().parents[2] / "policies" / "agents" / "agents.v1.yaml"
    raw = yaml.safe_load(path.read_text("utf-8"))
    policy = AgentPolicy.model_validate(raw)
    assert policy.version == 1
    assert set(TOOL_PERMISSIONS) == set(ToolName)
    writers = {t for t, p in TOOL_PERMISSIONS.items() if p is not ToolPermission.READ}
    assert writers == {ToolName.HAND_OFF_PLANS}  # agents never insert, approve or deploy


def test_llm_boundary_models_are_bounded_and_strict() -> None:
    from pydantic import ValidationError

    from ken_contracts.agents import (
        MAX_CANDIDATES_FOR_MODEL,
        CriticAssessment,
        PlannerSelection,
    )

    with pytest.raises(ValidationError):
        PlannerSelection.model_validate({"selected": ["a"], "anchor_text": "invented"})
    with pytest.raises(ValidationError):
        PlannerSelection(selected=[f"c{i}" for i in range(MAX_CANDIDATES_FOR_MODEL + 1)])
    for bad_ref in ("c1x", "1", "c123", "2b1e6a9c-0000-0000-0000-000000000000", "/reports/x"):
        with pytest.raises(ValidationError):
            PlannerSelection(selected=[bad_ref])
    with pytest.raises(ValidationError):
        PlannerSelection(selected=["c1", "c1"])
    with pytest.raises(ValidationError):
        CriticAssessment.model_validate(
            {
                "critiques": [
                    {"ref": "c0", "verdict": "accept"},
                    {"ref": "c0", "verdict": "reject"},
                ]
            }
        )
    assert PlannerSelection(selected=["c0", "c9", "c10"]).selected == ["c0", "c9", "c10"]
    with pytest.raises(ValidationError):
        CriticAssessment.model_validate(
            {"critiques": [{"ref": "c0", "verdict": "approve", "reasons": []}]}
        )
    with pytest.raises(ValidationError):
        CriticAssessment.model_validate(
            {"critiques": [{"ref": "c0", "verdict": "accept", "explanation": "x" * 301}]}
        )
