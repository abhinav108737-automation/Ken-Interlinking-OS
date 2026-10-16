"""``make analytics-sim-serve`` (``python -m ken_services.analytics.collector.serve``).

Only the paths that touch no database: the printed line, and the configuration refusals.
Starting the two servers needs the simulated CMS database, which these tests never touch.
"""

from __future__ import annotations

import json

import pytest

from ken_services.analytics.collector.serve import EXIT_OK, EXIT_USAGE, _serve, main, serve_line
from tests.conftest import make_settings


def test_the_printed_line_names_both_verified_urls() -> None:
    line = serve_line(
        site_url="http://127.0.0.1:5001",
        collector_url="http://127.0.0.1:5002",
        pages=7,
        live_plans=3,
        synthetic=True,
    )
    assert json.loads(line) == {
        "beacon_url": "http://127.0.0.1:5002/k.js",
        "collector_url": "http://127.0.0.1:5002",
        "events_url": "http://127.0.0.1:5002/e",
        "live_plans": 3,
        "pages": 7,
        "site_url": "http://127.0.0.1:5001",
        "synthetic": True,
    }
    assert line == json.dumps(json.loads(line), sort_keys=True)  # stable output


async def test_an_unsafe_simulated_cms_database_is_refused_before_any_io(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings(
        cms_sim_database_url="postgresql+asyncpg://ken:ken@localhost:5433/ken_other"
    )
    assert await _serve(settings, 1.0) == EXIT_USAGE
    error = capsys.readouterr().err
    assert "ken_cms_sim" in error
    assert "ken:ken" not in error  # never echoes a database URL or credentials


async def test_a_missing_analytics_policy_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings(analytics_policy_path="/nonexistent/analytics.v1.yaml")
    assert await _serve(settings, 1.0) == EXIT_USAGE
    assert capsys.readouterr().err.startswith("error:")


def test_help_and_usage_exit_codes() -> None:
    assert main(["--help"]) == EXIT_OK
    assert main(["--unknown-flag"]) == EXIT_USAGE
