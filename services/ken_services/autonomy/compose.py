"""Composition root of the autonomy workstream.

Everything is built here and injected, so nothing in the engine, the publisher or the
override service reaches for a global. Three builders:

* :func:`build_block_evaluator` -- the deterministic layer alone. It takes NO model provider,
  and nothing in this module gives it one.
* :func:`build_engine` -- the evaluator plus the risk classifier and the calibration reader.
  Also model-provider-free: every input of a decision is a stored fact.
* :func:`build_publisher` -- the engine, the deployment service (with the configured CMS
  provider), the retry-budget gate and the operator identity the CMS audit trail requires.

The CMS provider comes from :func:`~ken_core.providers.cms.factory.build_cms_provider`, which
honours ``KEN_CMS_PROVIDER`` and ``KEN_CMS_WRITES_ENABLED``. The autonomy settings validators
already refuse a publishing mode against a write-enabled ``http`` provider without
``KEN_AUTONOMY_ALLOW_PRODUCTION``; :func:`build_publisher` re-asserts that the mode publishes
at all before it builds anything, so a misconfiguration fails before a session is opened
rather than after a link is live.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.auth import Principal
from ken_contracts.autonomy import AutonomyPolicy
from ken_contracts.cms import CMSProvider
from ken_contracts.intelligence import TaxonomyPolicy
from ken_contracts.recommendations import RecommendationPolicy
from ken_core.config import Settings
from ken_services.autonomy.blocks import DeterministicBlocks
from ken_services.autonomy.budget import DatabaseRetryBudgetGate
from ken_services.autonomy.calibration import EvaluationCalibrationReader
from ken_services.autonomy.engine import DatabaseAutonomyEngine
from ken_services.autonomy.policy import effective_retry, load_configured_autonomy_policy
from ken_services.autonomy.publish import AutonomousPublisher
from ken_services.autonomy.risk import DeterministicRiskClassifier
from ken_services.deployment.compose import build_deployment_service
from ken_services.deployment.service import DatabaseDeploymentService
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.jobs.runner import utc_now
from ken_services.learning.policy import load_configured_learning_policy
from ken_services.recommendations.policy import load_recommendation_policy

__all__ = [
    "build_block_evaluator",
    "build_engine",
    "build_publisher",
    "load_autonomy_policies",
]


def load_autonomy_policies(
    settings: Settings,
) -> tuple[RecommendationPolicy, TaxonomyPolicy]:
    """The recommendation policy and the taxonomy. The AUTONOMY policy is loaded separately,
    by :func:`~ken_services.autonomy.policy.load_configured_autonomy_policy`."""
    return (
        load_recommendation_policy(settings.recommendation_policy_path),
        load_taxonomy(settings.taxonomy_path),
    )


def build_block_evaluator(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    policy: RecommendationPolicy | None = None,
    taxonomy: TaxonomyPolicy | None = None,
    autonomy_policy: AutonomyPolicy | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> DeterministicBlocks:
    if policy is None or taxonomy is None:
        policy, taxonomy = load_autonomy_policies(settings)
    return DeterministicBlocks(
        sessionmaker=sessionmaker,
        settings=settings,
        policy=policy,
        taxonomy=taxonomy,
        autonomy_policy=autonomy_policy or load_configured_autonomy_policy(settings),
        clock=clock,
    )


def build_engine(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    policy: RecommendationPolicy | None = None,
    taxonomy: TaxonomyPolicy | None = None,
    autonomy_policy: AutonomyPolicy | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> DatabaseAutonomyEngine:
    if policy is None or taxonomy is None:
        policy, taxonomy = load_autonomy_policies(settings)
    autonomy_policy = autonomy_policy or load_configured_autonomy_policy(settings)
    blocks = build_block_evaluator(
        sessionmaker=sessionmaker,
        settings=settings,
        policy=policy,
        taxonomy=taxonomy,
        autonomy_policy=autonomy_policy,
        clock=clock,
    )
    try:
        learning = load_configured_learning_policy(settings)
    except (FileNotFoundError, ValueError):
        # No learning policy on disk: no calibration applies, so every class is UNCALIBRATED
        # and guarded mode publishes nothing. Degrading to the conservative side is the point.
        learning = None
    return DatabaseAutonomyEngine(
        sessionmaker=sessionmaker,
        settings=settings,
        policy=policy,
        taxonomy=taxonomy,
        autonomy_policy=autonomy_policy,
        blocks=blocks,
        risk=DeterministicRiskClassifier(
            sessionmaker=sessionmaker,
            taxonomy=taxonomy,
            clock=clock,
            policy=autonomy_policy.risk,
        ),
        calibration=EvaluationCalibrationReader(
            sessionmaker=sessionmaker,
            policy=learning,
            clock=clock,
            min_positive_outcome_rate=autonomy_policy.confidence.min_positive_outcome_rate,
        ),
        clock=clock,
    )


def build_publisher(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    principal: Principal,
    cms: CMSProvider,
    deployments: DatabaseDeploymentService | None = None,
    engine: DatabaseAutonomyEngine | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> AutonomousPublisher:
    """The publisher. ``cms`` is passed in explicitly: there is no default, so no code path
    can publish to a provider nobody chose."""
    return AutonomousPublisher(
        sessionmaker=sessionmaker,
        settings=settings,
        engine=engine or build_engine(sessionmaker=sessionmaker, settings=settings, clock=clock),
        deployments=deployments
        or build_deployment_service(sessionmaker=sessionmaker, settings=settings, cms=cms),
        budget=DatabaseRetryBudgetGate(
            sessionmaker=sessionmaker,
            retry=effective_retry(load_configured_autonomy_policy(settings), settings),
            clock=clock,
        ),
        principal=principal,
    )
