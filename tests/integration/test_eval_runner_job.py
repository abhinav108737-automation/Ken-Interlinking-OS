"""``run_evaluation`` as a real ``JobKind.EVALUATION`` job run against the ``*_test`` database.

What matters here, and only here, is the part that needs PostgreSQL:

* a run bound to a SUBJECT writes exactly ONE ``policy_evaluations`` row, and that row carries
  the runner version, every dataset manifest sha256, the provider, the resolved model per ROLE,
  the seed, the git sha, the per-suite metrics and the gate outcome -- so a recorded
  ``evaluation_ref`` points at something reproducible;
* its ``metrics`` carries ``by_decision_class``, which migration 0009 CHECKs and Phase 9 reads;
* a run with NO subject writes NO row, because
  ``ck_policy_evaluations_exactly_one_subject`` makes one illegal -- and still returns the full
  report and still counts in ``job_runs.counters``, which is what CI gates on;
* the run writes nothing else: **no ``link_plans``, no policy file, no proposal transition, no
  skill status change.** An eval run is read-only results (``evals/README.md`` section 6);
* ``job_runs.counters`` match the report's own counters, and the snapshot is secret-free.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind
from ken_contracts.learning import EvalSuite, GateResult, is_canonical_decision_class
from ken_core.config import Settings
from ken_core.providers.model.mock import MockModelProvider
from ken_services.learning.datasets import available_suites
from ken_services.learning.evaluate import RUNNER_VERSION, run_evaluation_with_report
from ken_services.learning.policy import load_configured_learning_policy
from ken_services.learning.protocol import EVALUATION_COUNTER_NAMES, EvaluationRequest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("migrated")]

SKILL = uuid.UUID("00000000-0000-4000-8000-0000000009e1")
SUITES = (EvalSuite.PROPOSAL_SAFETY, EvalSuite.REFLECTION_GROUNDING)
SM = async_sessionmaker[AsyncSession]


async def _seed_skill(sessionmaker: SM) -> None:
    """A ``draft`` skill: a legal evaluation SUBJECT, and the only Phase 8 consumer of one."""
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO skills (id, name, version, description, instructions,"
                " input_schema, output_schema, suites, model_role, source, status)"
                " VALUES (:id, 'anchor_tightener', 1, 'Tighten anchors.', 'Prefer the"
                " shortest natural anchor.', CAST(:schema AS jsonb), CAST(:schema AS jsonb),"
                " CAST(:suites AS jsonb), 'reasoning', 'human', 'draft')"
            ),
            {
                "id": SKILL,
                "schema": json.dumps({"type": "object", "properties": {}}),
                "suites": json.dumps([suite.value for suite in SUITES]),
            },
        )


async def _one(sessionmaker: SM, sql: str, **params: Any) -> dict[str, Any]:
    async with sessionmaker() as session:
        return dict((await session.execute(text(sql), params)).mappings().one())


async def _rows(sessionmaker: SM, sql: str, **params: Any) -> list[dict[str, Any]]:
    async with sessionmaker() as session:
        return [dict(row) for row in (await session.execute(text(sql), params)).mappings().all()]


async def _count(sessionmaker: SM, table: str) -> int:
    # The table name is a literal from WATCHED below, never caller input.
    async with sessionmaker() as session:
        return int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608


async def _run(sessionmaker: SM, *, skill_id: uuid.UUID | None) -> tuple[Any, Any]:
    settings = Settings(_env_file=None)
    provider = MockModelProvider(settings)
    try:
        return await run_evaluation_with_report(
            sessionmaker=sessionmaker,
            settings=settings,
            policy=load_configured_learning_policy(settings),
            request=EvaluationRequest(suites=SUITES, skill_id=skill_id, seed=99),
            model_provider=provider,
        )
    finally:
        await provider.aclose()


async def test_a_run_with_a_subject_writes_exactly_one_reproducible_row(
    sessionmaker: SM,
) -> None:
    await _seed_skill(sessionmaker)
    outcome, report = await _run(sessionmaker, skill_id=SKILL)
    assert report is not None
    assert outcome.gate_result is report.gate_result

    rows = await _rows(
        sessionmaker,
        "SELECT id, job_run_id, mode, skill_id, proposal_id, suites, dataset_manifests,"
        " metrics, gate_result, gate_reasons, runner_version, model_provider,"
        " model_role_models, seed, git_sha, started_at, finished_at FROM policy_evaluations",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == outcome.evaluation_id
    assert row["job_run_id"] == outcome.job_run_id
    assert row["mode"] == "offline"
    assert row["skill_id"] == SKILL
    assert row["proposal_id"] is None
    assert row["suites"] == [suite.value for suite in SUITES]
    assert row["runner_version"] == RUNNER_VERSION
    assert row["model_provider"] == "mock"
    assert row["seed"] == 99
    assert row["started_at"] is not None and row["finished_at"] is not None

    # Every dataset manifest sha256, keyed by suite and dataset version.
    manifests = row["dataset_manifests"]
    assert set(manifests) == {
        "proposal-safety:core.v1",
        "reflection-grounding:core.v1",
    }
    assert all(len(value) == 64 for value in manifests.values())

    # The resolved model per ROLE -- never a model name in a case.
    assert set(row["model_role_models"]) == {"fast", "reasoning", "embedding"}
    assert row["model_role_models"]["reasoning"] == "mock-reasoning"

    # Phase 9 reads per-class calibration from here; the DB CHECK requires the key.
    metrics = row["metrics"]
    assert "by_decision_class" in metrics
    assert all(is_canonical_decision_class(key) for key in metrics["by_decision_class"])
    assert {entry["suite"] for entry in metrics["by_suite"]} == {s.value for s in SUITES}

    assert row["gate_result"] in {result.value for result in GateResult}
    assert isinstance(row["gate_reasons"], list)


async def test_a_run_without_a_subject_writes_no_row_but_still_reports(
    sessionmaker: SM,
) -> None:
    """``ck_policy_evaluations_exactly_one_subject`` makes a subject-less row illegal, so a
    baseline ``make eval`` run records its numbers on the job run instead. The report is the
    same either way, which is what CI gates on."""
    outcome, report = await _run(sessionmaker, skill_id=None)
    assert report is not None
    assert report.metrics.cases_total > 0
    assert await _count(sessionmaker, "policy_evaluations") == 0
    assert outcome.counters["cases_total"] == report.metrics.cases_total


async def test_the_job_run_records_the_counters_and_a_secret_free_snapshot(
    sessionmaker: SM,
) -> None:
    await _seed_skill(sessionmaker)
    outcome, report = await _run(sessionmaker, skill_id=SKILL)
    assert report is not None
    run = await _one(
        sessionmaker,
        "SELECT kind, status, counters, config_snapshot FROM job_runs WHERE id = :id",
        id=outcome.job_run_id,
    )
    assert run["kind"] == JobKind.EVALUATION.value
    assert run["status"] == RunStatus.SUCCEEDED.value
    assert set(run["counters"]) <= EVALUATION_COUNTER_NAMES
    assert run["counters"] == report.counters()
    snapshot = run["config_snapshot"]
    assert snapshot["runner_version"] == RUNNER_VERSION
    assert snapshot["suites"] == [suite.value for suite in SUITES]
    assert snapshot["provider"] == "mock"
    assert snapshot["skill_id"] == str(SKILL)
    text_snapshot = json.dumps(snapshot)
    for forbidden in ("password", "postgresql://", "ken:ken", "token", "secret"):
        assert forbidden not in text_snapshot


async def test_an_eval_run_activates_nothing(sessionmaker: SM) -> None:
    """ADR-0016 section 17's "read-only results", asserted as row counts rather than promised:
    no plan, no proposal transition, no candidate, and the skill's status is untouched."""
    await _seed_skill(sessionmaker)
    watched = (
        "link_plans",
        "link_plan_transitions",
        "learning_proposals",
        "learning_proposal_transitions",
        "policy_candidates",
        "experiments",
        "experiment_assignments",
    )
    before = {table: await _count(sessionmaker, table) for table in watched}
    await _run(sessionmaker, skill_id=SKILL)
    after = {table: await _count(sessionmaker, table) for table in watched}
    skill = await _one(
        sessionmaker,
        "SELECT status, promoted_by, promoted_at, evaluation_run_id, evaluation_score"
        " FROM skills WHERE id = :id",
        id=SKILL,
    )
    assert after == before
    assert skill["status"] == "draft"
    assert skill["promoted_by"] is None
    assert skill["promoted_at"] is None
    assert skill["evaluation_run_id"] is None
    assert skill["evaluation_score"] is None


async def test_a_baseline_reference_is_read_and_compared(sessionmaker: SM) -> None:
    """The second run names the first as its baseline, so the suite rows carry a baseline pass
    rate and a regression would have something to be measured against."""
    await _seed_skill(sessionmaker)
    first, _report = await _run(sessionmaker, skill_id=SKILL)
    settings = Settings(_env_file=None)
    provider = MockModelProvider(settings)
    try:
        _outcome, report = await run_evaluation_with_report(
            sessionmaker=sessionmaker,
            settings=settings,
            policy=load_configured_learning_policy(settings),
            request=EvaluationRequest(
                suites=SUITES,
                skill_id=SKILL,
                seed=99,
                baseline_evaluation_id=first.evaluation_id,
            ),
            model_provider=provider,
        )
    finally:
        await provider.aclose()
    assert report is not None
    rates = {row.suite: row.baseline_pass_rate for row in report.metrics.by_suite}
    assert rates == dict.fromkeys(SUITES, 1.0)


async def test_all_suites_run_end_to_end_as_a_job(sessionmaker: SM) -> None:
    """The whole gate as CI would run it: every populated suite, mock provider, no subject.
    The result is ``inconclusive`` and never ``pass``, because every decision class on a
    ~65-page synthetic corpus is below the sample floor (ADR-0016 15.4)."""
    settings = Settings(_env_file=None)
    provider = MockModelProvider(settings)
    try:
        outcome, report = await run_evaluation_with_report(
            sessionmaker=sessionmaker,
            settings=settings,
            policy=load_configured_learning_policy(settings),
            request=EvaluationRequest(suites=tuple(available_suites()), seed=0),
            model_provider=provider,
        )
    finally:
        await provider.aclose()
    assert report is not None
    assert report.metrics.cases_total == report.metrics.cases_passed
    assert outcome.gate_result is not GateResult.PASS
    assert outcome.gate_result is GateResult.INCONCLUSIVE
