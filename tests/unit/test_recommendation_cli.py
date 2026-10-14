"""``python -m ken_services.recommendations.cli recommend``: arguments, exit codes, output.

No database and no model: ``run_recommendations``, the policy loader and the provider
factory are replaced.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail
from ken_services.jobs.runner import JobAlreadyRunningError
from ken_services.recommendations import cli, pipeline
from tests.conftest import FakeModelProvider, make_settings

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
RUN_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
PAGE_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
PAGE_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
POLICY: Any = object()  # opaque here: the CLI only hands them to run_recommendations
RANKING: Any = object()


def _detail(status: RunStatus) -> JobRunDetail:
    return JobRunDetail(
        id=RUN_ID,
        kind=JobKind.RECOMMENDATION,
        status=status,
        triggered_by="cli",
        started_at=NOW,
        completed_at=NOW,
        counters={"plans_created": 3, "errors": 0 if status is RunStatus.SUCCEEDED else 1},
        config_snapshot={},
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
    monkeypatch.setattr(pipeline, "load_policies", lambda settings: (POLICY, RANKING))
    return fake


def _patch_run(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> _Recorder:
    recorder = _Recorder(outcome)
    monkeypatch.setattr(pipeline, "run_recommendations", recorder)
    return recorder


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (RunStatus.SUCCEEDED, cli.EXIT_OK),
        (RunStatus.COMPLETED_WITH_ERRORS, cli.EXIT_RUN_ERRORS),
        (RunStatus.FAILED, cli.EXIT_RUN_ERRORS),
    ],
)
def test_recommend_prints_one_json_line_and_maps_the_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: FakeModelProvider,
    status: RunStatus,
    code: int,
) -> None:
    recorder = _patch_run(monkeypatch, _detail(status))
    assert cli.main(["recommend"]) == code
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    line = json.loads(out[0])
    assert line["run_id"] == str(RUN_ID)
    assert (line["kind"], line["status"]) == ("recommendation", status.value)
    assert line["counters"]["plans_created"] == 3
    [kwargs] = recorder.kwargs
    assert kwargs["triggered_by"] == "cli"
    assert kwargs["source_page_ids"] is None
    assert (kwargs["policy"], kwargs["ranking"]) == (POLICY, RANKING)
    assert kwargs["model_provider"] is provider
    assert provider.closed == 1  # the provider is always closed


def test_sources_reach_the_job(
    monkeypatch: pytest.MonkeyPatch, provider: FakeModelProvider
) -> None:
    recorder = _patch_run(monkeypatch, _detail(RunStatus.SUCCEEDED))
    assert cli.main(["recommend", "--source", str(PAGE_A), "--source", str(PAGE_B)]) == 0
    assert recorder.kwargs[0]["source_page_ids"] == [PAGE_A, PAGE_B]


@pytest.mark.parametrize(
    "argv", [[], ["recommend", "--source", "not-a-uuid"], ["recommend", "--source"], ["publish"]]
)
def test_usage_errors_exit_2_and_run_nothing(
    monkeypatch: pytest.MonkeyPatch, provider: FakeModelProvider, argv: list[str]
) -> None:
    recorder = _patch_run(monkeypatch, _detail(RunStatus.SUCCEEDED))
    assert cli.main(argv) == cli.EXIT_USAGE
    assert recorder.kwargs == []


def test_already_running_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: FakeModelProvider,
) -> None:
    _patch_run(monkeypatch, JobAlreadyRunningError("another recommendation run is in progress"))
    assert cli.main(["recommend"]) == cli.EXIT_ALREADY_RUNNING
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
    assert cli.main(["recommend"]) == cli.EXIT_RUN_ERRORS
    captured = capsys.readouterr()
    assert "OSError" in captured.err
    assert "postgresql" not in captured.err + captured.out
    assert "ken:ken" not in captured.err + captured.out


def test_an_unloadable_policy_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: FakeModelProvider,
) -> None:
    recorder = _patch_run(monkeypatch, _detail(RunStatus.SUCCEEDED))

    def broken(settings: Any) -> Any:
        raise FileNotFoundError("/secret/path/recommendation.v1.yaml is missing")

    monkeypatch.setattr(pipeline, "load_policies", broken)
    assert cli.main(["recommend"]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "FileNotFoundError" in err and "recommendation policy" in err
    assert "secret" not in err  # no exception text (it carries a path)
    assert recorder.kwargs == []


def test_invalid_settings_are_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ken_core.config import ConfigurationError

    def broken() -> Any:
        raise ConfigurationError("KEN_DATABASE_URL=postgresql://ken:ken@x is invalid")

    monkeypatch.setattr(cli, "get_settings", broken)
    assert cli.main(["recommend"]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "configuration error" in err and "ken:ken" not in err


def test_the_real_policy_files_load() -> None:
    policy, ranking = pipeline.load_policies(make_settings())
    assert (policy.policy, ranking.policy) == ("recommendation", "ranking")
    assert policy.version >= 1 and ranking.version >= 1
