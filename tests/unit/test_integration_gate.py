"""The quality gate must fail, not silently skip, when the test database is down."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UNREACHABLE = "postgresql+asyncpg://ken:ken@127.0.0.1:1/ken_interlink_test"


def _run_integration(*, required: bool) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "KEN_TEST_DATABASE_URL": UNREACHABLE}
    env.pop("KEN_REQUIRE_INTEGRATION_DB", None)
    if required:
        env["KEN_REQUIRE_INTEGRATION_DB"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "integration", "-q", "-p", "no:cacheprovider",
         "tests/integration"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip


def test_gate_fails_when_integration_db_is_required_but_unreachable() -> None:
    result = _run_integration(required=True)
    assert result.returncode != 0
    assert "KEN_REQUIRE_INTEGRATION_DB" in result.stdout + result.stderr


def test_developer_runs_still_auto_skip_without_the_switch() -> None:
    result = _run_integration(required=False)
    assert result.returncode == 0
    assert "skipped" in result.stdout
