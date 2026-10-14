"""``python -m ken_services.intelligence.cli analyze``: arguments, exit codes, output.

No database and no model: ``run_analysis``, the taxonomy loader and the
provider factory are replaced.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail
from ken_services.intelligence import cli, pipeline
from ken_services.jobs.runner import JobAlreadyRunningError
from tests.conftest import FakeModelProvider, make_settings

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
RUN_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
TAXONOMY: Any = object()  # opaque here: the CLI only hands it to run_analysis


def _detail(status: RunStatus) -> JobRunDetail:
    return JobRunDetail(
        id=RUN_ID,
        kind=JobKind.ANALYSIS,
        status=status,
        triggered_by="cli",
        started_at=NOW,
        completed_at=NOW,
        counters={"pages_analyzed": 2, "errors": 0 if status is RunStatus.SUCCEEDED else 1},
        config_snapshot={"force": False},
        failure_reason="job aborted by an unexpected error (OSError)"
        if status is RunStatus.FAILED
        else None,
    )


class _Recorder:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.kwargs: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> FakeModelProvider:
    fake = FakeModelProvider()
    monkeypatch.setattr(cli, "get_settings", lambda: make_settings())
    monkeypatch.setattr(cli, "build_model_provider", lambda settings: fake)
    monkeypatch.setattr(pipeline, "load_policy", lambda path: TAXONOMY)
    return fake


def _patch_run(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> _Recorder:
    recorder = _Recorder(outcome)
    monkeypatch.setattr(pipeline, "run_analysis", recorder)
    return recorder


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (RunStatus.SUCCEEDED, cli.EXIT_OK),
        (RunStatus.COMPLETED_WITH_ERRORS, cli.EXIT_RUN_ERRORS),
        (RunStatus.FAILED, cli.EXIT_RUN_ERRORS),
    ],
)
def test_analyze_prints_one_json_line_and_maps_the_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: FakeModelProvider,
    status: RunStatus,
    code: int,
) -> None:
    recorder = _patch_run(monkeypatch, _detail(status))
    assert cli.main(["analyze"]) == code
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    line = json.loads(out[0])
    assert line["run_id"] == str(RUN_ID)
    assert (line["kind"], line["status"]) == ("analysis", status.value)
    assert line["counters"]["pages_analyzed"] == 2
    [kwargs] = recorder.kwargs
    assert (kwargs["force"], kwargs["embeddings"], kwargs["triggered_by"]) == (False, True, "cli")
    assert kwargs["taxonomy"] is TAXONOMY
    assert kwargs["model_provider"] is provider
    assert provider.closed == 1  # the provider is always closed


def test_flags_reach_the_job(monkeypatch: pytest.MonkeyPatch, provider: FakeModelProvider) -> None:
    recorder = _patch_run(monkeypatch, _detail(RunStatus.SUCCEEDED))
    assert cli.main(["analyze", "--force", "--no-embeddings"]) == cli.EXIT_OK
    assert (recorder.kwargs[0]["force"], recorder.kwargs[0]["embeddings"]) == (True, False)


def test_already_running_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: FakeModelProvider,
) -> None:
    _patch_run(monkeypatch, JobAlreadyRunningError("another analysis run is in progress"))
    assert cli.main(["analyze"]) == cli.EXIT_ALREADY_RUNNING
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "in progress" in captured.err
    assert provider.closed == 1


def test_unexpected_error_names_the_class_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: FakeModelProvider,
) -> None:
    _patch_run(monkeypatch, OSError("could not connect to postgresql://ken:ken@localhost:5433/x"))
    assert cli.main(["analyze"]) == cli.EXIT_RUN_ERRORS
    captured = capsys.readouterr()
    assert "OSError" in captured.err
    assert "postgresql" not in captured.err + captured.out
    assert "ken:ken" not in captured.err + captured.out


def test_an_unloadable_taxonomy_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: FakeModelProvider,
) -> None:
    recorder = _patch_run(monkeypatch, _detail(RunStatus.SUCCEEDED))

    def broken(path: str) -> Any:
        raise FileNotFoundError(f"{path} is missing")

    monkeypatch.setattr(pipeline, "load_policy", broken)
    assert cli.main(["analyze"]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "FileNotFoundError" in err and "taxonomy" in err
    assert "missing" not in err  # no exception text (it carries a path)
    assert recorder.kwargs == []


def test_invalid_settings_are_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ken_core.config import ConfigurationError

    def bad() -> Any:
        raise ConfigurationError("KEN_DATABASE_URL=postgresql://secret")

    monkeypatch.setattr(cli, "get_settings", bad)
    assert cli.main(["analyze"]) == cli.EXIT_USAGE
    assert "secret" not in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["crawl"], ["analyze", "--bogus"]])
def test_bad_arguments_are_usage_errors(argv: list[str]) -> None:
    assert cli.main(argv) == cli.EXIT_USAGE


def test_help_exits_ok() -> None:
    assert cli.main(["analyze", "--help"]) == cli.EXIT_OK
