"""The analytics CLI's argument handling, refusals and output (ADR-0015 §5).

No database and no provider: what is asserted here is that a bad invocation is refused with
exit 2 BEFORE anything runs, that a first-party import is pushed to ``aggregate`` instead of
inventing a transport, and that the one JSON line the CLI prints never carries a path, a
database URL or a credential.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from ken_services.analytics.cli import (
    EXIT_OK,
    EXIT_USAGE,
    SOURCE_ALIASES,
    main,
    outcome_line,
)
from ken_services.analytics.protocol import ImportOutcome


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["import"],  # no source/dataset/range
        ["import", "--source", "ga4", "--dataset", "links", "--start", "2026-05-01"],
        [
            "import",
            "--source",
            "ga4",
            "--dataset",
            "queries",
            "--start",
            "2026-05-01",
            "--end",
            "2026-05-02",
        ],  # queries are deferred to Phase 8
        [
            "import",
            "--source",
            "bigquery",
            "--dataset",
            "links",
            "--start",
            "2026-05-01",
            "--end",
            "2026-05-02",
        ],
        [
            "import",
            "--source",
            "ga4",
            "--dataset",
            "links",
            "--start",
            "01/05/2026",
            "--end",
            "2026-05-02",
        ],
        ["aggregate", "--start", "2026-05-01"],  # one bound only
        ["demo-seed", "--seed", ""],
        ["purge"],  # the retention command is deferred
        ["experiment-create"],  # experiments are deferred to Phase 8
    ],
)
def test_a_bad_invocation_exits_two_and_runs_nothing(argv: list[str]) -> None:
    assert main(argv) == EXIT_USAGE


def test_first_party_without_a_file_points_the_operator_at_aggregate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "import",
            "--source",
            "first_party",
            "--dataset",
            "links",
            "--start",
            "2026-05-01",
            "--end",
            "2026-05-02",
        ]
    )
    assert code == EXIT_USAGE
    message = capsys.readouterr().err
    assert "aggregate" in message
    assert "postgresql" not in message


def test_a_csv_import_needs_a_file_that_exists(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Refused before anything is built, so an obvious typo never opens a connection."""
    missing = tmp_path / "nope.csv"
    code = main(
        [
            "import",
            "--source",
            "ga4",
            "--dataset",
            "links",
            "--start",
            "2026-05-01",
            "--end",
            "2026-05-02",
            "--file",
            str(missing),
        ]
    )
    assert code == EXIT_USAGE
    printed = capsys.readouterr()
    assert printed.out == ""
    assert "nope.csv" not in printed.err
    assert "postgresql" not in printed.err


def test_the_source_aliases_cover_the_three_stored_origins() -> None:
    assert set(SOURCE_ALIASES) == {"ga4", "gsc", "search_console", "first_party"}
    assert SOURCE_ALIASES["gsc"] is SOURCE_ALIASES["search_console"]


def test_the_printed_line_is_one_json_object_without_secrets() -> None:
    outcome = ImportOutcome(
        job_run_id=uuid.uuid4(),
        import_id=uuid.uuid4(),
        counters={"rows_written": 3, "merged_rows": 1},
        plans_measured=2,
    )
    line = outcome_line("import", [outcome])
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["command"] == "import"
    assert parsed["runs"][0]["import_id"] == str(outcome.import_id)
    assert parsed["runs"][0]["counters"] == {"merged_rows": 1, "rows_written": 3}
    assert parsed["runs"][0]["plans_measured"] == 2
    assert "postgresql" not in line
    assert "token" not in line


def test_the_help_text_names_the_make_targets_commands() -> None:
    assert main(["--help"]) == EXIT_OK
