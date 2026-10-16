"""The autonomy CLI: usage, refusals and what it never prints.

The three things that matter: a publishing pass refuses to run without an operator identity,
``--mode`` can only make a run more conservative, and no refusal leaks a URL, a credential or
exception text.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ken_contracts.autonomy import AutonomyMode
from ken_contracts.jobs import JobKind
from ken_services.autonomy import cli
from ken_services.autonomy.job import job_kind_for


def test_the_job_kind_follows_the_effective_mode() -> None:
    """A shadow pass and a publishing pass must never share a lock: one writes nothing
    outside ``autonomy_decisions`` and the other can write to the CMS."""
    assert job_kind_for(AutonomyMode.SHADOW) is JobKind.AUTONOMY_SHADOW
    assert job_kind_for(AutonomyMode.GUARDED) is JobKind.AUTONOMY
    assert job_kind_for(AutonomyMode.AUTONOMOUS) is JobKind.AUTONOMY


def test_the_parser_accepts_the_documented_arguments() -> None:
    parsed = cli._parser().parse_args(
        [
            "evaluate",
            "--mode",
            "shadow",
            "--source",
            "11111111-1111-4111-8111-111111111111",
            "--plan",
            "22222222-2222-4222-8222-222222222222",
            "--as-user",
            "otto",
        ]
    )
    assert parsed.command == "evaluate"
    assert parsed.mode == "shadow"
    assert parsed.sources == [uuid.UUID("11111111-1111-4111-8111-111111111111")]
    assert parsed.plans == [uuid.UUID("22222222-2222-4222-8222-222222222222")]
    assert parsed.as_user == "otto"
    assert parsed.dry_run is False


def test_a_malformed_uuid_is_a_usage_error_and_never_echoed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli._parser().parse_args(["evaluate", "--source", "not-a-uuid"])
    assert exit_info.value.code == 2
    assert "not-a-uuid" not in capsys.readouterr().err


def test_a_publishing_pass_refuses_without_an_operator_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``deployments.actor`` carries a ``^user:`` CHECK and ``user_id`` is NOT NULL, so a CMS
    write has to be attributable to a real account. This is the refusal that says so."""
    from tests.conftest import make_settings

    settings = make_settings(autonomy_mode="autonomous", autonomy_autonomous_acknowledged=True)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    called: list[str] = []
    monkeypatch.setattr(
        cli,
        "run_autonomy",
        lambda **kw: called.append("ran"),  # pragma: no cover
    )

    code = cli.main(["evaluate"])

    assert code == cli.EXIT_USAGE
    assert called == []
    err = capsys.readouterr().err
    assert "--as-user" in err
    assert "postgresql" not in err and "://" not in err


def test_a_run_may_not_ask_for_a_laxer_mode_than_the_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.conftest import make_settings

    settings = make_settings()  # shadow
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    class Refusing:
        def resolve_mode(self, requested: Any) -> AutonomyMode:
            raise ValueError("a run cannot select a more permissive autonomy mode")

        async def decidable_plans(self, **_kw: Any) -> list[uuid.UUID]:  # pragma: no cover
            return []

    monkeypatch.setattr(cli, "build_engine", lambda **_kw: Refusing())
    monkeypatch.setattr(cli, "create_engine", lambda _s: _FakeEngine())

    code = cli.main(["evaluate", "--mode", "autonomous"])

    assert code == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "usage error" in err
    assert "ValueError" in err  # the CLASS only


def test_a_configuration_refusal_names_no_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ken_core.config import ConfigurationError

    def refuse() -> Any:
        raise ConfigurationError("KEN_AUTONOMY_MODE=autonomous requires an acknowledgement")

    monkeypatch.setattr(cli, "get_settings", refuse)

    assert cli.main(["evaluate"]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert err.strip() == "configuration error (ConfigurationError)"


class _FakeEngine:
    async def dispose(self) -> None:
        return None


def test_the_refusal_texts_name_no_account_and_no_url() -> None:
    for message in (cli.NO_USER_REFUSAL, cli.ROLE_REFUSAL, cli.UNKNOWN_USER_REFUSAL):
        assert "://" not in message
        assert "password" not in message.lower()
