"""Agent supervisor tables (Phase 4, migration 0005). FROZEN (orchestrator).

``agent_runs``   one supervised workflow over one source page (ADR-0012). Only the
                 columns in :data:`AGENT_RUN_MUTABLE_COLUMNS` may change after insert (a
                 generic trigger compares the rest, like ``link_plans``); DELETE is rejected.
``agent_steps``  APPEND-ONLY: one row per executed graph node (``ken_reject_mutation``).

A batch of runs is one ``job_runs`` row of kind ``agent`` (``job_run_id``).
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

from ken_contracts.agents import (
    AgentErrorCode,
    AgentNode,
    AgentOutcome,
    AgentRunStatus,
    AgentTrigger,
    FallbackReason,
    StepStatus,
    TriageDecision,
)
from ken_contracts.model import ModelRole
from ken_core.db.base import Base
from ken_core.db.models.inventory import _one_of

AGENT_RUN_MUTABLE_COLUMNS: Final = (
    "status",
    "outcome",
    "triage_decision",
    "source_page_version",
    "plan_ids",
    "review_notes",
    "error_code",
    "finished_at",
    "updated_at",
)
"""The ONLY agent_runs columns an UPDATE may change (enforced by trigger in 0005)."""

APPEND_ONLY_AGENT_TABLES: Final = ("agent_steps",)


def _ts(*, nullable: bool = False, default_now: bool = False) -> Any:
    return mapped_column(
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if default_now else None,
    )


def _jsonb(default: str) -> Any:
    return mapped_column(JSONB, nullable=False, server_default=text(f"'{default}'::jsonb"))


def _nullable_one_of(column: str, values: Any) -> str:
    return f"{column} IS NULL OR " + _one_of(column, values)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    job_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_runs.id", ondelete="RESTRICT"), nullable=True
    )
    source_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    source_page_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    triage_decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_ids: Mapped[list[str]] = _jsonb("[]")
    review_notes: Mapped[list[str]] = _jsonb("[]")
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    graph_version: Mapped[str] = mapped_column(Text, nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = _jsonb("{}")
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = _ts(default_now=True)
    finished_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        CheckConstraint(_one_of("trigger", AgentTrigger), name="trigger_valid"),
        CheckConstraint(_one_of("status", AgentRunStatus), name="status_valid"),
        CheckConstraint(_nullable_one_of("outcome", AgentOutcome), name="outcome_valid"),
        CheckConstraint(
            _nullable_one_of("triage_decision", TriageDecision), name="triage_decision_valid"
        ),
        CheckConstraint(_nullable_one_of("error_code", AgentErrorCode), name="error_code_valid"),
        # A run is RUNNING exactly while it has no finish time and no outcome.
        CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="finished_iff_not_running"
        ),
        CheckConstraint("(status = 'running') = (outcome IS NULL)", name="outcome_iff_finished"),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="finished_after_started"
        ),
        CheckConstraint("trace_id ~ '^[0-9a-f]{32}$'", name="trace_id_format"),
        Index("ix_agent_runs_started_at", text("started_at DESC")),
        Index("ix_agent_runs_source_page_id_started_at", "source_page_id", text("started_at DESC")),
        Index("ix_agent_runs_status_outcome", "status", "outcome"),
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    node: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime] = _ts()
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    model_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_used: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[list[dict[str, Any]]] = _jsonb("[]")
    output: Mapped[dict[str, Any]] = _jsonb("{}")
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    span_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        UniqueConstraint("run_id", "seq"),
        CheckConstraint(_one_of("node", AgentNode), name="node_valid"),
        CheckConstraint(_one_of("status", StepStatus), name="status_valid"),
        CheckConstraint(_nullable_one_of("model_role", ModelRole), name="model_role_valid"),
        CheckConstraint(
            _nullable_one_of("fallback_reason", FallbackReason), name="fallback_reason_valid"
        ),
        CheckConstraint(_nullable_one_of("error_code", AgentErrorCode), name="error_code_valid"),
        CheckConstraint(
            "fallback_used = (fallback_reason IS NOT NULL)", name="fallback_reason_iff"
        ),
        CheckConstraint("seq >= 0 AND attempt >= 1 AND duration_ms >= 0", name="counts_valid"),
        CheckConstraint("finished_at >= started_at", name="finished_after_started"),
        CheckConstraint("trace_id ~ '^[0-9a-f]{32}$'", name="trace_id_format"),
        CheckConstraint("span_id ~ '^[0-9a-f]{16}$'", name="span_id_format"),
    )


__all__ = [
    "AGENT_RUN_MUTABLE_COLUMNS",
    "APPEND_ONLY_AGENT_TABLES",
    "AgentRun",
    "AgentStep",
]
