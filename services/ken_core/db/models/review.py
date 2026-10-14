"""Authentication and human-review tables (Phase 5, migration 0006). FROZEN (orchestrator).

``users``           local accounts (ADR-0013). Password hashes only (``scrypt``, stdlib);
                    never deleted (disable instead): DELETE is rejected by trigger.
``auth_sessions``   opaque bearer sessions; only the SHA-256 of the token is stored.
``review_actions``  APPEND-ONLY: every human review action -- the audit record AND the
                    editorial feedback Phase 8 learns from. ``operation_id`` is UNIQUE (the
                    idempotency key), and ``request_fingerprint`` detects a reused key with a
                    different request.
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

from ken_contracts.auth import USERNAME_PATTERN, Role
from ken_contracts.recommendations import LinkPlanStatus
from ken_contracts.review import MAX_NOTE_CHARS, ReviewAction
from ken_core.db.base import Base
from ken_core.db.models.inventory import _one_of

APPEND_ONLY_REVIEW_TABLES: Final = ("review_actions",)
USER_MUTABLE_COLUMNS: Final = (
    "display_name",
    "role",
    "password_hash",
    "disabled",
    "failed_logins",
    "locked_until",
    "last_login_at",
    "updated_at",
)
"""``users`` columns an UPDATE may change (``id``, ``username``, ``created_at`` never)."""


def _ts(*, nullable: bool = False, default_now: bool = False) -> Any:
    return mapped_column(
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if default_now else None,
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_until: Mapped[datetime | None] = _ts(nullable=True)
    last_login_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        CheckConstraint(f"username ~ '{USERNAME_PATTERN}'", name="username_format"),
        CheckConstraint(_one_of("role", Role), name="role_valid"),
        CheckConstraint("length(display_name) BETWEEN 1 AND 120", name="display_name_length"),
        CheckConstraint("password_hash LIKE 'scrypt$%'", name="password_hash_scheme"),
        CheckConstraint("failed_logins >= 0", name="failed_logins_non_negative"),
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = _ts(default_now=True)
    expires_at: Mapped[datetime] = _ts()
    revoked_at: Mapped[datetime | None] = _ts(nullable=True)
    last_seen_at: Mapped[datetime | None] = _ts(nullable=True)

    __table_args__ = (
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_sha256"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        Index("ix_auth_sessions_user_id", "user_id"),
    )


class ReviewActionRow(Base):
    __tablename__ = "review_actions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    request_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("link_plans.id", ondelete="RESTRICT"), nullable=False
    )
    new_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("link_plans.id", ondelete="RESTRICT"), nullable=True
    )
    from_status: Mapped[str] = mapped_column(Text, nullable=False)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    anchor_before: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_after: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_before: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_after: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    bulk_operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    """The ReviewResult returned, replayed verbatim for an idempotent retry."""
    created_at: Mapped[datetime] = _ts(default_now=True)

    __table_args__ = (
        CheckConstraint(_one_of("action", ReviewAction), name="action_valid"),
        CheckConstraint(_one_of("from_status", LinkPlanStatus), name="from_status_valid"),
        CheckConstraint(_one_of("to_status", LinkPlanStatus), name="to_status_valid"),
        CheckConstraint("actor ~ '^user:'", name="actor_is_user"),
        CheckConstraint(f"length(note) <= {MAX_NOTE_CHARS}", name="note_bounded"),
        CheckConstraint("jsonb_typeof(reasons) = 'array'", name="reasons_array"),
        CheckConstraint("request_fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint_sha256"),
        # Edits always produce a superseding plan; approve/reject never do.
        CheckConstraint(
            "(action IN ('edit_anchor', 'replace_target')) = (new_plan_id IS NOT NULL)",
            name="new_plan_iff_edit",
        ),
        Index("ix_review_actions_plan_id", "plan_id"),
        Index("ix_review_actions_created_at", text("created_at DESC")),
        Index("ix_review_actions_bulk_operation_id", "bulk_operation_id"),
    )


__all__ = [
    "APPEND_ONLY_REVIEW_TABLES",
    "USER_MUTABLE_COLUMNS",
    "AuthSession",
    "ReviewActionRow",
    "User",
]
