"""Autonomy tables (migration 0010; docs/adr/ADR-0018-autonomy-first-publishing.md s4/s6).

``autonomy_decisions``   APPEND-ONLY. One row per evaluation of one plan: the decision, the
                        mode in force, the risk tier, the calibrated confidence, the critic
                        outcome, the policy version, the deterministic block codes that
                        FIRED, whether rollback was available, and -- in ``shadow`` -- what
                        would have happened. This is what Phase 9 calibration and Phase 8
                        learning read, so it is never edited and never deleted.
                        ``txid`` plus the DEFERRED ``ken_autonomy_decision_guard()`` require
                        the matching ``link_plan_transitions`` row to be written by the SAME
                        transaction whenever the decision moves the plan: without it a
                        caller could insert a decision in one transaction and move the plan
                        in another, and the CHECKs below only test a row's SHAPE (the 0007 /
                        0009 pattern).

``publish_attempts``    APPEND-ONLY. The retry budget and cool-down per (source, target)
                        that make repeated unsafe retries impossible (ADR-0018 s4). It is a
                        separate table rather than columns on ``deployments`` because the
                        budget belongs to the PAIR and must survive every plan that ever
                        targeted it: a superseded plan, a rolled-back deployment and a fresh
                        proposal all draw on the same budget, and attaching it to one
                        deployment row would reset it exactly when it matters.

Every guard here is a trigger, so they defend against application bugs and not against a DB
superuser (``session_replication_role = replica`` disables all of them). No
``SECURITY DEFINER`` function exists in this schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ken_contracts.autonomy import (
    MAX_OVERRIDE_REASON_CHARS,
    AutonomyDecision,
    AutonomyMode,
    CriticOutcome,
    RiskTier,
    VerificationProfile,
)
from ken_contracts.recommendations import ACTOR_PATTERN
from ken_core.db.base import Base
from ken_core.db.models.inventory import _one_of
from ken_core.db.types import Xid8

APPEND_ONLY_AUTONOMY_TABLES: Final = ("autonomy_decisions", "publish_attempts")

MAX_ENGINE_VERSION_CHARS: Final = 64
MAX_FAILURE_CODE_CHARS: Final = 64
MAX_CALIBRATION_VERSION_CHARS: Final = 64


def _ts(*, nullable: bool = False, default_now: bool = False) -> Any:
    return mapped_column(
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if default_now else None,
    )


class AutonomyDecisionRow(Base):
    __tablename__ = "autonomy_decisions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("link_plans.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    risk_tier: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    calibration_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    """NULL = RAW confidence. No threshold may treat raw as calibrated (ADR-0011)."""
    critic_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    critic_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    blocks: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    """The deterministic ``BlockCode``s that fired, as a jsonb ARRAY of strings. A non-empty
    array forces ``decision = 'block'`` (a CHECK below), so the record can never claim a
    publish over a fired block."""
    rollback_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    verification_profile: Mapped[str] = mapped_column(Text, nullable=False)
    would_have_decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    """SHADOW only: the decision that WOULD have been taken (ADR-0018 s6)."""
    engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    job_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_runs.id", ondelete="RESTRICT"), nullable=True
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    """``system:<component>`` on the autonomous path, ``user:<id>`` for an operator override."""
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Present exactly when a ``user:`` actor decided: an override records WHY (ADR-0018 s5)."""
    txid: Mapped[str] = mapped_column(
        Xid8(),
        nullable=False,
        server_default=text("pg_current_xact_id()"),
        deferred=True,  # never written (server default) nor loaded: compared in SQL only
    )
    """Migration 0010: the writing transaction's id. The DEFERRABLE
    ``ken_autonomy_decision_guard()`` requires the matching ``link_plan_transitions`` row to
    carry the same ``txid``, so a decision and the plan move it claims cannot be split
    across two transactions."""
    evaluated_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        CheckConstraint(_one_of("decision", AutonomyDecision), name="decision_valid"),
        CheckConstraint(_one_of("mode", AutonomyMode), name="mode_valid"),
        CheckConstraint(_one_of("risk_tier", RiskTier), name="risk_tier_valid"),
        CheckConstraint(_one_of("critic_outcome", CriticOutcome), name="critic_outcome_valid"),
        CheckConstraint(
            "critic_verdict IS NULL OR critic_verdict IN ('accept', 'reject', 'uncertain')",
            name="critic_verdict_valid",
        ),
        CheckConstraint(
            _one_of("verification_profile", VerificationProfile), name="verification_profile_valid"
        ),
        CheckConstraint(
            "would_have_decision IS NULL OR " + _one_of("would_have_decision", AutonomyDecision),
            name="would_have_decision_valid",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("policy_version >= 1", name="policy_version_positive"),
        CheckConstraint("jsonb_typeof(blocks) = 'array'", name="blocks_array"),
        # A fired block can never coexist with a publish.
        CheckConstraint(
            "jsonb_array_length(blocks) = 0 OR decision = 'block'", name="blocks_force_block"
        ),
        # NO_ACTION is an earlier outcome and is never recorded as a decision (ADR-0018 1).
        CheckConstraint("critic_outcome <> 'no_action'", name="no_action_never_decided"),
        # Nothing is published that cannot be undone.
        CheckConstraint(
            "rollback_available OR decision = 'block'", name="no_publish_without_rollback"
        ),
        # The strict profile is exactly the extra-verification decision.
        CheckConstraint(
            "(decision = 'auto_publish_with_extra_verification') "
            "<= (verification_profile = 'strict')",
            name="extra_verification_is_strict",
        ),
        # Shadow records a counterfactual and publishes nothing.
        CheckConstraint(
            "(mode = 'shadow') = (would_have_decision IS NOT NULL)",
            name="counterfactual_iff_shadow",
        ),
        CheckConstraint("mode <> 'shadow' OR decision = 'block'", name="shadow_never_publishes"),
        CheckConstraint(f"actor ~ '{ACTOR_PATTERN}'", name="actor_shape"),
        # An agent may neither decide a publish nor block one (ADR-0012 stands).
        CheckConstraint("actor NOT LIKE 'agent:%'", name="actor_is_not_an_agent"),
        CheckConstraint(
            "(actor LIKE 'user:%') = (override_reason IS NOT NULL)",
            name="override_records_a_reason",
        ),
        CheckConstraint(
            f"override_reason IS NULL OR length(override_reason) BETWEEN 1 AND "
            f"{MAX_OVERRIDE_REASON_CHARS}",
            name="override_reason_bounded",
        ),
        CheckConstraint(
            f"length(engine_version) BETWEEN 1 AND {MAX_ENGINE_VERSION_CHARS}",
            name="engine_version_bounded",
        ),
        CheckConstraint(
            "calibration_version IS NULL OR length(calibration_version) BETWEEN 1 AND "
            f"{MAX_CALIBRATION_VERSION_CHARS}",
            name="calibration_version_bounded",
        ),
        Index("ix_autonomy_decisions_plan_id_id", "plan_id", "id"),
        Index("ix_autonomy_decisions_decision_evaluated_at", "decision", "evaluated_at"),
        Index(
            "ix_autonomy_decisions_blocks",
            "blocks",
            postgresql_using="gin",
            postgresql_ops={"blocks": "jsonb_path_ops"},
        ),
    )


class PublishAttempt(Base):
    __tablename__ = "publish_attempts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    target_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("link_plans.id", ondelete="RESTRICT"), nullable=False
    )
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("deployments.id", ondelete="RESTRICT"), nullable=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    """1-based, per (source, target) pair -- NOT per plan: the budget belongs to the pair."""
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    verification_profile: Mapped[str] = mapped_column(Text, nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rolled_back: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    failure_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    cooldown_until: Mapped[datetime | None] = _ts(nullable=True)
    """While this is in the future nothing republishes the pair, whatever the decision says."""
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        CheckConstraint("source_page_id <> target_page_id", name="not_self"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("max_attempts BETWEEN 1 AND 10", name="max_attempts_bounded"),
        CheckConstraint("attempt <= max_attempts", name="attempt_within_budget"),
        CheckConstraint(_one_of("decision", AutonomyDecision), name="decision_valid"),
        CheckConstraint(_one_of("mode", AutonomyMode), name="mode_valid"),
        CheckConstraint(
            _one_of("verification_profile", VerificationProfile), name="verification_profile_valid"
        ),
        # Shadow publishes nothing, so it can never record an attempt.
        CheckConstraint("mode <> 'shadow'", name="shadow_never_attempts"),
        CheckConstraint("decision <> 'block'", name="a_block_is_never_attempted"),
        CheckConstraint("succeeded OR failure_code IS NOT NULL", name="failure_has_a_code"),
        CheckConstraint(
            f"failure_code IS NULL OR length(failure_code) BETWEEN 1 AND {MAX_FAILURE_CODE_CHARS}",
            name="failure_code_bounded",
        ),
        CheckConstraint("NOT rolled_back OR NOT succeeded", name="rolled_back_is_not_success"),
        CheckConstraint(f"actor ~ '{ACTOR_PATTERN}'", name="actor_shape"),
        CheckConstraint("actor NOT LIKE 'agent:%'", name="actor_is_not_an_agent"),
        # One row per (pair, attempt): the budget cannot be spent twice by a race, because the
        # second INSERT of the same attempt number is refused rather than merged.
        Index(
            "uq_publish_attempts_pair_attempt",
            "source_page_id",
            "target_page_id",
            "attempt",
            unique=True,
        ),
        Index("ix_publish_attempts_plan_id_id", "plan_id", "id"),
        Index(
            "ix_publish_attempts_cooldown",
            "source_page_id",
            "target_page_id",
            "cooldown_until",
        ),
    )
