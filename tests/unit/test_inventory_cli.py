"""``python -m ken_services.inventory.cli``: arguments, exit codes, output. No DB, no network."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ken_contracts.inventory import (
    ContentSourceProvider,
    IngestionRunDetail,
    RunCounters,
    RunStatus,
    SourceType,
)
from ken_services.inventory import cli
from ken_services.inventory.ingest import IngestionAlreadyRunningError
from tests.conftest import make_settings

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
RUN_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


def _detail(status: RunStatus) -> IngestionRunDetail:
    return IngestionRunDetail(
        id=RUN_ID,
        source_type=SourceType.FILE_EXPORT,
        source_name="file_export",
        status=status,
        started_at=NOW,
        completed_at=NOW,
        counters=RunCounters(pages_fetched=2, pages_created=2, pages_discovered=2),
        config_snapshot={},
        failure_reason="ingestion aborted (OSError)" if status is RunStatus.FAILED else None,
    )


class _Recorder:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.sources: list[ContentSourceProvider] = []

    async def __call__(self, *, source: ContentSourceProvider, sessionmaker: Any) -> Any:
        self.sources.append(source)
        try:
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return self.outcome
        finally:
            await source.aclose()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    value = make_settings()
    monkeypatch.setattr(cli, "get_settings", lambda: value)
    return value


def _patch_run(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> _Recorder:
    recorder = _Recorder(outcome)
    monkeypatch.setattr(cli, "run_ingestion", recorder)
    return recorder


@pytest.fixture
def export_file(tmp_path: Path) -> Path:
    path = tmp_path / "export.jsonl"
    path.write_text(
        '{"format": "ken-inventory-export", "version": 1, "site_url": "https://site.test/"}\n'
    )
    return path


# --------------------------------------------------------------------------- #
# Exit codes and output
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (RunStatus.SUCCEEDED, 0),
        (RunStatus.COMPLETED_WITH_ERRORS, 1),
        (RunStatus.FAILED, 1),
    ],
)
def test_import_file_exit_code_follows_run_status(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    export_file: Path,
    status: RunStatus,
    code: int,
) -> None:
    recorder = _patch_run(monkeypatch, _detail(status))
    assert cli.main(["import-file", str(export_file)]) == code

    out = capsys.readouterr().out.strip().splitlines()
    summary = json.loads(out[-1])
    assert summary["run_id"] == str(RUN_ID)
    assert summary["status"] == status.value
    assert summary["counters"]["pages_created"] == 2
    [source] = recorder.sources
    assert source.source_type is SourceType.FILE_EXPORT


def test_output_never_contains_the_database_url(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    export_file: Path,
) -> None:
    _patch_run(monkeypatch, _detail(RunStatus.SUCCEEDED))
    cli.main(["import-file", str(export_file)])
    captured = capsys.readouterr()
    for text in (captured.out, captured.err):
        assert "ken:ken" not in text
        assert settings.database_url not in text


def test_already_running_exits_3(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    export_file: Path,
) -> None:
    _patch_run(monkeypatch, IngestionAlreadyRunningError("busy"))
    assert cli.main(["import-file", str(export_file)]) == 3
    assert "in progress" in capsys.readouterr().err


def test_unexpected_error_exits_1_with_class_name_only(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    export_file: Path,
) -> None:
    _patch_run(monkeypatch, RuntimeError("postgresql://u:hunter2@db/x"))
    assert cli.main(["import-file", str(export_file)]) == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "hunter2" not in err


# --------------------------------------------------------------------------- #
# Usage and configuration errors (exit 2, nothing run)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("argv", [[], ["bogus"], ["import-file"], ["import-cms-mock"]])
def test_bad_arguments_exit_2(
    settings: Any, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    recorder = _patch_run(monkeypatch, _detail(RunStatus.SUCCEEDED))
    assert cli.main(argv) == 2
    assert recorder.sources == []


def test_help_exits_0(settings: Any) -> None:
    assert cli.main(["--help"]) == 0


def test_missing_export_file_exits_2(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    recorder = _patch_run(monkeypatch, _detail(RunStatus.SUCCEEDED))
    assert cli.main(["import-file", str(tmp_path / "nope.jsonl")]) == 2
    assert recorder.sources == []
    assert "does not exist" in capsys.readouterr().err


def test_crawl_refuses_without_a_base_url(
    settings: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert settings.crawl_base_url is None
    recorder = _patch_run(monkeypatch, _detail(RunStatus.SUCCEEDED))
    assert cli.main(["crawl"]) == 2
    assert recorder.sources == []
    assert "KEN_CRAWL_BASE_URL" in capsys.readouterr().err


def test_invalid_settings_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _broken() -> Any:
        return make_settings(database_url="mysql://user:hunter2@db/x")

    monkeypatch.setattr(cli, "get_settings", _broken)
    assert cli.main(["crawl"]) == 2
    err = capsys.readouterr().err
    assert "configuration error" in err
    assert "hunter2" not in err


# --------------------------------------------------------------------------- #
# Source construction (real source classes; nothing is fetched or read)
# --------------------------------------------------------------------------- #
def test_crawl_builds_the_public_web_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    value = make_settings(crawl_base_url="https://site.test/")
    monkeypatch.setattr(cli, "get_settings", lambda: value)
    recorder = _patch_run(monkeypatch, _detail(RunStatus.SUCCEEDED))
    assert cli.main(["crawl"]) == 0
    [source] = recorder.sources
    assert source.source_type is SourceType.PUBLIC_WEB
    assert isinstance(source, ContentSourceProvider)


def test_import_cms_mock_builds_the_cms_source(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _patch_run(monkeypatch, _detail(RunStatus.SUCCEEDED))
    assert cli.main(["import-cms-mock", "--site-url", "https://site.test/"]) == 0
    [source] = recorder.sources
    assert source.source_type is SourceType.CMS
    assert isinstance(source, ContentSourceProvider)


def test_file_source_uses_the_configured_size_cap(export_file: Path) -> None:
    source = cli.build_file_source(export_file, make_settings(ingest_max_file_bytes=4096))
    assert source.source_type is SourceType.FILE_EXPORT
    assert isinstance(source, ContentSourceProvider)
    assert json.dumps(source.config_snapshot())  # serialisable, as ingestion requires
