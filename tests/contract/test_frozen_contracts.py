"""Contract tests: the frozen W0 shapes, asserted.

A freeze without tests is a convention. These tests fail the build if a
frozen contract drifts, which is what lets parallel workstreams rely on it.
"""

from __future__ import annotations

import inspect
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from ken_contracts import analytics, cms, common, errors, events, health, model
from ken_contracts.common import AsyncClosable, ContractModel

CONTRACT_MODULES = [analytics, cms, common, errors, events, health, model]


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
    "protocol", [model.ModelProvider, cms.CMSProvider, analytics.AnalyticsProvider]
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
