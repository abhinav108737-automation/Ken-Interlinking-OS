"""Shared builders for the autonomy unit tests: a clean :class:`PlanFacts` and the inputs of
one decision, so each test changes exactly the fact it is about."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from ken_contracts.agents import CriticVerdict
from ken_contracts.autonomy import CriticOutcome, RiskTier
from ken_core.config import Settings
from ken_services.autonomy.engine import _Inputs
from ken_services.autonomy.facts import PlanIdentity
from ken_services.autonomy.protocol import CalibratedConfidence, PlanFacts
from tests.conftest import make_settings

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
SOURCE = uuid.UUID("11111111-1111-4111-8111-111111111111")
TARGET = uuid.UUID("22222222-2222-4222-8222-222222222222")
PLAN = uuid.UUID("33333333-3333-4333-8333-333333333333")


def clean_facts(**overrides: Any) -> PlanFacts:
    """A plan that fires NO block code: every fact is the safe value."""
    facts = PlanFacts(
        plan_id=PLAN,
        source_page_id=SOURCE,
        target_page_id=TARGET,
        source_page_version=3,
        source_current_version=3,
        target_page_version=2,
        target_current_version=2,
        source_protected=False,
        target_protected=False,
        target_published=True,
        target_gone=False,
        target_http_status=200,
        target_canonical_ok=True,
        target_redirect_supported=True,
        target_noindex=False,
        target_thin=False,
        source_page_type="report",
        target_page_type="report",
        page_types_compatible=True,
        destination_internal=True,
        destination_allowed_by_policy=True,
        already_linked=False,
        duplicate_allowed=False,
        anchor_valid=True,
        rollback_metadata_present=True,
        vector_fresh=True,
        policy_version=1,
        confidence=0.9,
        calibration_version="autonomy-calib-1:run",
    )
    return replace(facts, **overrides) if overrides else facts


def identity(**overrides: Any) -> PlanIdentity:
    base = PlanIdentity(
        plan_id=PLAN,
        source_page_id=SOURCE,
        target_page_id=TARGET,
        status="policy_checked",
        anchor_source="target_topic",
        relationship_type="related_market",
        channels=("semantic", "taxonomy"),
        source_url="https://site.test/a",
        target_url="https://site.test/b",
        anchor_text="lithium-ion batteries",
        reasoning_model=None,
    )
    return replace(base, **overrides) if overrides else base


def inputs(
    *,
    facts: PlanFacts | None = None,
    blocks: tuple[Any, ...] = (),
    critic: CriticOutcome = CriticOutcome.ACCEPT,
    verdict: CriticVerdict | None = CriticVerdict.ACCEPT,
    confidence: float = 0.9,
    calibration_version: str | None = "autonomy-calib-1:run",
    tier: RiskTier = RiskTier.LOW,
    extra_verification: bool = False,
) -> _Inputs:
    resolved = facts if facts is not None else clean_facts()
    return _Inputs(
        facts=resolved,
        identity=identity(),
        blocks=blocks,
        critic=critic,
        verdict=verdict,
        calibrated=CalibratedConfidence(value=confidence, calibration_version=calibration_version),
        tier=tier,
        extra_verification=extra_verification,
    )


def settings(**overrides: Any) -> Settings:
    built: Settings = make_settings(**overrides)
    return built
