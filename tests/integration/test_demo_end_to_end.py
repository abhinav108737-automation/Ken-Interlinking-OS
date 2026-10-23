"""``make demo`` end to end, against its own disposable resources (docs/DEMO.md).

The demo is the product's acceptance test in prose: this test runs it with the deterministic
mock model and asserts every acceptance check, with the evidence the demo recorded, then reads
the database back independently of the demo's own printing.

Resources: ``ken_demo_test`` and ``ken_cms_sim_demo_test`` on the project PostgreSQL (:5433,
recreated by the run) and the Qdrant collection ``ken_demo_test_pages`` (:6333). The demo
refuses any other names. Needs Qdrant: the test skips (it does not fake the index) without it.
"""

from __future__ import annotations

import contextlib
import io
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ken_services.demo import resources
from ken_services.demo.config import DemoConfig, DemoRefusalError
from ken_services.demo.console import Console, normalise
from ken_services.demo.runner import run_demo

pytestmark = pytest.mark.integration

ACCEPTANCE = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "P")


@pytest.fixture
async def config(tmp_path: Path) -> AsyncIterator[DemoConfig]:
    built = DemoConfig(
        app_db="ken_demo_test",
        sim_db="ken_cms_sim_demo_test",
        collection="ken_demo_test_pages",
        workdir=tmp_path,
    )
    try:
        yield built
    finally:
        with contextlib.suppress(Exception):  # Qdrant down: the run itself already skipped
            await resources.delete_collections(built)


async def _rows(engine: AsyncEngine, sql: str) -> list[Row[Any]]:
    async with engine.connect() as connection:
        return list((await connection.execute(text(sql))).all())


async def _count(engine: AsyncEngine, sql: str) -> int:
    return int((await _rows(engine, sql))[0][0])


async def test_the_demo_runs_end_to_end_and_every_acceptance_check_passes(
    config: DemoConfig,
) -> None:
    out = Console(stream=io.StringIO())
    code = await run_demo(config, out)
    if code == 2:
        pytest.skip("the demo refused to run: PostgreSQL or Qdrant is not reachable")
    report = "\n".join(out.lines)

    # -- honesty: the opening banner and the closing table
    opening = "\n".join(out.lines[:9])
    assert "ALL DATA IS SYNTHETIC" in opening
    assert "NO real CMS" in opening and "NO Google service" in opening
    assert "DETERMINISTIC MOCK" in opening
    assert "SYNTHETIC CALIBRATION FIXTURE - not evidence about any real site" in report
    assert "SYNTHETIC REVIEW HISTORY" in report
    assert "What this does NOT prove" in report
    for item in ("Real-template HTML parity", "Real CMS behaviour", "live model", "Real traffic"):
        assert item in report, item
    assert "Real review history" in report

    # -- every acceptance check passed, and left evidence behind
    for key in ACCEPTANCE:
        check = out.checks[key]
        assert check.passed is True, (key, check.title, check.evidence)
        assert any(line.startswith("PASS") for line in check.evidence), key
    assert code == 0
    assert out.failed == []

    # -- the output is reproducible text: after normalisation no generated id survives
    assert "-" not in normalise("00000000-0000-4000-8000-000000000000")

    # -- independent read-back of the database the demo left behind
    engine = create_async_engine(config.app_url, poolclass=NullPool)
    try:
        decisions = {
            r[0] for r in await _rows(engine, "SELECT DISTINCT decision FROM autonomy_decisions")
        }
        assert {"block", "auto_publish", "auto_publish_with_extra_verification"} <= decisions
        counterfactuals = await _count(
            engine, "SELECT count(*) FROM autonomy_decisions WHERE would_have_decision IS NOT NULL"
        )
        assert counterfactuals > 0
        # No human approved anything on the autonomous path.
        assert (
            await _count(
                engine,
                "SELECT count(*) FROM link_plan_transitions WHERE to_status IN "
                "('awaiting_approval', 'approved')",
            )
            == 0
        )
        assert await _count(engine, "SELECT count(*) FROM link_plans WHERE status = 'verified'") > 0
        assert (
            await _count(
                engine,
                "SELECT count(*) FROM publish_attempts WHERE rolled_back AND NOT succeeded "
                "AND cooldown_until IS NOT NULL",
            )
            == 1
        )
        # The synthetic calibration is labelled where it lives.
        assert (
            await _count(
                engine,
                "SELECT count(*) FROM policy_evaluations WHERE runner_version = "
                "'demo-synthetic-calibration' AND gate_result = 'pass' AND proposal_id IS NOT NULL",
            )
            == 1
        )
        # Nothing was promoted, anywhere.
        for sql in (
            "SELECT count(*) FROM learning_proposals WHERE status = 'promoted'",
            "SELECT count(*) FROM skills WHERE status = 'promoted'",
            "SELECT count(*) FROM link_measurements",
            "SELECT count(*) FROM link_metrics_daily WHERE NOT synthetic",
        ):
            assert await _count(engine, sql) == 0, sql
        # A service identity with no known password, and no human login unless one was asked for.
        usernames = {r[0] for r in await _rows(engine, "SELECT username FROM users")}
        assert "demo-autonomy-service" in usernames
        if not os.environ.get(resources.DEMO_ADMIN_ENV):
            assert "demo_admin" not in usernames
    finally:
        await engine.dispose()
    assert os.environ.get(resources.DEMO_ADMIN_ENV, "\0") not in report


def test_the_demo_refuses_resources_it_does_not_own() -> None:
    for kwargs in (
        {"app_db": "ken_interlink"},
        {"app_db": "automation_dashboard"},
        {"app_db": "ken_demo_other"},
        {"sim_db": "ken_cms_sim"},
        {"sim_db": "ken_cms_sim_test"},
        {"collection": "ken_pages"},
        {"collection": "other_ken_demo_pages"},
    ):
        with pytest.raises(DemoRefusalError):
            DemoConfig(**kwargs)  # type: ignore[arg-type]
    assert DemoConfig().app_url.endswith("@127.0.0.1:5433/ken_demo")
    assert DemoConfig().sim_url.endswith("@127.0.0.1:5433/ken_cms_sim_demo")
