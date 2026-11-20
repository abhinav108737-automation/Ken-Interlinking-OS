"""Bulk deployment CLI: the safety properties that do not need a database."""

from __future__ import annotations

import uuid

import pytest

from ken_services.deployment import batch_cli as cli
from ken_services.deployment.batch_cli import (
    DEFAULT_MAX_SOURCES,
    EXIT_USAGE,
    HARD_MAX_SOURCES,
    Tally,
    apply_operation_id,
    chunk,
    main,
    rollback_operation_id,
)

A, B, C = (uuid.UUID(int=n) for n in (1, 2, 3))
SOURCE = "00000000-0000-4000-8000-000000000001"


def test_operation_id_is_stable_and_order_independent() -> None:
    assert apply_operation_id([A, B, C]) == apply_operation_id([C, A, B])


def test_different_plans_get_different_operation_ids() -> None:
    assert apply_operation_id([A, B]) != apply_operation_id([A, C])


def test_apply_and_rollback_ids_never_collide() -> None:
    assert apply_operation_id([A]) != rollback_operation_id(A)


def test_chunking_respects_the_contract_limit() -> None:
    plans = [uuid.UUID(int=n + 1) for n in range(45)]
    parts = chunk(plans)
    assert [len(p) for p in parts] == [20, 20, 5]
    assert [p for part in parts for p in part] == plans


def test_the_hard_cap_is_enforced_by_the_parser() -> None:
    assert (
        main(["apply", "--as-user", "u", "--max-sources", str(HARD_MAX_SOURCES + 1)]) == EXIT_USAGE
    )
    assert main(["apply", "--as-user", "u", "--max-sources", "0"]) == EXIT_USAGE


def test_the_default_batch_is_small() -> None:
    assert DEFAULT_MAX_SOURCES <= 5 < HARD_MAX_SOURCES


def test_a_non_uuid_source_is_refused() -> None:
    assert main(["apply", "--as-user", "u", "--source", "nope"]) == EXIT_USAGE


def test_a_rollback_needs_a_target_and_a_reason() -> None:
    assert main(["rollback", "--as-user", "u", "--reason", "other"]) == EXIT_USAGE
    assert main(["rollback", "--as-user", "u", "--all-live"]) == EXIT_USAGE


def test_stop_after_must_be_positive() -> None:
    assert main(["apply", "--as-user", "u", "--stop-after", "0"]) == EXIT_USAGE


def test_apply_defaults_to_dry_run() -> None:
    args = cli._parser().parse_args(["apply", "--as-user", "u"])
    assert args.execute is False


def test_rollback_defaults_to_dry_run() -> None:
    args = cli._parser().parse_args(
        ["rollback", "--as-user", "u", "--all-live", "--reason", "broken_page"]
    )
    assert args.execute is False


def test_the_breaker_trips_only_on_consecutive_failures(capsys: pytest.CaptureFixture[str]) -> None:
    tally = Tally()
    for failed in (True, True, False, True, True):
        tally.record({"k": 1}, failed=failed, stop_after=3)
    assert not tally.tripped, "a success in between resets the run"
    tally.record({"k": 1}, failed=True, stop_after=3)
    assert tally.tripped
    capsys.readouterr()


def test_execute_is_refused_without_the_writes_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KEN_CMS_WRITES_ENABLED", "false")
    monkeypatch.setenv("KEN_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/none")
    code = main(["apply", "--as-user", "u", "--execute"])
    assert code == EXIT_USAGE
    assert "KEN_CMS_WRITES_ENABLED" in capsys.readouterr().err
