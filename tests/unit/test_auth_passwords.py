"""scrypt password hashing (ADR-0013 s1, s7) and the user CLI's password input. No database."""

from __future__ import annotations

import asyncio
import hashlib
import io

import pytest

from ken_contracts.auth import MAX_PASSWORD_CHARS, MIN_PASSWORD_CHARS
from ken_services.auth import cli
from ken_services.auth.passwords import (
    CURRENT_PARAMS,
    DUMMY_HASH,
    MAXMEM,
    ScryptParams,
    hash_password,
    hash_password_async,
    needs_rehash,
    parse_hash,
    verify_password,
    verify_password_async,
)
from ken_services.auth.provider import TOKEN_PATTERN, hash_token

CHEAP = ScryptParams(n=2**10, r=8, p=1)


def test_current_parameters_meet_the_adr() -> None:
    assert ScryptParams(n=2**17, r=8, p=1) == CURRENT_PARAMS
    assert MAXMEM == 192 * 1024 * 1024
    assert CURRENT_PARAMS.valid()


def test_stored_format_and_round_trip() -> None:
    stored = hash_password("correct horse battery", params=CHEAP)
    scheme, n, r, p, _salt, _key = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", "1024", "8", "1")
    parsed = parse_hash(stored)
    assert parsed is not None
    assert len(parsed[1]) == 16 and len(parsed[2]) == 32
    assert verify_password("correct horse battery", stored)
    assert not verify_password("correct horse batterY", stored)
    assert not verify_password("", stored)


def test_salts_are_random() -> None:
    assert hash_password("same password!", params=CHEAP) != hash_password(
        "same password!", params=CHEAP
    )


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "plaintext",
        "scrypt$1024$8$1$c2FsdA==",
        "bcrypt$1024$8$1$c2FsdA==$aGFzaA==",
        "scrypt$1000$8$1$c2FsdA==$aGFzaA==",  # n not a power of two
        f"scrypt${2**30}$8$1$c2FsdA==$aGFzaA==",  # absurd n: never hashed with it
        "scrypt$1024$99$1$c2FsdA==$aGFzaA==",
        "scrypt$1024$8$1$!!!$aGFzaA==",
        "scrypt$x$8$1$c2FsdA==$aGFzaA==",
    ],
)
def test_malformed_or_out_of_bounds_hashes_never_verify(stored: str) -> None:
    assert parse_hash(stored) is None
    assert not verify_password("anything at all", stored)
    assert needs_rehash(stored)


def test_needs_rehash_below_current_parameters() -> None:
    weak = hash_password("correct horse battery", params=CHEAP)
    assert needs_rehash(weak)
    assert not needs_rehash(weak, params=CHEAP)
    assert not needs_rehash(DUMMY_HASH)


def test_the_dummy_hash_is_well_formed_at_current_parameters() -> None:
    parsed = parse_hash(DUMMY_HASH)
    assert parsed is not None and parsed[0] == CURRENT_PARAMS
    assert not verify_password("password", DUMMY_HASH)


def test_out_of_bounds_parameters_are_refused() -> None:
    with pytest.raises(ValueError, match="out of bounds"):
        hash_password("x" * 12, params=ScryptParams(n=2**4, r=8, p=1))


async def test_async_helpers_run_concurrently_and_agree() -> None:
    hashes = await asyncio.gather(
        *(hash_password_async(f"password-{i:04d}", params=CHEAP) for i in range(4))
    )
    results = await asyncio.gather(
        *(verify_password_async(f"password-{i:04d}", h) for i, h in enumerate(hashes))
    )
    assert results == [True] * 4


def test_tokens_are_stored_as_sha256() -> None:
    token = "A" * 43
    assert hash_token(token) == hashlib.sha256(token.encode()).hexdigest()
    assert TOKEN_PATTERN.fullmatch(token)
    assert not TOKEN_PATTERN.fullmatch("A" * 42)
    assert not TOKEN_PATTERN.fullmatch("A" * 42 + "=")


# --------------------------------------------------------------------------- #
# CLI password input
# --------------------------------------------------------------------------- #
def test_password_from_stdin_drops_only_the_newline() -> None:
    value = "  spaced password  "
    assert cli.read_password(from_stdin=True, stdin=io.StringIO(value + "\n")) == value
    assert cli.read_password(from_stdin=True, stdin=io.StringIO(value + "\r\n")) == value


@pytest.mark.parametrize(
    "value",
    ["x" * (MIN_PASSWORD_CHARS - 1), "x" * (MAX_PASSWORD_CHARS + 1), "", "abc\x00defghijkl"],
)
def test_password_bounds_are_enforced(value: str) -> None:
    with pytest.raises(cli.UsageError):
        cli.read_password(from_stdin=True, stdin=io.StringIO(value + "\n"))


def test_prompted_password_must_be_repeated() -> None:
    answers = iter(["correct horse battery", "correct horse battery"])
    assert cli.read_password(from_stdin=False, prompt=lambda _p: next(answers)) == (
        "correct horse battery"
    )
    answers = iter(["correct horse battery", "different horse battery"])
    with pytest.raises(cli.UsageError, match="do not match"):
        cli.read_password(from_stdin=False, prompt=lambda _p: next(answers))


@pytest.mark.parametrize(
    "argv",
    [
        ["user-create", "--username", "Alice", "--role", "reviewer", "--display-name", "A"],
        ["user-create", "--username", "alice", "--role", "admin", "--display-name", "A"],
        ["user-create", "--username", "alice", "--role", "reviewer", "--password", "x" * 20],
        ["user-set-role", "--username", "alice"],
        ["nope"],
    ],
)
def test_bad_arguments_are_usage_errors_and_change_nothing(argv: list[str]) -> None:
    assert cli.main(argv) == cli.EXIT_USAGE


def test_a_short_stdin_password_is_refused_before_any_database_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))

    def no_db(*_a: object, **_k: object) -> None:
        raise AssertionError("must not run")

    monkeypatch.setattr(cli, "_run", no_db)
    argv = ["user-create", "--username", "alice", "--role", "reviewer", "--display-name", "A"]
    assert cli.main([*argv, "--password-stdin"]) == cli.EXIT_USAGE
    captured = capsys.readouterr()
    assert "short" not in captured.out + captured.err
