"""Content inventory tables (Phase 1, migration 0002). FROZEN (orchestrator-owned).

Designed from what Phase 1 actually needs (ADR-0006: no speculative columns).

``ingestion_runs``    one row per crawl / import, with its accounting
``ingestion_issues``  per-URL problems of a run (never fatal to it)
``pages``             one row per canonical page: stable identity + CURRENT-version summary
``page_versions``     IMMUTABLE content history; a row only when the content hash changes
``existing_links``    IMMUTABLE body links of one page version
``known_urls``        every URL observed and what it led to (aliases, redirects, 404s)

``known_urls.page_id`` is "the last page this URL led to": kept when a later
observation of the URL fails transiently, so an alias -> page mapping never
flaps. Link resolution branches on ``outcome``, not on ``page_id`` alone.

``pages(id, current_version)`` references ``page_versions`` through a
DEFERRABLE INITIALLY DEFERRED foreign key, so the denormalised current-version
pointer can never name a version that does not exist (checked at commit).

Identity: ``pages.id`` is a UUID we mint. ``pages.canonical_url`` is unique; a
source-provided ``external_id`` (opaque CMS id) is unique when present and wins
over the URL when matching, so a page whose URL changes in the CMS keeps its id.

Immutability of ``page_versions`` and ``existing_links`` is enforced IN THE
DATABASE by a trigger created in migration 0002 that rejects UPDATE and DELETE,
not only by the absence of repository methods.

Enumerated text columns carry CHECK constraints built from the contract enums,
except ``ingestion_issues.code``: ``IssueCode`` is additive by contract, and a
new member must not need a migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
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

from ken_contracts.inventory import (
    CanonicalStatus,
    IssueSeverity,
    LinkType,
    PublicationState,
    RunStatus,
    SourceType,
    UrlOutcome,
)
from ken_core.db.base import Base

IMMUTABLE_TABLES: Final = ("page_versions", "existing_links")
"""Tables whose rows the migration's trigger refuses to UPDATE or DELETE."""


def _one_of(column: str, values: type[Any]) -> str:
    """CHECK expression: ``column IN ('a', 'b', ...)`` from a StrEnum, in declaration order."""
    quoted = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({quoted})"


def _now() -> Any:
    return func.now()


def _timestamp(*, nullable: bool = False, default_now: bool = False) -> Any:
    return mapped_column(
        DateTime(timezone=True),
        nullable=nullable,
        server_default=_now() if default_now else None,
    )


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = _timestamp(default_now=True)
    completed_at: Mapped[datetime | None] = _timestamp(nullable=True)

    pages_discovered: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pages_created: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pages_changed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pages_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pages_duplicate: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pages_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pages_skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pages_gone: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    config_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _timestamp(default_now=True)
    updated_at: Mapped[datetime] = _timestamp(default_now=True)

    __table_args__ = (
        CheckConstraint(_one_of("source_type", SourceType), name="source_type_valid"),
        CheckConstraint(_one_of("status", RunStatus), name="status_valid"),
        CheckConstraint(
            "(status = 'running') = (completed_at IS NULL)", name="completed_iff_not_running"
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at", name="completed_after_started"
        ),
        CheckConstraint(
            "LEAST(pages_discovered, pages_fetched, pages_created, pages_changed, "
            "pages_unchanged, pages_duplicate, pages_failed, pages_skipped, pages_gone, "
            "error_count, warning_count) >= 0",
            name="counters_non_negative",
        ),
        Index("ix_ingestion_runs_started_at", text("started_at DESC")),
    )


class IngestionIssue(Base):
    __tablename__ = "ingestion_issues"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ingestion_runs.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = _timestamp()
    created_at: Mapped[datetime] = _timestamp(default_now=True)

    __table_args__ = (
        CheckConstraint(_one_of("severity", IssueSeverity), name="severity_valid"),
        Index("ix_ingestion_issues_run_id_id", "run_id", "id"),
    )


class Page(Base):
    __tablename__ = "pages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    publication_state: Mapped[str] = mapped_column(Text, nullable=False)
    page_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    internal_link_count: Mapped[int] = mapped_column(Integer, nullable=False)
    external_link_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = _timestamp()
    last_seen_at: Mapped[datetime] = _timestamp()
    last_changed_at: Mapped[datetime] = _timestamp()
    gone_at: Mapped[datetime | None] = _timestamp(nullable=True)
    created_at: Mapped[datetime] = _timestamp(default_now=True)
    updated_at: Mapped[datetime] = _timestamp(default_now=True)

    __table_args__ = (
        CheckConstraint("current_version >= 1", name="current_version_positive"),
        # Circular with page_versions.page_id, hence use_alter; deferred so a new
        # page and its first version can be inserted in either order.
        ForeignKeyConstraint(
            ["id", "current_version"],
            ["page_versions.page_id", "page_versions.version"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(_one_of("publication_state", PublicationState), name="publication_valid"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_sha256"),
        # An opaque CMS id identifies at most one page; most pages have none.
        Index(
            "uq_pages_external_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index("ix_pages_last_changed_at", text("last_changed_at DESC")),
    )


class PageVersion(Base):
    __tablename__ = "page_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    hash_version: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_chain: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    declared_canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_status: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    publication_state: Mapped[str] = mapped_column(Text, nullable=False)
    page_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = _timestamp(nullable=True)
    modified_at: Mapped[datetime | None] = _timestamp(nullable=True)
    robots_directives: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    page_metadata: Mapped[dict[str, str]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    headings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    internal_link_count: Mapped[int] = mapped_column(Integer, nullable=False)
    external_link_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = _timestamp()
    created_at: Mapped[datetime] = _timestamp(default_now=True)

    __table_args__ = (
        UniqueConstraint("page_id", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("hash_version >= 1", name="hash_version_positive"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_sha256"),
        CheckConstraint(_one_of("source_type", SourceType), name="source_type_valid"),
        CheckConstraint(_one_of("publication_state", PublicationState), name="publication_valid"),
        CheckConstraint(
            _one_of("canonical_status", CanonicalStatus), name="canonical_status_valid"
        ),
        Index("ix_page_versions_run_id", "run_id"),
    )


class ExistingLink(Base):
    __tablename__ = "existing_links"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    page_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    page_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    href: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_type: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_text: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_source: Mapped[str] = mapped_column(Text, nullable=False)
    rel: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    block_id: Mapped[str] = mapped_column(Text, nullable=False)
    block_kind: Mapped[str] = mapped_column(Text, nullable=False)
    occurrence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    context_text: Mapped[str] = mapped_column(Text, nullable=False)
    context_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _timestamp(default_now=True)

    __table_args__ = (
        # A link belongs to exactly one immutable page version.
        ForeignKeyConstraint(
            ["page_id", "page_version"],
            ["page_versions.page_id", "page_versions.version"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("page_id", "page_version", "ordinal"),
        CheckConstraint(_one_of("link_type", LinkType), name="link_type_valid"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        CheckConstraint("occurrence_index >= 0", name="occurrence_non_negative"),
        # Inbound-link and resolution joins: existing_links.resolved_url = known_urls.url.
        Index("ix_existing_links_resolved_url", "resolved_url"),
    )


class KnownUrl(Base):
    __tablename__ = "known_urls"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), nullable=True
    )
    first_seen_at: Mapped[datetime] = _timestamp()
    last_seen_at: Mapped[datetime] = _timestamp()
    updated_at: Mapped[datetime] = _timestamp(default_now=True)

    __table_args__ = (
        CheckConstraint(_one_of("outcome", UrlOutcome), name="outcome_valid"),
        # A URL that led to a page must say which page. (An implication, not
        # "iff": a later failed observation keeps the last known page.)
        CheckConstraint(
            "outcome NOT IN ('page', 'redirect') OR page_id IS NOT NULL",
            name="resolved_has_page",
        ),
        Index("ix_known_urls_page_id", "page_id"),
    )
