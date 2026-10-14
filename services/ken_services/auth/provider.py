"""The local, self-hosted :class:`~ken_contracts.auth.AuthProvider` (ADR-0013 sections 1, 7).

**login** (always in this order, so timing and errors reveal nothing):

1. the user is read by (lower-cased) username;
2. the password is ALWAYS verified first -- against :data:`DUMMY_HASH` for an unknown user
   -- and only then are the disabled and locked states checked;
3. unknown user, wrong password, disabled user -> :class:`InvalidCredentialsError`; a locked
   account -> :class:`AccountLockedError` (the API answers both with the SAME 401 body and no
   ``Retry-After``);
4. a wrong password increments ``failed_logins`` ATOMICALLY (``UPDATE ... RETURNING``); the
   5th consecutive failure locks the account for 15 minutes;
5. success resets the counter and the lock, stamps ``last_login_at``, re-hashes a password
   stored with parameters below the current ones, and opens a session: an opaque
   ``secrets.token_urlsafe(32)`` token (shown once) whose SHA-256 alone is stored, valid
   12 hours.

**authenticate** re-reads the session AND the user on every request: a revoked or expired
session, a disabled user or a changed role takes effect at once (the CLI also revokes the
user's sessions). ``last_seen_at`` is written at most once a minute.

**logout** revokes one session; revoking an already revoked session is a no-op.

Nothing here logs a password or a token.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.auth import (
    LOCKOUT_MINUTES,
    MAX_FAILED_LOGINS,
    MAX_PASSWORD_CHARS,
    SESSION_TTL_HOURS,
    AccountLockedError,
    InvalidCredentialsError,
    Principal,
    SessionInvalidError,
)
from ken_core.db.repositories import auth as repo
from ken_services.auth.passwords import (
    CURRENT_PARAMS,
    DUMMY_HASH,
    ScryptParams,
    hash_password_async,
    needs_rehash,
    verify_password_async,
)

__all__ = [
    "LAST_SEEN_THROTTLE",
    "TOKEN_PATTERN",
    "LocalAuthProvider",
    "hash_token",
]

TOKEN_BYTES: Final = 32
TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{43,128}")
"""``secrets.token_urlsafe(32)`` is 43 characters; anything else is refused unhashed."""
LAST_SEEN_THROTTLE: Final = timedelta(minutes=1)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def hash_token(token: str) -> str:
    """The stored form of a session token: its SHA-256, hex."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class LocalAuthProvider:
    """Users and sessions in PostgreSQL; scrypt hashes; opaque bearer tokens.

    ``params`` are the CURRENT scrypt parameters (tests pass cheaper ones); a stored hash
    weaker than them is re-hashed at the next successful login.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        params: ScryptParams = CURRENT_PARAMS,
        clock: Callable[[], datetime] = _utc_now,
        session_ttl: timedelta = timedelta(hours=SESSION_TTL_HOURS),
        max_failed_logins: int = MAX_FAILED_LOGINS,
        lockout: timedelta = timedelta(minutes=LOCKOUT_MINUTES),
        dummy_hash: str = DUMMY_HASH,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._params = params
        self._clock = clock
        self._ttl = session_ttl
        self._max_failed = max_failed_logins
        self._lockout = lockout
        self._dummy = dummy_hash

    async def login(self, *, username: str, password: SecretStr) -> tuple[str, Principal, datetime]:
        secret = password.get_secret_value()
        if len(secret) > MAX_PASSWORD_CHARS:  # never a valid password; same cost and error
            await verify_password_async(secret[:MAX_PASSWORD_CHARS], self._dummy)
            raise InvalidCredentialsError
        name = username.lower()
        async with self._sessionmaker() as session:
            user = await repo.get_user_by_username(session, username=name)
        if user is None:
            await verify_password_async(secret, self._dummy)
            raise InvalidCredentialsError
        ok = await verify_password_async(secret, user.password_hash)
        now = self._clock()
        if user.disabled:
            raise InvalidCredentialsError
        if user.locked_until is not None and user.locked_until > now:
            raise AccountLockedError
        if not ok:
            async with self._sessionmaker() as session, session.begin():
                await repo.record_failed_login(
                    session,
                    user_id=user.id,
                    max_failed=self._max_failed,
                    lock_until=now + self._lockout,
                    now=now,
                )
            raise InvalidCredentialsError

        rehash = (
            await hash_password_async(secret, params=self._params)
            if needs_rehash(user.password_hash, params=self._params)
            else None
        )
        token = secrets.token_urlsafe(TOKEN_BYTES)
        session_id = uuid.uuid4()
        now = self._clock()
        expires_at = now + self._ttl
        async with self._sessionmaker() as session, session.begin():
            current = await repo.record_successful_login(
                session, user_id=user.id, now=now, new_password_hash=rehash
            )
            if current is None:  # disabled or locked concurrently
                raise InvalidCredentialsError
            await repo.insert_session(
                session,
                session_id=session_id,
                user_id=current.id,
                token_hash=hash_token(token),
                created_at=now,
                expires_at=expires_at,
            )
        principal = Principal(
            user_id=current.id,
            username=current.username,
            display_name=current.display_name,
            role=current.role,
            session_id=session_id,
        )
        return token, principal, expires_at

    async def authenticate(self, *, token: str) -> tuple[Principal, datetime]:
        if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
            raise SessionInvalidError
        now = self._clock()
        async with self._sessionmaker() as session, session.begin():
            record = await repo.session_by_token_hash(session, token_hash=hash_token(token))
            if (
                record is None
                or record.revoked_at is not None
                or record.expires_at <= now
                or record.disabled
            ):
                raise SessionInvalidError
            if record.last_seen_at is None or record.last_seen_at <= now - LAST_SEEN_THROTTLE:
                await repo.touch_session(
                    session, session_id=record.id, now=now, throttle=LAST_SEEN_THROTTLE
                )
        try:
            principal = Principal(
                user_id=record.user_id,
                username=record.username,
                display_name=record.display_name,
                role=record.role,
                session_id=record.id,
            )
        except ValueError:  # a row that no longer satisfies the contract: fail closed
            raise SessionInvalidError from None
        return principal, record.expires_at

    async def logout(self, *, session_id: uuid.UUID) -> None:
        async with self._sessionmaker() as session, session.begin():
            await repo.revoke_session(session, session_id=session_id, now=self._clock())
