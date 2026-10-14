"""LocalAuthProvider and the user CLI on the ``*_test`` database (ADR-0013 s1, s7)."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from datetime import timedelta

import pytest
from pydantic import SecretStr

from ken_contracts.auth import (
    AccountLockedError,
    InvalidCredentialsError,
    Role,
    SessionInvalidError,
)
from ken_services.auth import cli
from ken_services.auth.passwords import CURRENT_PARAMS, ScryptParams, parse_hash
from tests.integration.recommendation_support import SM, rows, scalar
from tests.integration.review_support import PASSWORD, Clock, auth_provider, create_user

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]


def _pw(value: str = PASSWORD) -> SecretStr:
    return SecretStr(value)


async def test_login_authenticate_logout(sessionmaker: SM) -> None:
    clock = Clock()
    provider = auth_provider(sessionmaker, clock=clock)
    user_id = await create_user(sessionmaker, "alice", role=Role.REVIEWER)
    token, principal, expires = await provider.login(username="alice", password=_pw())
    assert len(token) == 43
    assert principal.user_id == user_id and principal.actor == "user:alice"
    assert expires == clock.now + timedelta(hours=12)
    stored = await rows(sessionmaker, "SELECT token_hash, expires_at FROM auth_sessions")
    assert stored[0].token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(await rows(sessionmaker, "SELECT * FROM auth_sessions"))

    again, until = await provider.authenticate(token=token)
    assert again == principal and until == expires
    await provider.logout(session_id=principal.session_id)
    await provider.logout(session_id=principal.session_id)  # idempotent
    with pytest.raises(SessionInvalidError):
        await provider.authenticate(token=token)
    last_login = await scalar(sessionmaker, "SELECT last_login_at FROM users")
    assert last_login is not None


async def test_usernames_are_matched_case_insensitively(sessionmaker: SM) -> None:
    await create_user(sessionmaker, "alice")
    _token, principal, _ = await auth_provider(sessionmaker).login(username="Alice", password=_pw())
    assert principal.username == "alice"


@pytest.mark.parametrize(
    "token", ["", "short", "A" * 43, "A" * 42 + "=", "x" * 200, "Bearer " + "A" * 43]
)
async def test_unknown_or_malformed_tokens_are_invalid(sessionmaker: SM, token: str) -> None:
    with pytest.raises(SessionInvalidError):
        await auth_provider(sessionmaker).authenticate(token=token)


async def test_expired_sessions_are_invalid(sessionmaker: SM) -> None:
    clock = Clock()
    provider = auth_provider(sessionmaker, clock=clock)
    await create_user(sessionmaker, "alice")
    token, _p, _e = await provider.login(username="alice", password=_pw())
    clock.advance(hours=12)
    with pytest.raises(SessionInvalidError):
        await provider.authenticate(token=token)


async def test_every_credential_failure_is_the_same_error(sessionmaker: SM) -> None:
    provider = auth_provider(sessionmaker)
    await create_user(sessionmaker, "alice")
    await create_user(sessionmaker, "bob")
    async with sessionmaker() as session, session.begin():
        from sqlalchemy import text

        await session.execute(text("UPDATE users SET disabled = true WHERE username = 'bob'"))
    for username, password in (
        ("nobody", PASSWORD),
        ("alice", "wrong password entirely"),
        ("bob", PASSWORD),  # disabled: indistinguishable
        ("alice", "x" * 300),
    ):
        with pytest.raises(InvalidCredentialsError) as info:
            await provider.login(username=username, password=_pw(password))
        assert type(info.value) is InvalidCredentialsError
        assert str(info.value) == ""


async def test_five_failures_lock_for_fifteen_minutes_and_success_resets(
    sessionmaker: SM,
) -> None:
    clock = Clock()
    provider = auth_provider(sessionmaker, clock=clock)
    await create_user(sessionmaker, "alice")
    for attempt in range(4):
        with pytest.raises(InvalidCredentialsError):
            await provider.login(username="alice", password=_pw("bad password number"))
        assert await scalar(sessionmaker, "SELECT failed_logins FROM users") == attempt + 1
    await provider.login(username="alice", password=_pw())  # success resets the counter
    assert await scalar(sessionmaker, "SELECT failed_logins FROM users") == 0

    for _ in range(5):
        with pytest.raises(InvalidCredentialsError):
            await provider.login(username="alice", password=_pw("bad password number"))
    locked_until = await scalar(sessionmaker, "SELECT locked_until FROM users")
    assert locked_until == clock.now + timedelta(minutes=15)
    # Locked: even the right password is refused (the API answers with the same 401).
    with pytest.raises(AccountLockedError):
        await provider.login(username="alice", password=_pw())
    with pytest.raises(AccountLockedError):
        await provider.login(username="alice", password=_pw("bad password number"))
    clock.advance(minutes=15, seconds=1)
    await provider.login(username="alice", password=_pw())
    state = (await rows(sessionmaker, "SELECT failed_logins, locked_until FROM users"))[0]
    assert (state.failed_logins, state.locked_until) == (0, None)


async def test_concurrent_failures_are_counted_atomically(sessionmaker: SM) -> None:
    provider = auth_provider(sessionmaker, max_failed_logins=50)
    await create_user(sessionmaker, "alice")

    async def fail() -> None:
        with pytest.raises(InvalidCredentialsError):
            await provider.login(username="alice", password=_pw("bad password number"))

    await asyncio.gather(*(fail() for _ in range(8)))
    assert await scalar(sessionmaker, "SELECT failed_logins FROM users") == 8


async def test_a_weak_hash_is_rehashed_at_login(sessionmaker: SM) -> None:
    stronger = ScryptParams(n=2**11, r=8, p=1)
    await create_user(sessionmaker, "alice")  # cheap (2**10) hash
    await auth_provider(sessionmaker).login(username="alice", password=_pw())
    hashed = await scalar(sessionmaker, "SELECT password_hash FROM users")
    assert parse_hash(hashed) is not None and parse_hash(hashed)[0].n == 2**10  # type: ignore[index]
    from ken_services.auth.provider import LocalAuthProvider

    await LocalAuthProvider(sessionmaker=sessionmaker, params=stronger).login(
        username="alice", password=_pw()
    )
    rehashed = await scalar(sessionmaker, "SELECT password_hash FROM users")
    assert parse_hash(rehashed)[0] == stronger  # type: ignore[index]
    assert CURRENT_PARAMS.n > stronger.n  # production would re-hash again, upward only


async def test_authenticate_rereads_role_and_disabled_every_request(sessionmaker: SM) -> None:
    provider = auth_provider(sessionmaker)
    await create_user(sessionmaker, "alice", role=Role.OPERATOR_ADMIN)
    token, _p, _e = await provider.login(username="alice", password=_pw())
    from sqlalchemy import text

    async with sessionmaker() as session, session.begin():
        await session.execute(text("UPDATE users SET role = 'viewer'"))
    principal, _ = await provider.authenticate(token=token)
    assert principal.role is Role.VIEWER
    async with sessionmaker() as session, session.begin():
        await session.execute(text("UPDATE users SET disabled = true"))
    with pytest.raises(SessionInvalidError):
        await provider.authenticate(token=token)


async def test_last_seen_is_written_at_most_once_a_minute(sessionmaker: SM) -> None:
    clock = Clock()
    provider = auth_provider(sessionmaker, clock=clock)
    await create_user(sessionmaker, "alice")
    token, _p, _e = await provider.login(username="alice", password=_pw())
    await provider.authenticate(token=token)
    first = await scalar(sessionmaker, "SELECT last_seen_at FROM auth_sessions")
    assert first == clock.now
    clock.advance(seconds=30)
    await provider.authenticate(token=token)
    assert await scalar(sessionmaker, "SELECT last_seen_at FROM auth_sessions") == first
    clock.advance(seconds=31)
    await provider.authenticate(token=token)
    assert await scalar(sessionmaker, "SELECT last_seen_at FROM auth_sessions") == clock.now


# --------------------------------------------------------------------------- #
# CLI (execute() against the test database; main() is covered by unit tests)
# --------------------------------------------------------------------------- #
def _args(command: str, **values: object) -> argparse.Namespace:
    return argparse.Namespace(command=command, **values)


async def test_cli_user_lifecycle_revokes_sessions(
    sessionmaker: SM, capsys: pytest.CaptureFixture[str]
) -> None:
    await cli.execute(
        _args("user-create", username="carol", role=Role.REVIEWER, display_name="Carol"),
        sessionmaker=sessionmaker,
        password=PASSWORD,
    )
    with pytest.raises(cli.OperationFailedError, match="already exists"):
        await cli.execute(
            _args("user-create", username="carol", role=Role.VIEWER, display_name="C"),
            sessionmaker=sessionmaker,
            password=PASSWORD,
        )
    # The CLI hashes with the PRODUCTION parameters.
    stored = await scalar(sessionmaker, "SELECT password_hash FROM users")
    assert parse_hash(stored)[0] == CURRENT_PARAMS  # type: ignore[index]

    provider = auth_provider(sessionmaker)
    for command, extra, password in (
        ("user-set-role", {"role": Role.VIEWER}, None),
        ("user-set-password", {}, "a brand new passphrase"),
        ("user-disable", {}, None),
    ):
        login_password = "a brand new passphrase" if command == "user-disable" else PASSWORD
        token, _p, _e = await provider.login(username="carol", password=_pw(login_password))
        await cli.execute(
            _args(command, username="carol", **extra), sessionmaker=sessionmaker, password=password
        )
        with pytest.raises(SessionInvalidError):
            await provider.authenticate(token=token)
    assert await scalar(sessionmaker, "SELECT role FROM users") == "viewer"
    with pytest.raises(InvalidCredentialsError):
        await provider.login(username="carol", password=_pw("a brand new passphrase"))
    await cli.execute(_args("user-enable", username="carol"), sessionmaker=sessionmaker)
    await provider.login(username="carol", password=_pw("a brand new passphrase"))

    capsys.readouterr()
    await cli.execute(_args("user-list"), sessionmaker=sessionmaker)
    out = capsys.readouterr().out
    assert '"username": "carol"' in out and "scrypt" not in out
    assert PASSWORD not in out
    with pytest.raises(cli.OperationFailedError, match="no such user"):
        await cli.execute(_args("user-disable", username="nobody"), sessionmaker=sessionmaker)


async def test_cli_prunes_only_long_expired_sessions(
    sessionmaker: SM, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy import text

    user_id = await create_user(sessionmaker, "alice")
    async with sessionmaker() as session, session.begin():
        for days, token in ((-40, "a" * 64), (-10, "b" * 64), (1, "c" * 64)):
            await session.execute(
                text(
                    "INSERT INTO auth_sessions (id, user_id, token_hash, created_at, expires_at) "
                    "VALUES (gen_random_uuid(), :u, :h, now() + make_interval(days => :d) - "
                    "interval '12 hours', now() + make_interval(days => :d))"
                ),
                {"u": user_id, "h": token, "d": days},
            )
    capsys.readouterr()
    await cli.execute(_args("sessions-prune"), sessionmaker=sessionmaker)
    assert '"pruned_sessions": 1' in capsys.readouterr().out
    assert await scalar(sessionmaker, "SELECT count(*) FROM auth_sessions") == 2
