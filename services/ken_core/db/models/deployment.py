"""Controlled-deployment tables (Phase 6, migration 0007; ADR-0014 sections 5 and 8).

``deployments``               one CMS request for 1-20 approved plans of ONE source page (or a
                              per-plan REMOVE rollback recorded on it). ``operation_id`` is
                              UNIQUE (the idempotency key) with its ``request_fingerprint``;
                              ``request_json`` is the EXACT serialised ``LinkChangeRequest``
                              sent to the CMS, resent byte for byte by reconciliation. GUARDED
                              by ``ken_deployment_guard()``: DELETE is rejected; only the
                              :data:`DEPLOYMENT_MUTABLE_COLUMNS` may change; ``status`` moves
                              only along :data:`DEPLOYMENT_TRANSITIONS`; a row in a
                              :data:`FROZEN_DEPLOYMENT_STATUSES` status never changes again.
``deployment_items``          APPEND-ONLY per-plan stage records (``requested`` at intent,
                              then the outcome rows).
``deployment_verifications``  APPEND-ONLY: each verification run and its checks.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
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

from ken_contracts.deployment import MAX_NOTE_CHARS, DeploymentStatus, RollbackReason
from ken_core.db.base import Base
from ken_core.db.models.inventory import _one_of

_D = DeploymentStatus

DEPLOYMENT_TRANSITIONS: Final[Mapping[DeploymentStatus, frozenset[DeploymentStatus]]] = {
    _D.APPLYING: frozenset({_D.APPLIED, _D.FAILED, _D.CONFLICT}),
    _D.APPLIED: frozenset({_D.VERIFIED, _D.VERIFICATION_FAILED, _D.ROLLING_BACK}),
    _D.VERIFIED: frozenset({_D.VERIFICATION_FAILED, _D.ROLLING_BACK}),
    _D.VERIFICATION_FAILED: frozenset({_D.VERIFIED, _D.ROLLING_BACK}),
    # A rollback conflict (or a per-plan rollback that leaves links live) returns the row to
    # the status it had before ``rolling_back`` (``rollback_from_status``).
    _D.ROLLING_BACK: frozenset({_D.ROLLED_BACK, _D.APPLIED, _D.VERIFIED, _D.VERIFICATION_FAILED}),
    _D.FAILED: frozenset(),
    _D.CONFLICT: frozenset(),
    _D.ROLLED_BACK: frozenset(),
}
"""The deployment state machine (``DeploymentStatus`` docstring), enforced by trigger."""

FROZEN_DEPLOYMENT_STATUSES: Final = frozenset({_D.FAILED, _D.CONFLICT, _D.ROLLED_BACK})
APPLIED_DEPLOYMENT_STATUSES: Final = frozenset(
    {_D.APPLIED, _D.VERIFIED, _D.VERIFICATION_FAILED, _D.ROLLING_BACK, _D.ROLLED_BACK}
)
"""Statuses whose CMS change reached the page (``applied_at`` is set)."""

DEPLOYMENT_MUTABLE_COLUMNS: Final = (
    "status",
    "cms_deployment_id",
    "previous_version",
    "new_version",
    "error_code",
    "applied_at",
    "verified_at",
    "rolled_back_at",
    "updated_at",
    "attempts",
    "rollback_operation_id",
    "rollback_fingerprint",
    "rollback_reason",
    "rollback_note",
    "rollback_actor",
    "rollback_user_id",
    "rollback_from_status",
    "rollback_plan_ids",
    "rollback_request_json",
    "restored_version",
)
"""The ONLY ``deployments`` columns an UPDATE may change (trigger in 0007)."""

APPEND_ONLY_DEPLOYMENT_TABLES: Final = ("deployment_items", "deployment_verifications")


class ItemStage:
    """``deployment_items.stage`` vocabulary (fixed)."""

    REQUESTED: Final = "requested"
    APPLIED: Final = "applied"
    ALREADY_APPLIED: Final = "already_applied"
    FAILED: Final = "failed"
    CONFLICT: Final = "conflict"
    VERIFIED: Final = "verified"
    VERIFICATION_FAILED: Final = "verification_failed"
    ROLLED_BACK: Final = "rolled_back"
    ROLLBACK_CONFLICT: Final = "rollback_conflict"

    ALL: Final = (
        REQUESTED,
        APPLIED,
        ALREADY_APPLIED,
        FAILED,
        CONFLICT,
        VERIFIED,
        VERIFICATION_FAILED,
        ROLLED_BACK,
        ROLLBACK_CONFLICT,
    )


def _ts(*, nullable: bool = False, default_now: bool = False) -> Any:
    return mapped_column(
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if default_now else None,
    )


def _jsonb(default: str, *, nullable: bool = False) -> Any:
    # A nullable JSONB column stores Python None as SQL NULL (not the JSON ``null``, which
    # its ``jsonb_typeof(...) = 'array'`` CHECK would refuse).
    return mapped_column(
        JSONB(none_as_null=True) if nullable else JSONB,
        nullable=nullable,
        server_default=None if nullable else text(f"'{default}'::jsonb"),
    )


_STAGES: Final = ", ".join(f"'{s}'" for s in ItemStage.ALL)
_FINGERPRINT: Final = "~ '^[0-9a-f]{64}$'"


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    request_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    """The exact serialised ``LinkChangeRequest`` sent to the CMS (resent byte for byte)."""
    cms_provider: Mapped[str] = mapped_column(Text, nullable=False)
    cms_page_id: Mapped[str] = mapped_column(Text, nullable=False)
    cms_deployment_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    source_page_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    preview: Mapped[list[dict[str, Any]]] = _jsonb("[]")
    """The dry-run snapshot (``ChangePreview`` list) validated at intent time."""
    previous_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)
    applied_at: Mapped[datetime | None] = _ts(nullable=True)
    verified_at: Mapped[datetime | None] = _ts(nullable=True)
    rolled_back_at: Mapped[datetime | None] = _ts(nullable=True)
    rollback_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    rollback_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    rollback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rollback_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    rollback_actor: Mapped[str | None] = mapped_column(Text, nullable=True)
    rollback_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    rollback_from_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    rollback_plan_ids: Mapped[list[str] | None] = _jsonb("[]", nullable=True)
    """``None`` = the whole deployment; else the plans of a per-plan (REMOVE) rollback."""
    rollback_request_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    """A per-plan rollback's exact REMOVE ``LinkChangeRequest`` (reconciliation resends it)."""
    restored_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(_one_of("status", DeploymentStatus), name="status_valid"),
        CheckConstraint(f"request_fingerprint {_FINGERPRINT}", name="fingerprint_sha256"),
        CheckConstraint(
            f"rollback_fingerprint IS NULL OR rollback_fingerprint {_FINGERPRINT}",
            name="rollback_fingerprint_sha256",
        ),
        CheckConstraint("actor ~ '^user:'", name="actor_is_user"),
        CheckConstraint(
            "rollback_actor IS NULL OR rollback_actor ~ '^user:'", name="rollback_actor_is_user"
        ),
        CheckConstraint(f"length(note) <= {MAX_NOTE_CHARS}", name="note_bounded"),
        CheckConstraint(
            f"rollback_note IS NULL OR length(rollback_note) <= {MAX_NOTE_CHARS}",
            name="rollback_note_bounded",
        ),
        CheckConstraint(
            "rollback_reason IS NULL OR " + _one_of("rollback_reason", RollbackReason),
            name="rollback_reason_valid",
        ),
        CheckConstraint(
            "rollback_from_status IS NULL OR " + _one_of("rollback_from_status", DeploymentStatus),
            name="rollback_from_status_valid",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z_]{1,64}$'", name="error_code_vocabulary"
        ),
        CheckConstraint("jsonb_typeof(preview) = 'array'", name="preview_array"),
        CheckConstraint(
            "rollback_plan_ids IS NULL OR jsonb_typeof(rollback_plan_ids) = 'array'",
            name="rollback_plan_ids_array",
        ),
        CheckConstraint("attempts >= 1", name="attempts_positive"),
        CheckConstraint("source_page_version >= 1", name="source_page_version_positive"),
        # applying is the durable intent; ``status = 'applying'`` iff nothing is known yet.
        CheckConstraint(
            "(status IN ('applying', 'failed', 'conflict')) = (applied_at IS NULL)",
            name="applied_at_iff_applied",
        ),
        CheckConstraint(
            "(status = 'rolling_back') = (rollback_from_status IS NOT NULL)",
            name="rolling_back_iff_from_status",
        ),
        Index(
            "uq_deployments_rollback_operation_id",
            "rollback_operation_id",
            unique=True,
            postgresql_where=text("rollback_operation_id IS NOT NULL"),
        ),
        Index(
            "ix_deployments_source_page_id_created_at", "source_page_id", text("created_at DESC")
        ),
        Index("ix_deployments_status_updated_at", "status", "updated_at"),
        Index("ix_deployments_created_at", text("created_at DESC")),
    )


class DeploymentItem(Base):
    __tablename__ = "deployment_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("deployments.id", ondelete="RESTRICT"), nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("link_plans.id", ondelete="RESTRICT"), nullable=False
    )
    change_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    """The rollback operation that wrote a ``rolled_back`` row (idempotent replay lookup)."""
    fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    href: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_text: Mapped[str] = mapped_column(Text, nullable=False)
    target_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT"), nullable=False
    )
    codes: Mapped[list[str]] = _jsonb("[]")
    recorded_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        CheckConstraint(f"stage IN ({_STAGES})", name="stage_valid"),
        CheckConstraint("jsonb_typeof(codes) = 'array'", name="codes_array"),
        CheckConstraint(
            f"fingerprint IS NULL OR fingerprint {_FINGERPRINT}", name="fingerprint_sha256"
        ),
        CheckConstraint("change_id = plan_id", name="change_id_is_plan_id"),
        Index("ix_deployment_items_deployment_id_id", "deployment_id", "id"),
        Index("ix_deployment_items_plan_id", "plan_id"),
        Index(
            "ix_deployment_items_operation_id",
            "operation_id",
            postgresql_where=text("operation_id IS NOT NULL"),
        ),
    )


class DeploymentVerification(Base):
    __tablename__ = "deployment_verifications"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("deployments.id", ondelete="RESTRICT"), nullable=False
    )
    run_at: Mapped[datetime] = _ts(default_now=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    inconclusive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    http_checked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    checks: Mapped[list[dict[str, Any]]] = _jsonb("[]")

    __table_args__ = (
        CheckConstraint("jsonb_typeof(checks) = 'array'", name="checks_array"),
        CheckConstraint("NOT (passed AND inconclusive)", name="passed_xor_inconclusive"),
        Index("ix_deployment_verifications_deployment_id_id", "deployment_id", "id"),
    )


__all__ = [
    "APPEND_ONLY_DEPLOYMENT_TABLES",
    "APPLIED_DEPLOYMENT_STATUSES",
    "DEPLOYMENT_MUTABLE_COLUMNS",
    "DEPLOYMENT_TRANSITIONS",
    "FROZEN_DEPLOYMENT_STATUSES",
    "Deployment",
    "DeploymentItem",
    "DeploymentVerification",
    "ItemStage",
]
