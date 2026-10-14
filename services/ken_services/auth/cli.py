"""User administration: ``python -m ken_services.auth.cli <command>`` (ADR-0013 section 1).

The ONLY way accounts are created or changed -- there is no HTTP endpoint for it, and no
account exists after a fresh install (no default credentials)::

    user-create        --username U --role R --display-name D [--password-stdin]
    user-set-password  --username U [--password-stdin]
    user-set-role      --username U --role R
    user-disable       --username U
    user-enable        --username U
    user-list
    sessions-prune

* Passwords are read with ``getpass`` (asked twice) or, with ``--password-stdin``, as ONE
  line from standard input (the trailing newline is dropped). They must be 12-256
  characters. A password is never echoed, printed or logged, and never accepted as an
  argument (it would land in the shell history and the process list).
* ``user-disable``, ``user-set-role`` and ``user-set-password`` revoke every session of the
  user, so the change takes effect at once. ``user-enable`` also clears a lockout.
* ``sessions-prune`` deletes sessions that expired more than 30 days ago.

Prints one JSON line per result. Exit codes: 0 ok; 1 the operation failed (unknown user,
duplicate username, database error); 2 usage or input error (nothing was changed). Never
prints the database URL or exception text: errors name a class only.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import re
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Final, TextIO

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.auth import MAX_PASSWORD_CHARS, MIN_PASSWORD_CHARS, USERNAME_PATTERN, Role
from ken_core import logging as ken_logging
from ken_core.config import ConfigurationError, get_settings
from ken_core.db.engine import create_engine
from ken_core.db.repositories import auth as repo
from ken_core.db.session import create_sessionmaker
from ken_services.auth.passwords import hash_password_async

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
_USERNAME: Final = re.compile(USERNAME_PATTERN)
MAX_DISPLAY_NAME_CHARS: Final = 120


class UsageError(Exception):
    """Bad input; the message is fixed text (never a password)."""


class OperationFailedError(Exception):
    """The operation could not be applied; the message is fixed text."""


def _username(value: str) -> str:
    if _USERNAME.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "must be 3-64 characters: lower-case letters, digits, '.', '_' or '-'"
        )
    return value


def _role(value: str) -> Role:
    try:
        return Role(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "must be one of " + ", ".join(r.value for r in Role)
        ) from None


def _display_name(value: str) -> str:
    text = " ".join(value.split())
    if not 1 <= len(text) <= MAX_DISPLAY_NAME_CHARS or not text.isprintable():
        raise argparse.ArgumentTypeError("must be 1-120 printable characters")
    return text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ken_services.auth.cli",
        description="Administer local user accounts (passwords are never arguments).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def with_username(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--username", required=True, type=_username)
        return sub

    create = with_username("user-create", "Create a user (prompts for the password).")
    create.add_argument("--role", required=True, type=_role)
    create.add_argument("--display-name", required=True, type=_display_name)
    create.add_argument("--password-stdin", action="store_true")
    password = with_username("user-set-password", "Set a password; revokes the sessions.")
    password.add_argument("--password-stdin", action="store_true")
    role = with_username("user-set-role", "Change a role; revokes the sessions.")
    role.add_argument("--role", required=True, type=_role)
    with_username("user-disable", "Disable a user; revokes the sessions.")
    with_username("user-enable", "Re-enable a user and clear a lockout.")
    commands.add_parser("user-list", help="List users (never hashes).")
    commands.add_parser("sessions-prune", help="Delete sessions expired over 30 days ago.")
    return parser


def _check_password(password: str) -> str:
    if not MIN_PASSWORD_CHARS <= len(password) <= MAX_PASSWORD_CHARS:
        raise UsageError(
            f"the password must be {MIN_PASSWORD_CHARS}-{MAX_PASSWORD_CHARS} characters"
        )
    if "\x00" in password:
        raise UsageError("the password must not contain NUL")
    return password


def read_password(
    *,
    from_stdin: bool,
    stdin: TextIO | None = None,
    prompt: Callable[[str], str] = getpass.getpass,
) -> str:
    """The new password, validated. Raises :class:`UsageError`."""
    if from_stdin:
        line = (stdin or sys.stdin).readline()
        return _check_password(line.removesuffix("\n").removesuffix("\r"))
    first = _check_password(prompt("Password: "))
    if prompt("Repeat password: ") != first:
        raise UsageError("the passwords do not match")
    return first


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _now() -> datetime:
    return datetime.now(UTC)


async def _require_user(session: AsyncSession, username: str) -> repo.UserRecord:
    user = await repo.get_user_by_username(session, username=username)
    if user is None:
        raise OperationFailedError("no such user")
    return user


async def execute(
    args: argparse.Namespace,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    password: str | None = None,
) -> None:
    """Apply one parsed command. ``password`` is required by the two password commands."""
    command: str = args.command
    now = _now()
    if command == "user-list":
        async with sessionmaker() as session:
            users = await repo.list_users(session)
        for user in users:
            _emit(
                {
                    "username": user.username,
                    "display_name": user.display_name,
                    "role": user.role.value,
                    "disabled": user.disabled,
                    "locked": user.locked_until is not None and user.locked_until > now,
                    "last_login_at": None
                    if user.last_login_at is None
                    else user.last_login_at.isoformat(),
                }
            )
        return
    if command == "sessions-prune":
        async with sessionmaker() as session, session.begin():
            pruned = await repo.prune_sessions(session)
        _emit({"pruned_sessions": pruned})
        return

    hashed = None if password is None else await hash_password_async(password)
    if command == "user-create":
        if hashed is None:  # pragma: no cover - main always reads one
            raise UsageError("a password is required")
        try:
            async with sessionmaker() as session, session.begin():
                await repo.insert_user(
                    session,
                    user_id=uuid.uuid4(),
                    username=args.username,
                    display_name=args.display_name,
                    role=args.role,
                    password_hash=hashed,
                    now=now,
                )
        except IntegrityError:
            raise OperationFailedError("that username already exists") from None
        _emit({"created": args.username, "role": Role(args.role).value})
        return

    revoked = 0
    async with sessionmaker() as session, session.begin():
        user = await _require_user(session, args.username)
        if command == "user-set-password":
            await repo.update_user(
                session, user_id=user.id, now=now, password_hash=hashed, reset_lockout=True
            )
        elif command == "user-set-role":
            await repo.update_user(session, user_id=user.id, now=now, role=args.role)
        elif command == "user-disable":
            await repo.update_user(session, user_id=user.id, now=now, disabled=True)
        elif command == "user-enable":
            await repo.update_user(
                session, user_id=user.id, now=now, disabled=False, reset_lockout=True
            )
        else:  # pragma: no cover - argparse restricts the commands
            raise UsageError("unknown command")
        if command != "user-enable":
            revoked = await repo.revoke_user_sessions(session, user_id=user.id, now=now)
    _emit({"updated": args.username, "command": command, "revoked_sessions": revoked})


async def _run(args: argparse.Namespace, password: str | None) -> int:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        await execute(args, sessionmaker=create_sessionmaker(engine), password=password)
    except OperationFailedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:
        print(f"error: the operation failed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_FAILED
    finally:
        await engine.dispose()
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse has already printed its usage message
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK
    try:
        settings = get_settings()
    except (ValidationError, ConfigurationError):
        print("configuration error: invalid KEN_* settings", file=sys.stderr)
        return EXIT_USAGE
    ken_logging.configure_logging(level=settings.log_level, fmt=settings.log_format)
    password: str | None = None
    if args.command in {"user-create", "user-set-password"}:
        try:
            password = read_password(from_stdin=args.password_stdin)
        except UsageError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        except (EOFError, KeyboardInterrupt):
            print("error: no password was entered", file=sys.stderr)
            return EXIT_USAGE
    return asyncio.run(_run(args, password))


if __name__ == "__main__":
    raise SystemExit(main())
