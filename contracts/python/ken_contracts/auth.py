"""Local authentication and role-based authorisation (Phase 5, ADR-0013).

FROZEN CONTRACT (orchestrator).

* **Provider abstraction.** :class:`AuthProvider` is the seam: the first implementation is a
  local, self-hosted provider (users in PostgreSQL, ``scrypt`` password hashes from the
  standard library, opaque server-side session tokens). A future SSO/OIDC provider plugs in
  behind the same protocol without touching routes.
* **Bearer tokens, not cookies, at the API.** The API reads ``Authorization: Bearer <token>``
  only, so it has no ambient credentials and no CSRF surface. The web app keeps the token
  in an ``HttpOnly`` ``SameSite=Strict`` cookie on ITS origin and forwards it server-side;
  an Electron shell loads the same web app.
* **Roles** (TRD section 23 RBAC): ``viewer`` < ``reviewer`` < ``operator_admin``. Every
  route declares the :class:`Permission` it needs; :data:`ROLE_PERMISSIONS` is the only
  mapping. There is NO permission that approves automatically: AUTO_APPROVE stays globally
  disabled (TRD section 20) and is not a role capability.
* **Actors.** An authenticated principal acts as ``user:<username>`` (ACTOR_PATTERN). The
  actor is ALWAYS derived from the session, never read from a request body (Phase 3 QA H3).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Protocol, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, Field, SecretStr, field_validator

from ken_contracts.common import FrozenModel, reject_unstorable_text

USERNAME_PATTERN: Final = r"^[a-z0-9][a-z0-9._-]{2,63}$"
"""Lower-case, 3-64 chars: also valid inside ``user:<username>``."""
MIN_PASSWORD_CHARS: Final = 12
MAX_PASSWORD_CHARS: Final = 256
SESSION_TTL_HOURS: Final = 12
MAX_FAILED_LOGINS: Final = 5
LOCKOUT_MINUTES: Final = 15


class Role(StrEnum):
    VIEWER = "viewer"
    """Reads everything a reviewer reads; changes nothing."""
    REVIEWER = "reviewer"
    """Approves, rejects and edits recommendations; records editorial feedback."""
    OPERATOR_ADMIN = "operator_admin"
    """Everything a reviewer may do, plus operator views (audit log, settings, policies)."""


class Permission(StrEnum):
    READ = "read"
    """Inventory, intelligence, recommendations, agent runs, jobs, dashboard."""
    REVIEW = "review"
    """Approve / reject / edit anchor / replace destination / bulk review / feedback."""
    READ_AUDIT = "read_audit"
    """The event log."""
    READ_SETTINGS = "read_settings"
    """Non-secret settings and the active policy documents."""


ROLE_PERMISSIONS: Final[dict[Role, frozenset[Permission]]] = {
    Role.VIEWER: frozenset({Permission.READ}),
    Role.REVIEWER: frozenset({Permission.READ, Permission.REVIEW}),
    Role.OPERATOR_ADMIN: frozenset(
        {Permission.READ, Permission.REVIEW, Permission.READ_AUDIT, Permission.READ_SETTINGS}
    ),
}


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]


class Principal(FrozenModel):
    """The authenticated caller of one request."""

    user_id: UUID
    username: str = Field(pattern=USERNAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=120)
    role: Role
    session_id: UUID

    @property
    def actor(self) -> str:
        """The ONLY way an API handler obtains an actor string."""
        return f"user:{self.username}"

    @property
    def permissions(self) -> frozenset[Permission]:
        return ROLE_PERMISSIONS[self.role]


class LoginRequest(FrozenModel):
    username: str = Field(min_length=3, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)

    @field_validator("username")
    @classmethod
    def _storable_username(cls, value: str) -> str:
        return reject_unstorable_text(value)

    @field_validator("password")
    @classmethod
    def _storable_password(cls, value: SecretStr) -> SecretStr:
        reject_unstorable_text(value.get_secret_value())
        return value


class SessionView(FrozenModel):
    """``POST /auth/login`` response. ``token`` is shown ONCE; only its hash is stored."""

    token: str = Field(description="Opaque bearer token (URL-safe, >= 43 chars).")
    expires_at: AwareDatetime
    user: PrincipalView


class PrincipalView(FrozenModel):
    """``GET /auth/me``: who the caller is and what they may do (drives the UI)."""

    username: str
    display_name: str
    role: Role
    permissions: list[Permission]
    session_expires_at: AwareDatetime


class AuthError(Exception):
    """Base of authentication failures. Messages never say WHICH part was wrong."""


class InvalidCredentialsError(AuthError):
    """Unknown user, wrong password, disabled user: one indistinguishable error."""


class AccountLockedError(AuthError):
    """Too many failed logins; locked until a time (not disclosed to the caller)."""


class SessionInvalidError(AuthError):
    """Missing, malformed, unknown, expired or revoked token."""


@runtime_checkable
class AuthProvider(Protocol):
    async def login(
        self, *, username: str, password: SecretStr
    ) -> tuple[str, Principal, AwareDatetime]:
        """``(token, principal, expires_at)``. Raises InvalidCredentialsError /
        AccountLockedError. Constant-time password check; failures are counted."""
        ...

    async def authenticate(self, *, token: str) -> tuple[Principal, AwareDatetime]:
        """``(principal, expires_at)`` for a live session. Raises SessionInvalidError."""
        ...

    async def logout(self, *, session_id: UUID) -> None:
        """Revoke one session (idempotent)."""
        ...


SessionView.model_rebuild()
