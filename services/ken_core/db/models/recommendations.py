"""Recommendation engine tables (Phase 3, migration 0004). FROZEN (orchestrator).

``link_plans``             a proposed contextual link. CONTENT IMMUTABLE: a trigger rejects any
                           UPDATE that changes a column other than ``status`` / ``updated_at``.
                           At most one OPEN plan per (source page, target page), and per
                           (source page version, block, anchor start): enforced by partial
                           unique indexes, so a re-run can never duplicate a recommendation.
``link_plan_transitions``  APPEND-ONLY status history (PRD v1.1 Appendix C lifecycle).
``link_candidates``        APPEND-ONLY audit of every retrieved candidate of a run, kept or
                           rejected (with the rejection code), for explainability and evals.
``opportunities``          derived page-level signals (orphan / under-linked / capacity);
                           replaced per run.

Migration 0004 also adds ``page_versions.search_vector`` (a STORED generated
``tsvector`` with a GIN index) for the lexical retrieval channel -- built-in
PostgreSQL full-text search, no new infrastructure.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ken_contracts.recommendations import (
    CandidateRejection,
    LinkPlanStatus,
    OpportunityKind,
)
from ken_core.db.base import Base
from ken_core.db.models.inventory import _one_of

OPEN_PLAN_STATUSES: Final = (
    LinkPlanStatus.PROPOSED,
    LinkPlanStatus.CRITIQUED,
    LinkPlanStatus.POLICY_CHECKED,
    LinkPlanStatus.AWAITING_APPROVAL,
    LinkPlanStatus.APPROVED,
    LinkPlanStatus.VALIDATED,
)
"""Statuses in which a plan still occupies its (source, target) and its anchor span."""

PLAN_MUTABLE_COLUMNS: Final = ("status", "updated_at")
"""The ONLY link_plans columns an UPDATE may change (enforced by trigger in 0004)."""

APPEND_ONLY_RECOMMENDATION_TABLES: Final = ("link_plan_transitions", "link_candidates")

_OPEN: Final = ", ".join(f"'{status.value}'" for status in OPEN_PLAN_STATUSES)


def _ts(*, nullable: bool = False, default_now: bool = False) -> Any:
    return mapped_column(
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if default_now else None,
    )


def _jsonb(default: str) -> Any:
    return mapped_column(JSONB, nullable=False, server_default=text(f"'{default}'::jsonb"))


class LinkPlan(Base):
    __tablename__ = "link_plans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_runs.id", ondelete="RESTRICT"), nullable=True
    )
    source_page_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_page_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    target_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    target_page_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    target_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    relationship_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    channels: Mapped[list[str]] = _jsonb("[]")
    anchor_text: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_source: Mapped[str] = mapped_column(Text, nullable=False)
    block_id: Mapped[str] = mapped_column(Text, nullable=False)
    block_kind: Mapped[str] = mapped_column(Text, nullable=False)
    occurrence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    context_hash: Mapped[str] = mapped_column(Text, nullable=False)
    preview: Mapped[dict[str, str]] = _jsonb("{}")
    score: Mapped[dict[str, Any]] = _jsonb("{}")
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason_codes: Mapped[list[str]] = _jsonb("[]")
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ranking_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_version: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("link_plans.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_page_id", "source_page_version"],
            ["page_versions.page_id", "page_versions.version"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(_one_of("status", LinkPlanStatus), name="status_valid"),
        CheckConstraint("source_page_id <> target_page_id", name="not_self"),
        CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="offsets_valid"),
        CheckConstraint("occurrence_index >= 0", name="occurrence_non_negative"),
        CheckConstraint("total_score >= 0 AND total_score <= 1", name="score_range"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("context_hash ~ '^[0-9a-f]{64}$'", name="context_hash_sha256"),
        CheckConstraint("length(anchor_text) BETWEEN 1 AND 120", name="anchor_length"),
        # Never two open recommendations for the same pair...
        Index(
            "uq_link_plans_open_pair",
            "source_page_id",
            "target_page_id",
            unique=True,
            postgresql_where=text(f"status IN ({_OPEN})"),
        ),
        # ...nor two open plans competing for the same anchor span.
        Index(
            "uq_link_plans_open_span",
            "source_page_id",
            "source_page_version",
            "block_id",
            "start_offset",
            unique=True,
            postgresql_where=text(f"status IN ({_OPEN})"),
        ),
        Index("ix_link_plans_status_total_score", "status", text("total_score DESC")),
        Index("ix_link_plans_target_page_id", "target_page_id"),
    )


class LinkPlanTransition(Base):
    __tablename__ = "link_plan_transitions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("link_plans.id", ondelete="RESTRICT"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        CheckConstraint(_one_of("to_status", LinkPlanStatus), name="to_status_valid"),
        CheckConstraint(
            "from_status IS NULL OR " + _one_of("from_status", LinkPlanStatus),
            name="from_status_valid",
        ),
        CheckConstraint("length(actor) > 0", name="actor_present"),
        Index("ix_link_plan_transitions_plan_id_id", "plan_id", "id"),
    )


class LinkCandidate(Base):
    __tablename__ = "link_candidates"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_runs.id", ondelete="RESTRICT"), nullable=False
    )
    source_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    source_page_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    target_page_version: Mapped[int] = mapped_column(Integer, nullable=False)
    channels: Mapped[list[str]] = _jsonb("[]")
    channel_scores: Mapped[dict[str, float]] = _jsonb("{}")
    total_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rejection: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("link_plans.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        UniqueConstraint("run_id", "source_page_id", "target_page_id"),
        CheckConstraint(
            "rejection IS NULL OR " + _one_of("rejection", CandidateRejection),
            name="rejection_valid",
        ),
        # A kept candidate became a plan; a rejected one did not.
        CheckConstraint("(rejection IS NULL) = (plan_id IS NOT NULL)", name="kept_iff_planned"),
        Index("ix_link_candidates_source_page_id", "source_page_id"),
    )


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_runs.id", ondelete="RESTRICT"), nullable=False
    )
    page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = _jsonb("{}")
    computed_at: Mapped[datetime] = _ts()

    __table_args__ = (
        UniqueConstraint("page_id", "kind"),
        CheckConstraint(_one_of("kind", OpportunityKind), name="kind_valid"),
        CheckConstraint("score >= 0 AND score <= 1", name="score_range"),
    )
