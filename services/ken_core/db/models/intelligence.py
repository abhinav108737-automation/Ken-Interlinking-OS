"""Job runs and content-intelligence tables (Phase 2, migration 0003). FROZEN (orchestrator).

``job_runs``           generic run record for every batch job from Phase 2 on
``page_analyses``      IMMUTABLE deterministic analysis per (page version, analysis version)
``page_embeddings``    IMMUTABLE page vector per (page version, model, recipe version)
``page_relationships`` derived typed page-to-page edges; REPLACED by each analysis run for every
                       edge touching an analysed page (as source OR target). Each edge records
                       both endpoint versions; readers treat an edge whose endpoint version is not
                       current as stale. No table may foreign-key ``page_relationships.id``: Phase 3
                       copies the evidence values it relies on.
``page_protections``   protection flags (policy / cms / manual); deactivated, never deleted

Migration 0003 also runs ``CREATE EXTENSION vector`` (ADR-0003 deferred it to
the first vector column, which is ``page_embeddings.embedding``), and extends
the Phase 1 append-only trigger to ``page_analyses`` and ``page_embeddings``.

``page_embeddings.embedding`` is a dimension-less ``vector``: the dimension is
the provider's, stored in ``dimensions`` and enforced by a CHECK against
``vector_dims(embedding)``. Similarity queries always filter on (model,
dimensions), so vectors from two models are never compared.
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

from ken_contracts.intelligence import Intent, ProtectionSource, RelationshipType
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind
from ken_core.db.base import Base
from ken_core.db.models.inventory import _one_of
from ken_core.db.types import Vector

IMMUTABLE_INTELLIGENCE_TABLES: Final = ("page_analyses", "page_embeddings")


def _ts(*, nullable: bool = False, default_now: bool = False) -> Any:
    return mapped_column(
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if default_now else None,
    )


def _jsonb(default: str) -> Any:
    return mapped_column(JSONB, nullable=False, server_default=text(f"'{default}'::jsonb"))


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = _ts(default_now=True)
    completed_at: Mapped[datetime | None] = _ts(nullable=True)
    counters: Mapped[dict[str, int]] = _jsonb("{}")
    config_snapshot: Mapped[dict[str, Any]] = _jsonb("{}")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        CheckConstraint(_one_of("kind", JobKind), name="kind_valid"),
        CheckConstraint(_one_of("status", RunStatus), name="status_valid"),
        CheckConstraint(
            "(status = 'running') = (completed_at IS NULL)", name="completed_iff_not_running"
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at", name="completed_after_started"
        ),
        Index("ix_job_runs_kind_started_at", "kind", text("started_at DESC")),
    )


class PageAnalysis(Base):
    __tablename__ = "page_analyses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    page_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    page_version: Mapped[int] = mapped_column(Integer, nullable=False)
    analysis_version: Mapped[str] = mapped_column(Text, nullable=False)
    extractor: Mapped[str] = mapped_column(Text, nullable=False)
    page_type: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    commercial_importance: Mapped[int] = mapped_column(Integer, nullable=False)
    industry_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    topics: Mapped[list[dict[str, Any]]] = _jsonb("[]")
    geographies: Mapped[list[dict[str, Any]]] = _jsonb("[]")
    entities: Mapped[list[dict[str, Any]]] = _jsonb("[]")
    keywords: Mapped[list[str]] = _jsonb("[]")
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    salient_blocks: Mapped[list[dict[str, Any]]] = _jsonb("[]")
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_runs.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["page_id", "page_version"],
            ["page_versions.page_id", "page_versions.version"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("page_id", "page_version", "analysis_version"),
        CheckConstraint(_one_of("intent", Intent), name="intent_valid"),
        CheckConstraint("commercial_importance BETWEEN 0 AND 3", name="importance_range"),
        CheckConstraint("extractor IN ('deterministic', 'llm')", name="extractor_valid"),
        Index("ix_page_analyses_market_id", "market_id"),
        # Topic / geography membership: topics @> '[{"node_id": "x"}]'.
        Index(
            "ix_page_analyses_topics",
            "topics",
            postgresql_using="gin",
            postgresql_ops={"topics": "jsonb_path_ops"},
        ),
        Index(
            "ix_page_analyses_geographies",
            "geographies",
            postgresql_using="gin",
            postgresql_ops={"geographies": "jsonb_path_ops"},
        ),
        # Entity-overlap retrieval (Phase 3): entities @> '[{"entity_id": "x"}]'.
        Index(
            "ix_page_analyses_entities",
            "entities",
            postgresql_using="gin",
            postgresql_ops={"entities": "jsonb_path_ops"},
        ),
    )


class PageEmbedding(Base):
    __tablename__ = "page_embeddings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    page_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    page_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    recipe_version: Mapped[int] = mapped_column(Integer, nullable=False)
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_runs.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["page_id", "page_version"],
            ["page_versions.page_id", "page_versions.version"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("page_id", "page_version", "model", "recipe_version"),
        CheckConstraint("dimensions > 0", name="dimensions_positive"),
        CheckConstraint("vector_dims(embedding) = dimensions", name="dimensions_match"),
        CheckConstraint("input_hash ~ '^[0-9a-f]{64}$'", name="input_hash_sha256"),
        Index("ix_page_embeddings_model_dimensions", "model", "dimensions"),
    )


class PageRelationship(Base):
    __tablename__ = "page_relationships"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    source_page_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    target_page_version: Mapped[int] = mapped_column(Integer, nullable=False)
    relationship_type: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = _jsonb("{}")
    analysis_version: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_runs.id", ondelete="RESTRICT"), nullable=True
    )
    computed_at: Mapped[datetime] = _ts()

    __table_args__ = (
        UniqueConstraint("source_page_id", "target_page_id", "relationship_type"),
        CheckConstraint(_one_of("relationship_type", RelationshipType), name="type_valid"),
        CheckConstraint("weight >= 0 AND weight <= 1", name="weight_range"),
        CheckConstraint("source_page_id <> target_page_id", name="not_self"),
        Index("ix_page_relationships_target_page_id", "target_page_id"),
    )


class PageProtection(Base):
    __tablename__ = "page_protections"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    rule_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(default_now=True)
    deactivated_at: Mapped[datetime | None] = _ts(nullable=True)
    deactivated_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(_one_of("source", ProtectionSource), name="source_valid"),
        CheckConstraint("active = (deactivated_at IS NULL)", name="active_iff_not_deactivated"),
        # At most one ACTIVE protection per (page, source, rule).
        Index(
            "uq_page_protections_active",
            "page_id",
            "source",
            text("coalesce(rule_id, '')"),
            unique=True,
            postgresql_where=text("active"),
        ),
    )
