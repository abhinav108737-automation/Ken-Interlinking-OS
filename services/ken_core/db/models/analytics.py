"""Analytics tables (Phase 7, migration 0008; docs/adr/ADR-0015-analytics.md §2 as amended by §12).

``analytics_imports``   the import LEDGER: one row per SUCCEEDED import or aggregation,
                        inserted once in the data transaction and IMMUTABLE afterwards
                        (``ken_reject_mutation``). Failures live only in ``job_runs``.
``link_events``         first-party impressions/clicks from the loopback collector. NO PII:
                        no IP, UA, page-view id, referrer, query string, cookie or user id
                        (the column set is asserted by a test). ``received_at`` is truncated
                        to the minute. UPDATE is rejected; DELETE only under the
                        transaction-local ``ken.analytics_writer`` setting.
``link_metrics_daily``  link-level daily rows (``ga4`` | ``first_party``).
``page_metrics_daily``  page-level daily rows (``ga4`` | ``search_console``).
                        Both daily tables: UPDATE rejected; DELETE only under
                        ``set_config('ken.analytics_writer', 'on', true)`` (replace imports).
``link_measurements``   APPEND-ONLY snapshot of what was measured when a plan moved
                        ``verified -> measured`` (``ken_contracts.analytics.LinkMeasurement``).

The guard for the three writer-gated tables is ONE shared function,
``ken_analytics_writer_guard()``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ken_contracts.analytics import (
    MAX_FILE_NAME_CHARS,
    MAX_RANGE_DAYS,
    MAX_UNRESOLVED_SAMPLE,
    AnalyticsSource,
    Dataset,
    ImportVia,
    LinkEventType,
    MatchMode,
)
from ken_core.db.base import Base
from ken_core.db.models.inventory import _one_of

ANALYTICS_WRITER_SETTING: Final = "ken.analytics_writer"
"""Transaction-local GUC: ``SELECT set_config('ken.analytics_writer', 'on', true)`` lets the
SAME transaction DELETE rows of :data:`WRITER_GATED_TABLES` (and nothing else)."""
WRITER_GATED_TABLES: Final = (
    "link_events",
    "link_metrics_daily",
    "page_metrics_daily",
    "page_query_metrics_daily",
)
APPEND_ONLY_ANALYTICS_TABLES: Final = ("analytics_imports", "link_measurements")
LINK_EVENT_COLUMNS: Final = frozenset(
    {"id", "event_type", "plan_id", "match_mode", "synthetic", "event_date", "received_at",
     "dedupe_key"}
)  # fmt: skip
"""The EXACT ``link_events`` column set (pseudonymity, ADR-0015 §1): a test asserts it."""

_SHA256: Final = "~ '^[0-9a-f]{64}$'"
_LINK_SOURCES: Final = "source IN ('ga4', 'first_party')"
_PAGE_SOURCES: Final = "source IN ('ga4', 'search_console')"


def _ts(*, nullable: bool = False, default_now: bool = False) -> Any:
    return mapped_column(
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if default_now else None,
    )


def _jsonb(default: str) -> Any:
    return mapped_column(JSONB, nullable=False, server_default=text(f"'{default}'::jsonb"))


def _count() -> Any:
    return mapped_column(Integer, nullable=False)


class AnalyticsImport(Base):
    __tablename__ = "analytics_imports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    job_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_runs.id", ondelete="RESTRICT"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    dataset: Mapped[str] = mapped_column(Text, nullable=False)
    via: Mapped[str] = mapped_column(Text, nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    replace_via: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    """``--replace-via``: this import overwrote rows of another ``via`` (recorded override)."""
    date_start: Mapped[date] = mapped_column(Date, nullable=False)
    date_end: Mapped[date] = mapped_column(Date, nullable=False)
    source_timezone: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Basename only (never a path); NULL unless ``via = 'csv'``."""
    file_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    sampled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    rows_written: Mapped[int] = _count()
    analytics_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    unresolved_sample: Mapped[list[str]] = _jsonb("[]")
    """Up to 100 unresolved PATHS (no query string)."""
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        CheckConstraint(_one_of("source", AnalyticsSource), name="source_valid"),
        CheckConstraint(_one_of("dataset", Dataset), name="dataset_valid"),
        CheckConstraint(_one_of("via", ImportVia), name="via_valid"),
        CheckConstraint(f"dataset <> 'links' OR {_LINK_SOURCES}", name="link_dataset_source"),
        CheckConstraint(f"dataset <> 'pages' OR {_PAGE_SOURCES}", name="page_dataset_source"),
        CheckConstraint(
            "dataset <> 'queries' OR source = 'search_console'",
            name="query_dataset_source",
        ),
        CheckConstraint(
            "via <> 'collector' OR (source = 'first_party' AND dataset = 'links')",
            name="collector_is_first_party_links",
        ),
        CheckConstraint(
            "via <> 'api' OR source IN ('ga4', 'search_console')", name="api_is_google"
        ),
        CheckConstraint("via <> 'synthetic' OR synthetic", name="synthetic_via_is_synthetic"),
        CheckConstraint("date_end >= date_start", name="date_range_ordered"),
        CheckConstraint(f"date_end - date_start < {MAX_RANGE_DAYS}", name="date_range_bounded"),
        CheckConstraint("length(source_timezone) BETWEEN 1 AND 64", name="source_timezone_bounded"),
        CheckConstraint("(via = 'csv') = (file_sha256 IS NOT NULL)", name="file_sha256_iff_csv"),
        CheckConstraint("(file_name IS NULL) = (file_sha256 IS NULL)", name="file_name_iff_sha256"),
        CheckConstraint(f"file_sha256 IS NULL OR file_sha256 {_SHA256}", name="file_sha256_hex"),
        CheckConstraint(
            f"file_name IS NULL OR (length(file_name) BETWEEN 1 AND {MAX_FILE_NAME_CHARS} "
            "AND file_name !~ '[/\\\\]' AND file_name NOT IN ('.', '..'))",
            name="file_name_is_basename",
        ),
        CheckConstraint("rows_written >= 0", name="rows_written_non_negative"),
        CheckConstraint("analytics_policy_version >= 1", name="policy_version_positive"),
        CheckConstraint(
            "jsonb_typeof(unresolved_sample) = 'array' AND "
            f"jsonb_array_length(unresolved_sample) <= {MAX_UNRESOLVED_SAMPLE}",
            name="unresolved_sample_bounded_array",
        ),
        CheckConstraint("length(triggered_by) BETWEEN 1 AND 200", name="triggered_by_bounded"),
        Index(
            "ix_analytics_imports_source_dataset_date_end",
            "source",
            "dataset",
            text("date_end DESC"),
        ),
        Index("ix_analytics_imports_job_run_id", "job_run_id"),
        Index("ix_analytics_imports_created_at", text("created_at DESC")),
    )


class LinkEvent(Base):
    __tablename__ = "link_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("link_plans.id", ondelete="RESTRICT"), nullable=False
    )
    match_mode: Mapped[str] = mapped_column(Text, nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    """Set by the collector from ITS configuration (every allowed origin loopback), never by
    the client."""
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    """UTC date of ``received_at``."""
    received_at: Mapped[datetime] = _ts()
    """Truncated to the minute (a CHECK): full precision would allow a join with web logs."""
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    """sha256(pv | type | plan_id); the page-view id itself is never stored."""

    __table_args__ = (
        CheckConstraint(_one_of("event_type", LinkEventType), name="event_type_valid"),
        CheckConstraint(_one_of("match_mode", MatchMode), name="match_mode_valid"),
        CheckConstraint(f"dedupe_key {_SHA256}", name="dedupe_key_sha256"),
        CheckConstraint(
            "date_trunc('minute', received_at AT TIME ZONE 'UTC') "
            "= (received_at AT TIME ZONE 'UTC')",
            name="received_at_minute",
        ),
        CheckConstraint(
            "event_date = (received_at AT TIME ZONE 'UTC')::date", name="event_date_is_utc"
        ),
        Index("ix_link_events_plan_id_event_date", "plan_id", "event_date"),
        Index("ix_link_events_event_date", "event_date"),
    )


class LinkMetricsDaily(Base):
    __tablename__ = "link_metrics_daily"

    source: Mapped[str] = mapped_column(Text, nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("link_plans.id", ondelete="RESTRICT"), nullable=False
    )
    impressions: Mapped[int] = _count()
    clicks: Mapped[int] = _count()
    import_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analytics_imports.id", ondelete="RESTRICT"), nullable=False
    )
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("source", "metric_date", "plan_id"),
        CheckConstraint(_LINK_SOURCES, name="source_valid"),
        CheckConstraint("impressions >= 0 AND clicks >= 0", name="counts_non_negative"),
        Index("ix_link_metrics_daily_plan_id_metric_date", "plan_id", "metric_date"),
        Index("ix_link_metrics_daily_import_id", "import_id"),
    )


class PageMetricsDaily(Base):
    __tablename__ = "page_metrics_daily"

    source: Mapped[str] = mapped_column(Text, nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    impressions: Mapped[int] = _count()
    clicks: Mapped[int] = _count()
    position_sum: Mapped[float] = mapped_column(Float, nullable=False)
    """Sum of position x impressions (GSC); average position = sum / impressions."""
    pageviews: Mapped[int] = _count()
    sessions: Mapped[int] = _count()
    import_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analytics_imports.id", ondelete="RESTRICT"), nullable=False
    )
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("source", "metric_date", "page_id"),
        CheckConstraint(_PAGE_SOURCES, name="source_valid"),
        CheckConstraint(
            "impressions >= 0 AND clicks >= 0 AND pageviews >= 0 AND sessions >= 0",
            name="counts_non_negative",
        ),
        CheckConstraint(
            "position_sum >= 0 AND (impressions > 0 OR position_sum = 0)",
            name="position_sum_valid",
        ),
        Index("ix_page_metrics_daily_page_id_metric_date", "page_id", "metric_date"),
        Index("ix_page_metrics_daily_import_id", "import_id"),
    )


class PageQueryMetricsDaily(Base):
    __tablename__ = "page_query_metrics_daily"

    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    query_hash: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    impressions: Mapped[int] = _count()
    clicks: Mapped[int] = _count()
    position_sum: Mapped[float] = mapped_column(Float, nullable=False)
    import_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analytics_imports.id", ondelete="RESTRICT"), nullable=False
    )
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("metric_date", "page_id", "query_hash"),
        CheckConstraint(f"query_hash {_SHA256}", name="query_hash_sha256"),
        CheckConstraint("length(query) BETWEEN 1 AND 500", name="query_bounded"),
        CheckConstraint("impressions >= 0 AND clicks >= 0", name="counts_non_negative"),
        CheckConstraint(
            "position_sum >= 0 AND (impressions > 0 OR position_sum = 0)",
            name="position_sum_valid",
        ),
        Index("ix_page_query_metrics_daily_page_id_metric_date", "page_id", "metric_date"),
        Index("ix_page_query_metrics_daily_import_id", "import_id"),
    )


class LinkMeasurementRow(Base):
    __tablename__ = "link_measurements"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("link_plans.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    measured_at: Mapped[datetime] = _ts(default_now=True)
    analytics_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)
    link_totals: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    target_comparison: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    source_comparison: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    flags: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    import_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    """The ``analytics_imports`` ids whose rows were measured (at least one)."""

    __table_args__ = (
        CheckConstraint("analytics_policy_version >= 1", name="policy_version_positive"),
        CheckConstraint("window_end >= window_start", name="window_ordered"),
        CheckConstraint("jsonb_typeof(link_totals) = 'object'", name="link_totals_object"),
        CheckConstraint(
            "jsonb_typeof(target_comparison) = 'array'", name="target_comparison_array"
        ),
        CheckConstraint(
            "jsonb_typeof(source_comparison) = 'array'", name="source_comparison_array"
        ),
        CheckConstraint("jsonb_typeof(flags) = 'array'", name="flags_array"),
        CheckConstraint(
            "jsonb_typeof(import_ids) = 'array' AND jsonb_array_length(import_ids) >= 1",
            name="import_ids_non_empty_array",
        ),
    )


__all__ = [
    "ANALYTICS_WRITER_SETTING",
    "APPEND_ONLY_ANALYTICS_TABLES",
    "LINK_EVENT_COLUMNS",
    "WRITER_GATED_TABLES",
    "AnalyticsImport",
    "LinkEvent",
    "LinkMeasurementRow",
    "LinkMetricsDaily",
    "PageMetricsDaily",
    "PageQueryMetricsDaily",
]
