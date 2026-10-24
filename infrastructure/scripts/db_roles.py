"""Create/update, and audit, the least-privilege application database role.

    .venv/bin/python infrastructure/scripts/db_roles.py apply [--write-env] [--rotate-password]
    .venv/bin/python infrastructure/scripts/db_roles.py check

``apply`` runs as the OWNER role (``KEN_MIGRATION_DATABASE_URL``, else ``KEN_DATABASE_URL``)
against the database that DSN names, creates ``ken_app`` if absent and (re)applies
infrastructure/sql/app_role.sql. ``check`` inspects the live grants and exits non-zero when the
application role is a superuser, owns anything, or can TRUNCATE / run DDL.

Passwords: ``KEN_APP_DB_PASSWORD`` if the operator set it, else generated at runtime with
``secrets.token_urlsafe``. A password is never printed, logged, or written anywhere except the
gitignored ``.env`` (``--write-env``, mode 0600, only keys not already defined there).

Refuses port 5432 (the developer's own PostgreSQL) and any database not named ``ken_*``.
This is NOT an Alembic migration: roles are cluster-level.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

REPO_ROOT = Path(__file__).resolve().parents[2]
SQL_PATH = REPO_ROOT / "infrastructure" / "sql" / "app_role.sql"
APP_ROLE = "ken_app"
NATIVE_POSTGRES_PORT = 5432

# Every query below is read-only catalog inspection; ``{role}`` is always APP_ROLE or the
# connection's own CURRENT_USER, never operator input.
_ROLE_SQL = text(
    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, oid "
    "FROM pg_roles WHERE rolname = :role"
)
_OWNED_SQL = text(
    "SELECT count(*) FROM pg_shdepend WHERE refclassid = 'pg_authid'::regclass "
    "AND refobjid = :oid AND deptype = 'o'"
)
_MEMBER_OF_SQL = text(
    "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
    "WHERE m.member = :oid ORDER BY 1"
)
_TABLE_PRIV_SQL = text(
    "SELECT c.relname, p.priv FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public' "
    "CROSS JOIN (VALUES ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) AS p(priv) "
    "WHERE c.relkind IN ('r', 'p') AND has_table_privilege(:oid_role, c.oid, p.priv) "
    "ORDER BY 1, 2"
)
_DDL_SQL = text(
    "SELECT has_schema_privilege(:r, 'public', 'CREATE'), "
    "has_database_privilege(:r, current_database(), 'CREATE')"
)


class RoleError(Exception):
    """A refusal or a failed audit. The message never contains a credential."""


def assert_safe_target(url: URL) -> None:
    """Refuse the developer's own PostgreSQL and any database not named ``ken_*``."""
    if url.port == NATIVE_POSTGRES_PORT or url.port is None:
        raise RoleError(
            "refusing to manage roles on port 5432 (or an unspecified port): that is the "
            "developer's own PostgreSQL. Use the project container on 127.0.0.1:5433."
        )
    if not (url.database or "").startswith("ken_"):
        raise RoleError("refusing to manage roles in a database whose name does not start 'ken_'")


async def audit_role(connection: AsyncConnection, role: str) -> list[str]:
    """Violations of the least-privilege contract for ``role`` (empty list = compliant)."""
    row = (await connection.execute(_ROLE_SQL, {"role": role})).one_or_none()
    if row is None:
        return [f"role {role!r} does not exist"]
    sup, createdb, createrole, repl, bypass, oid = row
    problems: list[str] = []
    for flag, name in (
        (sup, "SUPERUSER"),
        (createdb, "CREATEDB"),
        (createrole, "CREATEROLE"),
        (repl, "REPLICATION"),
        (bypass, "BYPASSRLS"),
    ):
        if flag:
            problems.append(f"role has {name}")
    owned: int = (await connection.execute(_OWNED_SQL, {"oid": oid})).scalar_one()
    if owned:
        problems.append(f"role owns {owned} object(s)")
    memberships: Sequence[str] = (
        (await connection.execute(_MEMBER_OF_SQL, {"oid": oid})).scalars().all()
    )
    if memberships:
        problems.append(f"role is a member of: {', '.join(memberships)}")
    privs = (await connection.execute(_TABLE_PRIV_SQL, {"oid_role": role})).all()
    by_priv: dict[str, list[str]] = {}
    for table, priv in ((str(t), str(p)) for t, p in privs):
        by_priv.setdefault(priv, []).append(table)
    for priv, tables in sorted(by_priv.items()):
        shown = ", ".join(tables[:3]) + (", ..." if len(tables) > 3 else "")
        problems.append(f"role holds {priv} on {len(tables)} table(s) ({shown})")
    create_schema, create_db = (await connection.execute(_DDL_SQL, {"r": role})).one()
    if create_schema:
        problems.append("role holds CREATE on schema public")
    if create_db:
        problems.append("role holds CREATE on the database")
    return problems


def owner_url_from_env() -> URL:
    """The owner DSN: KEN_MIGRATION_DATABASE_URL, else KEN_DATABASE_URL (via Settings/.env)."""
    sys.path.insert(0, str(REPO_ROOT / "services"))
    from ken_core.config import Settings

    return make_url(Settings().effective_migration_database_url)


def _read_env_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            keys.add(stripped.split("=", 1)[0].strip())
    return keys


def write_env(env_path: Path, *, app_dsn: str, owner_dsn: str) -> list[str]:
    """Append the two DSNs to ``env_path`` unless it already defines them. Returns keys written."""
    defined = _read_env_keys(env_path)
    wanted = {"KEN_DATABASE_URL": app_dsn, "KEN_MIGRATION_DATABASE_URL": owner_dsn}
    missing = {key: value for key, value in wanted.items() if key not in defined}
    if not missing:
        return []
    existing = env_path.read_text() if env_path.exists() else ""
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    block = "".join(f"{key}={value}\n" for key, value in missing.items())
    env_path.touch(mode=0o600, exist_ok=True)
    env_path.chmod(0o600)  # touch() only applies the mode to a NEW file
    with env_path.open("a") as handle:
        handle.write(prefix + "# least-privilege roles (make db-roles)\n" + block)
    return sorted(missing)


async def _raw_execute(connection: AsyncConnection, sql: str) -> None:
    """Run a (possibly multi-statement) script on the open transaction via asyncpg."""
    raw = await connection.get_raw_connection()
    await raw.driver_connection.execute(sql)  # type: ignore[union-attr]


def scram_verifier(password: str, *, iterations: int = 4096) -> str:
    """PostgreSQL SCRAM-SHA-256 verifier (RFC 5802/7677) for ``password``.

    Sending this instead of the plaintext means the password never appears in SQL text, so
    ``log_statement``/``log_min_error_statement`` or ``pg_stat_statements`` cannot capture it.
    A verifier is salted and one-way (it still must not be published, but it is not the secret).
    """
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode()

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


async def apply_role(owner: URL, *, password: str | None, rotate: bool) -> tuple[bool, str | None]:
    """Create/update ``ken_app``. Returns (role_was_created, password_in_effect_or_None)."""
    assert_safe_target(owner)
    engine = create_async_engine(owner, poolclass=NullPool, connect_args={"timeout": 5})
    try:
        async with engine.begin() as connection:
            exists = (
                await connection.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}
                )
            ).scalar_one_or_none()
            created = exists is None
            if created:
                await connection.execute(text(f'CREATE ROLE "{APP_ROLE}" LOGIN'))
            effective = password
            if effective is None and (created or rotate):
                effective = secrets.token_urlsafe(32)
            if effective is not None:
                # Only a SCRAM verifier crosses the wire, never the plaintext (see scram_verifier);
                # format(%L) quotes it server-side.
                statement: str = (
                    await connection.execute(
                        text(
                            "SELECT format('ALTER ROLE %I PASSWORD %L', cast(:r AS text), "
                            "cast(:p AS text))"
                        ),
                        {"r": APP_ROLE, "p": scram_verifier(effective)},
                    )
                ).scalar_one()
                await _raw_execute(connection, statement)
            await _raw_execute(connection, SQL_PATH.read_text())
        return created, effective
    finally:
        await engine.dispose()


async def check_role(owner: URL) -> list[str]:
    assert_safe_target(owner)
    engine = create_async_engine(owner, poolclass=NullPool, connect_args={"timeout": 5})
    try:
        async with engine.connect() as connection:
            return await audit_role(connection, APP_ROLE)
    finally:
        await engine.dispose()


def app_url(owner: URL, password: str) -> URL:
    return owner.set(username=APP_ROLE, password=password)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("command", choices=("apply", "check"))
    parser.add_argument("--write-env", action="store_true")
    parser.add_argument("--rotate-password", action="store_true")
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    args = parser.parse_args(argv)

    try:
        owner = owner_url_from_env()
        assert_safe_target(owner)
        if args.command == "check":
            problems = asyncio.run(check_role(owner))
            if problems:
                for problem in problems:
                    print(f"FAIL  {APP_ROLE}: {problem}")
                return 1
            print(f"OK    {APP_ROLE}: no superuser/DDL/TRUNCATE privilege, owns nothing")
            return 0
        supplied = os.environ.get("KEN_APP_DB_PASSWORD") or None
        created, password = asyncio.run(
            apply_role(owner, password=supplied, rotate=args.rotate_password)
        )
        print(f"{'created' if created else 'updated'} role {APP_ROLE} in {owner.database}")
        if args.write_env:
            if password is None:
                raise RoleError(
                    f"{APP_ROLE} already exists and its password is not known here; re-run "
                    "with --rotate-password (or set KEN_APP_DB_PASSWORD) to use --write-env"
                )
            owner_dsn = owner.render_as_string(hide_password=False)
            app_dsn = app_url(owner, password).render_as_string(hide_password=False)
            written = write_env(args.env_file, app_dsn=app_dsn, owner_dsn=owner_dsn)
            print(
                f"wrote {', '.join(written)} to {args.env_file.name}"
                if written
                else f"{args.env_file.name} already defines both DSNs; left unchanged"
            )
        return 0
    except RoleError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # no str(exc): driver messages can echo a DSN
        print(f"failed: {type(exc).__name__}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
