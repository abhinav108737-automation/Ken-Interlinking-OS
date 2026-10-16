"""Job runs and content-intelligence tables (Phase 2, migration 0003). FROZEN (orchestrator).

``job_runs``           generic run record for every batch job from Phase 2 on
``page_analyses``      IMMUTABLE deterministic analysis per (page version, analysis version)
``page_embeddings``    IMMUTABLE page vector per (page version, model, recipe version,
                       recipe fingerprint)
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
dimensions, recipe version, recipe fingerprint), so vectors from two models or
two text recipes are never compared. ``recipe_fingerprint`` hashes the settings
that shape the embedded text (document prefix, max chars) with the recipe
version (``ken_services.intelligence.embeddings.recipe_fingerprint``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    REAL,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ken_contracts.intelligence import Intent, ProtectionSource, RelationshipType
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind
from ken_core.db.base import Base
from ken_core.db.models.inventory import _one_of

IMMUTABLE_INTELLIGENCE_TABLES: Final = ("page_analyses", "page_embeddings")

MAX_METRICS_VERSION_CHARS: Final = 64
GRAPH_METRICS_VERSION: Final = "graph-alg1"
"""The link-graph recipe version stamped on every ``page_graph_metrics`` row. Bump it when
the algorithm changes; rows of two versions are never compared."""


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
    recipe_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # ADR-0017: a plain PostgreSQL real[] (float4, exactly the precision the extension
    # stored), so the values survive without it and stay readable and checkable in SQL.
    embedding: Mapped[list[float]] = mapped_column(ARRAY(REAL), nullable=False)
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
        UniqueConstraint(
            "page_id", "page_version", "model", "recipe_version", "recipe_fingerprint"
        ),
        CheckConstraint("dimensions > 0", name="dimensions_positive"),
        CheckConstraint("array_length(embedding, 1) = dimensions", name="dimensions_match"),
        CheckConstraint("input_hash ~ '^[0-9a-f]{64}$'", name="input_hash_sha256"),
        Index(
            "ix_page_embeddings_model_dimensions_recipe_fingerprint",
            "model",
            "dimensions",
            "recipe_fingerprint",
        ),
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


class PageGraphMetrics(Base):
    """Internal link-graph position of one page VERSION (migration 0010, directive s5).

    **Recomputable by REPLACE, not append-only, and deliberately so.** Every value here is a
    pure function of (the current link graph, the recipe version): nothing about it is a
    historical fact that could be lost, and a graph recomputation touches EVERY page at once.
    Keeping history would therefore grow the table by the whole corpus on each run while
    adding nothing an audit could use -- and the Phase 2 precedent for derived, version-
    stamped data is exactly this: ``page_relationships`` is replaced per run.

    The row is still identified by ``(page_id, page_version, metrics_version)``, so two
    recipe versions can coexist for a comparison, and ``computed_at`` says when it was
    derived. What a run REPLACES is a row of the same identity; what it never does is edit a
    row of a different ``metrics_version``, so a published number stays reproducible.

    Nothing may reference these ids (the Phase 2 rule for derived data).
    """

    __tablename__ = "page_graph_metrics"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    page_version: Mapped[int] = mapped_column(Integer, nullable=False)
    in_degree: Mapped[int] = mapped_column(Integer, nullable=False)
    out_degree: Mapped[int] = mapped_column(Integer, nullable=False)
    internal_pagerank: Mapped[float] = mapped_column(Float, nullable=False)
    hub_score: Mapped[float] = mapped_column(Float, nullable=False)
    authority_score: Mapped[float] = mapped_column(Float, nullable=False)
    crawl_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Clicks from the configured entry page; NULL = unreachable (an orphan by definition)."""
    is_orphan: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_nearly_orphan: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_bridge: Mapped[bool] = mapped_column(Boolean, nullable=False)
    component_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metrics_version: Mapped[str] = mapped_column(Text, nullable=False)
    """Algorithm/recipe version (e.g. ``graph-alg1``). Two versions are never compared."""
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_runs.id", ondelete="RESTRICT"), nullable=True
    )
    computed_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["page_id", "page_version"],
            ["page_versions.page_id", "page_versions.version"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("page_id", "page_version", "metrics_version"),
        CheckConstraint("in_degree >= 0 AND out_degree >= 0", name="degrees_non_negative"),
        CheckConstraint(
            "internal_pagerank >= 0 AND internal_pagerank <= 1", name="pagerank_range"
        ),
        CheckConstraint("hub_score >= 0 AND hub_score <= 1", name="hub_score_range"),
        CheckConstraint(
            "authority_score >= 0 AND authority_score <= 1", name="authority_score_range"
        ),
        CheckConstraint("crawl_depth IS NULL OR crawl_depth >= 0", name="crawl_depth_valid"),
        CheckConstraint("component_id IS NULL OR component_id >= 0", name="component_id_valid"),
        CheckConstraint("cluster_id IS NULL OR cluster_id >= 0", name="cluster_id_valid"),
        # An orphan has no inbound link, and no inbound link makes it an orphan: the flag and
        # the count can never disagree.
        CheckConstraint("is_orphan = (in_degree = 0)", name="orphan_iff_no_inbound"),
        CheckConstraint("NOT is_orphan OR is_nearly_orphan", name="orphan_implies_nearly"),
        CheckConstraint(
            f"length(metrics_version) BETWEEN 1 AND {MAX_METRICS_VERSION_CHARS}",
            name="metrics_version_bounded",
        ),
        Index("ix_page_graph_metrics_metrics_version_page_id", "metrics_version", "page_id"),
        Index(
            "ix_page_graph_metrics_orphans",
            "metrics_version",
            "page_id",
            postgresql_where=text("is_orphan OR is_nearly_orphan"),
        ),
    )
