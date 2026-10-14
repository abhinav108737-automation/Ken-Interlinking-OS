"""Authentication repository: SQL primitives for ``users`` and ``auth_sessions`` (ADR-0013).

The caller owns the transaction: nothing here commits. Migration 0006's guards bound what is
possible anyway -- users are never deleted and their id / username / created_at never change;
a session changes only ``revoked_at`` / ``last_seen_at`` and is deleted only by the prune
(expired more than 30 days ago).

Nothing here hashes or verifies a password and nothing here sees a token: callers pass the
scrypt hash and the token's SHA-256 (``ken_services.auth``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ken_contracts.auth import Role
from ken_core.db.models.review import AuthSession, User

SESSION_PRUNE_AFTER: Final = timedelta(days=30)
"""Migration 0006 allows DELETE only of sessions expired longer than this."""


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: uuid.UUID
    username: str
    display_name: str
    role: Role
    password_hash: str
    disabled: bool
    failed_logins: int
    locked_until: datetime | None
    last_login_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """A session joined with its user's CURRENT role and state."""

    id: uuid.UUID
    user_id: uuid.UUID
    expires_at: datetime
    revoked_at: datetime | None
    last_seen_at: datetime | None
    username: str
    display_name: str
    role: Role
    disabled: bool


def _user(row: User) -> UserRecord:
    return UserRecord(
        id=row.id,
        username=row.username,
        display_name=row.display_name,
        role=Role(row.role),
        password_hash=row.password_hash,
        disabled=row.disabled,
        failed_logins=row.failed_logins,
        locked_until=row.locked_until,
        last_login_at=row.last_login_at,
        created_at=row.created_at,
    )


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #
async def get_user_by_username(session: AsyncSession, *, username: str) -> UserRecord | None:
    row = (await session.scalars(select(User).where(User.username == username))).first()
    return None if row is None else _user(row)


async def get_user(session: AsyncSession, *, user_id: uuid.UUID) -> UserRecord | None:
    row = (await session.scalars(select(User).where(User.id == user_id))).first()
    return None if row is None else _user(row)


async def list_users(session: AsyncSession) -> list[UserRecord]:
    rows = (await session.scalars(select(User).order_by(User.username))).all()
    return [_user(r) for r in rows]


async def insert_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    username: str,
    display_name: str,
    role: Role,
    password_hash: str,
    now: datetime,
) -> None:
    session.add(
        User(
            id=user_id,
            username=username,
            display_name=display_name,
            role=Role(role).value,
            password_hash=password_hash,
            disabled=False,
            failed_logins=0,
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()


async def record_failed_login(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    max_failed: int,
    lock_until: datetime,
    now: datetime,
) -> datetime | None:
    """ATOMIC ``failed_logins + 1`` (``UPDATE ... RETURNING``); on reaching ``max_failed`` the
    account is locked until ``lock_until`` and the counter restarts. A user already locked
    at ``now`` is left unchanged. Returns the (new) ``locked_until``."""
    reached = User.failed_logins + 1 >= max_failed
    statement = (
        update(User)
        .where(User.id == user_id)
        .where((User.locked_until.is_(None)) | (User.locked_until <= now))
        .values(
            failed_logins=case((reached, 0), else_=User.failed_logins + 1),
            locked_until=case((reached, lock_until), else_=None),
            updated_at=now,
        )
        .returning(User.locked_until)
        .execution_options(synchronize_session=False)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def record_successful_login(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    now: datetime,
    new_password_hash: str | None = None,
) -> UserRecord | None:
    """Reset the failure counter and lock, stamp ``last_login_at`` (and store a re-hash).

    Only an ENABLED user that is not locked at ``now`` is updated; ``None`` otherwise (a
    concurrent disable or lock wins)."""
    values: dict[str, object] = {
        "failed_logins": 0,
        "locked_until": None,
        "last_login_at": now,
        "updated_at": now,
    }
    if new_password_hash is not None:
        values["password_hash"] = new_password_hash
    statement = (
        update(User)
        .where(User.id == user_id, User.disabled.is_(False))
        .where((User.locked_until.is_(None)) | (User.locked_until <= now))
        .values(**values)
        .returning(User)
        .execution_options(synchronize_session=False)
    )
    row = (await session.execute(statement)).scalars().first()
    return None if row is None else _user(row)


async def update_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    now: datetime,
    role: Role | None = None,
    password_hash: str | None = None,
    disabled: bool | None = None,
    reset_lockout: bool = False,
) -> bool:
    """Operator changes (CLI only). True when the user exists."""
    values: dict[str, object] = {"updated_at": now}
    if role is not None:
        values["role"] = Role(role).value
    if password_hash is not None:
        values["password_hash"] = password_hash
    if disabled is not None:
        values["disabled"] = disabled
    if reset_lockout:
        values["failed_logins"] = 0
        values["locked_until"] = None
    result = await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(**values)
        .returning(User.id)
        .execution_options(synchronize_session=False)
    )
    return result.scalar_one_or_none() is not None


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
async def insert_session(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    token_hash: str,
    created_at: datetime,
    expires_at: datetime,
) -> None:
    session.add(
        AuthSession(
            id=session_id,
            user_id=user_id,
            token_hash=token_hash,
            created_at=created_at,
            expires_at=expires_at,
        )
    )
    await session.flush()


async def session_by_token_hash(session: AsyncSession, *, token_hash: str) -> SessionRecord | None:
    row = (
        await session.execute(
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.token_hash == token_hash)
        )
    ).first()
    if row is None:
        return None
    auth, user = row[0], row[1]
    return SessionRecord(
        id=auth.id,
        user_id=user.id,
        expires_at=auth.expires_at,
        revoked_at=auth.revoked_at,
        last_seen_at=auth.last_seen_at,
        username=user.username,
        display_name=user.display_name,
        role=Role(user.role),
        disabled=user.disabled,
    )


async def touch_session(
    session: AsyncSession, *, session_id: uuid.UUID, now: datetime, throttle: timedelta
) -> bool:
    """Stamp ``last_seen_at`` unless it was stamped within ``throttle``. True if written."""
    result = await session.execute(
        update(AuthSession)
        .where(
            AuthSession.id == session_id,
            AuthSession.revoked_at.is_(None),
            (AuthSession.last_seen_at.is_(None)) | (AuthSession.last_seen_at <= now - throttle),
        )
        .values(last_seen_at=now)
        .returning(AuthSession.id)
        .execution_options(synchronize_session=False)
    )
    return result.scalar_one_or_none() is not None


async def revoke_session(session: AsyncSession, *, session_id: uuid.UUID, now: datetime) -> bool:
    """Revoke one live session (idempotent: an already revoked one is left as it is)."""
    result = await session.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
        .returning(AuthSession.id)
        .execution_options(synchronize_session=False)
    )
    return result.scalar_one_or_none() is not None


async def revoke_user_sessions(session: AsyncSession, *, user_id: uuid.UUID, now: datetime) -> int:
    """Revoke every unrevoked session of a user; the number revoked."""
    result = await session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
        .returning(AuthSession.id)
        .execution_options(synchronize_session=False)
    )
    return len(result.scalars().all())


async def prune_sessions(session: AsyncSession) -> int:
    """Delete sessions that expired more than :data:`SESSION_PRUNE_AFTER` ago. The database
    clock decides, exactly as the 0006 guard does, so the two can never disagree."""
    result = await session.execute(
        delete(AuthSession)
        .where(AuthSession.expires_at < func.now() - SESSION_PRUNE_AFTER)
        .returning(AuthSession.id)
        .execution_options(synchronize_session=False)
    )
    return len(result.scalars().all())
