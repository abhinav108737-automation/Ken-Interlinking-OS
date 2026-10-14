"""``python -m ken_agents.cli agents``: usage, refusal and policy wiring (no database)."""

from __future__ import annotations

import pytest

from ken_agents import cli
from ken_contracts.agents import AgentPolicy
from tests.conftest import make_settings


def test_bad_source_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["agents", "--source", "not-a-uuid"]) == cli.EXIT_USAGE


def test_missing_command_is_a_usage_error() -> None:
    assert cli.main([]) == cli.EXIT_USAGE


def test_refuses_to_run_with_remote_tracing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_super_secret_value")
    monkeypatch.setattr(cli, "get_settings", lambda: pytest.fail("must refuse first"))
    assert cli.main(["agents"]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "LANGSMITH_API_KEY" in err
    assert "super_secret" not in err


def test_policies_load_from_the_configured_files() -> None:
    policy, recommendation_version, ranking_version = cli.load_policies(make_settings())
    assert isinstance(policy, AgentPolicy)
    assert policy.version == 1
    assert recommendation_version >= 1 and ranking_version >= 1


def test_policy_error_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings()
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    def broken(_: object) -> None:
        raise ValueError("/secret/path/agents.v1.yaml")

    monkeypatch.setattr(cli, "load_policies", broken)
    assert cli.main(["agents"]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "ValueError" in err and "/secret/path" not in err
