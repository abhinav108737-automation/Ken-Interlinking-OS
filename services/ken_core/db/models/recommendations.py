"""Recommendation engine tables (Phase 3, migration 0004). FROZEN (orchestrator).

``link_plans``             a proposed contextual link. CONTENT IMMUTABLE: a GENERIC trigger
                           compares ``to_jsonb(NEW) - 'status' - 'updated_at'`` with OLD and
                           rejects any difference (so future columns are immutable too), and
                           rejects DELETE. At most one OPEN plan per (source, target) and per
                           (source, block context hash, anchor start) -- partial unique
                           indexes -- and a plan can be superseded at most once.
``link_plan_transitions``  APPEND-ONLY status history (PRD v1.1 Appendix C lifecycle).
                           Migration 0007 adds ``txid`` and a DEFERRABLE constraint trigger on
                           ``link_plans``: a status change must be in ALLOWED_TRANSITIONS AND
                           have a matching transition row written in the same transaction.
``link_candidates``        APPEND-ONLY audit of every retrieved candidate of a run, kept or
                           rejected (with the rejection code), for explainability and evals.
``opportunities``          derived page-level signals (orphan / under-linked / capacity);
                           replaced per run.
``page_modules`` /         a related-reports module proposed for a page, and its ranked
``page_module_items``      items (migration 0012). Derived and replaced per run like
                           ``opportunities``, so no status and no transition history: see
                           :class:`PageModule`.

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
    OPEN_STATUSES,
    CandidateRejection,
    LinkPlanStatus,
    ModuleKind,
    OpportunityKind,
)
from ken_core.db.base import Base
from ken_core.db.models.inventory import _one_of
from ken_core.db.types import Xid8
from ken_core.priority import MAX_TIER, MIN_TIER, PriorityType

OPEN_PLAN_STATUSES: Final = tuple(s for s in LinkPlanStatus if s in OPEN_STATUSES)
"""Statuses in which a plan still occupies its (source, target) and its anchor span."""

PLAN_MUTABLE_COLUMNS: Final = ("status", "updated_at")
"""The ONLY link_plans columns an UPDATE may change (enforced by trigger in 0004)."""

APPEND_ONLY_RECOMMENDATION_TABLES: Final = ("link_plan_transitions", "link_candidates")
"""Rows these tables hold may never be UPDATEd or DELETEd (``ken_reject_mutation``)."""

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
    target_page_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
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
    calibration_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_codes: Mapped[list[str]] = _jsonb("[]")
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ranking_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning_model: Mapped[str | None] = mapped_column(Text, nullable=True)
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
        ForeignKeyConstraint(
            ["target_page_id", "target_page_version"],
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
        # ...nor two open plans competing for the same anchor start in the same block TEXT
        # (keyed by context hash, so it holds across source versions). Overlapping spans
        # are additionally rejected in code.
        Index(
            "uq_link_plans_open_span",
            "source_page_id",
            "context_hash",
            "start_offset",
            unique=True,
            postgresql_where=text(f"status IN ({_OPEN})"),
        ),
        # A plan is superseded by at most one edit.
        Index(
            "uq_link_plans_supersedes",
            "supersedes",
            unique=True,
            postgresql_where=text("supersedes IS NOT NULL"),
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
    txid: Mapped[str] = mapped_column(
        Xid8(),
        nullable=False,
        server_default=text("pg_current_xact_id()"),
        deferred=True,  # never written (server default) nor loaded: compared in SQL only
    )
    """Migration 0007: the writing transaction's id. The DEFERRABLE status-integrity trigger
    on ``link_plans`` requires a (plan, from, to, txid) row for every status change."""

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
    attribution: Mapped[list[Any]] = _jsonb("[]")
    """Migration 0010: a jsonb ARRAY of ``CandidateAttribution`` -- one entry per channel that
    retrieved this candidate, with its raw AND normalised value and its rank in that channel.
    Deliberately per channel: a single opaque score cannot be audited, re-derived or ablated
    (directive section 6)."""
    graph_signals: Mapped[dict[str, Any]] = _jsonb("{}")
    """Migration 0010: the TARGET's ``GraphSignals`` as they were when it was retrieved, each
    signal separately. Snapshotted here rather than joined later because the graph moves."""
    created_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        UniqueConstraint("run_id", "source_page_id", "target_page_id"),
        CheckConstraint(
            "rejection IS NULL OR " + _one_of("rejection", CandidateRejection),
            name="rejection_valid",
        ),
        # A kept candidate became a plan; a rejected one did not.
        CheckConstraint("(rejection IS NULL) = (plan_id IS NOT NULL)", name="kept_iff_planned"),
        CheckConstraint("jsonb_typeof(attribution) = 'array'", name="attribution_array"),
        CheckConstraint("jsonb_typeof(graph_signals) = 'object'", name="graph_signals_object"),
        Index("ix_link_candidates_source_page_id", "source_page_id"),
    )


class PageModule(Base):
    """A related-reports module proposed for one page (migration 0012, ADR-0027 §1b).

    DERIVED and REPLACED PER RUN, like ``opportunities``, which is why it carries no status
    and no transition history: there is no reviewer edit to preserve, and approving a module
    would mean composing it onto a page -- a decision that waits on the CMS surface
    (NEXT_STEPS item 12). Items cascade from the module because they are the module; half a
    module is not a smaller module, it is a wrong one.
    """

    __tablename__ = "page_modules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_runs.id", ondelete="RESTRICT"), nullable=False
    )
    page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    page_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    market_core: Mapped[str] = mapped_column(Text, nullable=False)
    geography_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    geography_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_version: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["page_id", "page_version"],
            ["page_versions.page_id", "page_versions.version"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("page_id", "kind"),
        CheckConstraint(_one_of("kind", ModuleKind), name="kind_valid"),
        CheckConstraint("length(market_core) > 0", name="market_core_present"),
        Index("ix_page_modules_run_id", "run_id"),
    )


class PageModuleItem(Base):
    __tablename__ = "page_module_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    module_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("page_modules.id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    target_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    target_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    geography_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_codes: Mapped[list[str]] = _jsonb("[]")

    __table_args__ = (
        UniqueConstraint("module_id", "rank"),
        UniqueConstraint("module_id", "target_page_id"),
        CheckConstraint("rank >= 1", name="rank_positive"),
        CheckConstraint("jsonb_typeof(reason_codes) = 'array'", name="reason_codes_array"),
        Index("ix_page_module_items_target_page_id", "target_page_id"),
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


class PagePriority(Base):
    """A governed business-priority designation for one page (migration 0013).

    Business Phase 2A-1, knowledge base §5.1. UNLIKE every other table in this module it is
    neither derived nor replaced per run: a designation is EDITORIAL INPUT, written by a
    person and owned by one. So it carries its own ``owner``, ``reason`` and created/updated
    audit pair, and it is mutable -- an owner revises a tier or retires a designation.

    It carries no status column and no transition history. The only question a reader asks --
    "was this in force at instant X?" -- is answered by arithmetic on the row's own
    timestamps, so there is no lifecycle for a reviewer to advance and nothing for a
    transition table to record.

    **Time-bound by the row, never by a sweep.** Validity is the half-open interval
    ``[effective_from, min(effective_to, retired_at))``. Retirement records an INSTANT, so a
    designation retired today still resolves for a date on which it was in force -- a boolean
    ``active`` could only answer "is it live now" and would give every historical question
    today's answer (:mod:`ken_services.recommendations.priority`).

    Two designations of one (page, type) may not be in force at the same instant. That is a
    NON-OVERLAP rule, enforced by the ``page_priorities_no_overlap`` trigger, not a unique
    index: uniqueness would forbid scheduling a replacement for a future date and would let an
    expired row block its own successor.
    """

    __tablename__ = "page_priorities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    priority_type: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[datetime] = _ts()
    effective_to: Mapped[datetime | None] = _ts(nullable=True)
    retired_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = _ts(default_now=True)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(_one_of("priority_type", PriorityType), name="priority_type_valid"),
        CheckConstraint(f"tier >= {MIN_TIER} AND tier <= {MAX_TIER}", name="tier_range"),
        CheckConstraint("length(btrim(reason)) > 0", name="reason_present"),
        CheckConstraint("length(btrim(owner)) > 0", name="owner_present"),
        CheckConstraint("length(btrim(created_by)) > 0", name="created_by_present"),
        CheckConstraint("length(btrim(updated_by)) > 0", name="updated_by_present"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from", name="window_ordered"
        ),
        Index("ix_page_priorities_page_id", "page_id"),
        Index("ix_page_priorities_page_id_type", "page_id", "priority_type"),
    )
