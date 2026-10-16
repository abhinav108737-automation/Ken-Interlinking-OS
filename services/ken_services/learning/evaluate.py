"""The offline eval runner and the gate (ADR-0016 sections 7 and 15; ``make eval``).

``run_evaluation`` implements the frozen :class:`~ken_services.learning.protocol.RunEvaluation`
and runs as ``run_job(kind=JobKind.EVALUATION)``. ``evaluate_gate`` implements the frozen
:class:`~ken_services.learning.protocol.GateEvaluator`.

**A run writes a report and activates nothing.** ``evals/README.md`` section 6 calls this
"read-only results", and it is literal here: this module writes one ``policy_evaluations`` row
and a JSON report, never a policy file, never a ``link_plans`` row, never a proposal
transition, never a skill status. The static boundary test in
``tests/unit/learning/test_boundaries.py`` is what keeps that true.

**Determinism is the requirement, not a nicety.** The default provider is ``mock``, which
replays recorded responses and computes embeddings by feature hashing, so the same datasets and
seed produce a BYTE-IDENTICAL report; a test runs the whole thing twice and diffs. That is what
lets CI gate on the suites. ``--provider ollama`` is opt-in behind ``KEN_RUN_LIVE_OLLAMA=1``
and **can never gate**: its gate result is forced to ``inconclusive``, because a gate that
depends on whichever model happens to be installed is not a gate.

**What the gate requires** (from ``policies/learning/learning.v1.yaml``):

1. every hard-fail suite CLEAN -- and the runner unions the policy's list with
   :data:`~ken_contracts.learning.HARD_FAIL_SUITES` from the contract, so a hard-fail suite
   cannot be waived even by a policy document loaded from somewhere unexpected;
2. every declared suite at or above its ``min_pass_rate``;
3. no drop beyond ``max_regression_vs_baseline`` against a recorded baseline run;
4. zero ``must_not`` violations anywhere;
5. no behavioural BLOCK (through :class:`~ken_services.learning.protocol.BlockOnly`, which has
   no method that can pass a gate).

Below ``min_sample_count`` in any decision class -- which, on a corpus of ~65 synthetic pages,
is every class -- the result is :attr:`~ken_contracts.learning.GateResult.INCONCLUSIVE`,
**never** ``pass``. A declared suite that was not run is also ``inconclusive``: absence of
evidence is not evidence of safety, and ``inconclusive`` is not promotable. A real failure
outranks missing data, so any fail reason gives ``fail``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind
from ken_contracts.learning import (
    DEFERRED_SUITES,
    HARD_FAIL_SUITES,
    EvalCase,
    EvalSuite,
    EvaluationMetrics,
    EvaluationMode,
    GateReason,
    GateReasonCode,
    GateResult,
    LearningPolicy,
    SuiteMetrics,
)
from ken_contracts.model import ModelProvider
from ken_core.config import REPO_ROOT, Settings, git_sha
from ken_services.agents.policy import load_configured_agent_policy
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.jobs.runner import JobContext, run_job
from ken_services.learning.datasets import (
    DatasetError,
    SuiteDataset,
    available_suites,
    load_suite_datasets,
    manifest_shas,
    sha256_file,
)
from ken_services.learning.metrics import build_metrics, critic_scores
from ken_services.learning.policy import load_configured_learning_policy
from ken_services.learning.protocol import (
    BEHAVIOURAL_REASON_CODES,
    EVALUATION_COUNTER_NAMES,
    BlockOnly,
    EvaluationOutcome,
    EvaluationRequest,
)
from ken_services.learning.scorers import CaseOutcome, ScoringContext, score_case
from ken_services.recommendations.policy import (
    load_recommendation_policy,
    resolve_ranking_policy,
)

__all__ = [
    "LIVE_PROVIDER_ENV",
    "RUNNER_VERSION",
    "BehaviouralBlockOnly",
    "EvaluationReport",
    "eval_main",
    "evaluate_gate",
    "report_json",
    "run_evaluation",
    "run_evaluation_with_report",
    "run_suites",
]

RUNNER_VERSION: Final = "eval-runner/1"
"""Bumped when a scorer, a metric or the gate changes shape. Recorded on every row, so an old
``evaluation_ref`` says which runner produced it."""
LIVE_PROVIDER_ENV: Final = "KEN_RUN_LIVE_OLLAMA"
DEFAULT_RESULTS_DIR: Final = REPO_ROOT / "evals" / "results"
_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)
_POLICY_FILE_KEYS: Final = ("taxonomy", "recommendation", "ranking", "agents", "learning")


# --------------------------------------------------------------------------- #
# Behavioural signals: block-only, by construction
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BehaviouralBlockOnly(BlockOnly):
    """The only route from behavioural data into the gate (ADR-0016 15.7.12 / 16.7).

    It can return exactly two reason codes, both of which BLOCK, and it has no method that
    returns a :class:`GateResult` -- so a spoofable first-party count can never graduate
    anything. Search Console data does not appear here at all: it is diagnostic only.
    """

    negative_outcome_rate: float = 0.5
    """At or below this, a class's measured outcome counts as negative and blocks."""

    def blocking_reasons(self) -> tuple[GateReason, ...]:
        reasons: list[GateReason] = []
        for observation in sorted(self.observations, key=lambda row: row.decision_class):
            rate = observation.positive_outcome_rate
            if rate is not None and rate <= self.negative_outcome_rate:
                reasons.append(
                    GateReason(
                        code=GateReasonCode.BEHAVIOURAL_SIGNAL_NEGATIVE,
                        decision_class=observation.decision_class,
                        detail=(
                            "measured outcomes are not positive in this class "
                            f"({observation.positive_outcomes}/{observation.sample_count})"
                        ),
                    )
                )
            elif not observation.sufficient:
                reasons.append(
                    GateReason(
                        code=GateReasonCode.SAMPLE_TOO_SMALL,
                        decision_class=observation.decision_class,
                        detail=f"{observation.sample_count} observations in this class",
                    )
                )
        # Belt and braces: this type structurally cannot pass a gate, and it also cannot
        # emit a code outside the two that BLOCK. A stray code is dropped, never returned.
        return tuple(reason for reason in reasons if reason.code in BEHAVIOURAL_REASON_CODES)


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def evaluate_gate(
    *,
    metrics: EvaluationMetrics,
    policy: LearningPolicy,
    baseline: EvaluationMetrics | None = None,
    behavioural: BlockOnly | None = None,
) -> tuple[GateResult, tuple[GateReason, ...]]:
    """``metrics`` + ``policy`` -> a verdict and the reasons (module docstring).

    Implements the frozen :class:`~ken_services.learning.protocol.GateEvaluator`.
    """
    by_suite: dict[EvalSuite, SuiteMetrics] = {row.suite: row for row in metrics.by_suite}
    baseline_by_suite: dict[EvalSuite, SuiteMetrics] = (
        {} if baseline is None else {row.suite: row for row in baseline.by_suite}
    )
    hard_fail = frozenset(policy.gates.hard_fail_suites) | HARD_FAIL_SUITES
    fail: list[GateReason] = []
    unresolved: list[GateReason] = []

    for suite in sorted(by_suite, key=lambda item: item.value):
        row = by_suite[suite]
        if row.must_not_violations:
            fail.append(
                GateReason(
                    code=GateReasonCode.MUST_NOT_VIOLATION,
                    suite=suite,
                    detail=f"{row.must_not_violations} case(s) produced a forbidden decision",
                )
            )
        if suite in hard_fail and row.failed:
            fail.append(
                GateReason(
                    code=GateReasonCode.HARD_FAIL_SUITE,
                    suite=suite,
                    detail=f"{row.failed} failing case(s); this suite cannot be waived",
                )
            )

    for suite in sorted(policy.gates.suites, key=lambda item: item.value):
        gate = policy.gates.suites[suite]
        declared = by_suite.get(suite)
        if declared is None or declared.cases == 0 or declared.pass_rate is None:
            if suite in DEFERRED_SUITES:
                continue
            unresolved.append(
                GateReason(
                    code=GateReasonCode.SUITE_NOT_RUN,
                    suite=suite,
                    detail="declared by the gate policy and produced no cases in this run",
                )
            )
            continue
        if declared.pass_rate < gate.min_pass_rate:
            fail.append(
                GateReason(
                    code=GateReasonCode.BELOW_MIN_PASS_RATE,
                    suite=suite,
                    detail=f"pass rate {declared.pass_rate} below the floor {gate.min_pass_rate}",
                )
            )
        previous = baseline_by_suite.get(suite)
        if previous is not None and previous.pass_rate is not None:
            drop = previous.pass_rate - declared.pass_rate
            if drop > gate.max_regression_vs_baseline:
                fail.append(
                    GateReason(
                        code=GateReasonCode.REGRESSION_VS_BASELINE,
                        suite=suite,
                        detail=(
                            f"pass rate fell {round(drop, 6)} against the baseline, "
                            f"tolerance {gate.max_regression_vs_baseline}"
                        ),
                    )
                )
        elif baseline is not None:
            unresolved.append(
                GateReason(
                    code=GateReasonCode.BASELINE_MISSING,
                    suite=suite,
                    detail="the baseline run did not measure this suite",
                )
            )

    for key, row_metrics in sorted(metrics.by_decision_class.items()):
        if not row_metrics.sufficient:
            unresolved.append(
                GateReason(
                    code=GateReasonCode.SAMPLE_TOO_SMALL,
                    decision_class=key,
                    detail=(
                        f"{row_metrics.sample_count} sample(s), floor "
                        f"{policy.gates.min_sample_count}"
                    ),
                )
            )

    if behavioural is not None:
        fail.extend(behavioural.blocking_reasons())

    reasons = tuple(fail) + tuple(unresolved)
    if fail:
        return GateResult.FAIL, reasons
    if unresolved:
        return GateResult.INCONCLUSIVE, reasons
    return GateResult.PASS, reasons


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Everything a run measured. **This IS the report** ADR-0016 section 17 calls read-only.

    Its canonical JSON (:func:`report_json`) holds no wall clock, no run id and no row id, so
    the same datasets and seed give byte-identical bytes -- which is what the determinism test
    diffs and what makes a recorded ``evaluation_ref`` meaningful.
    """

    runner_version: str
    suites: tuple[EvalSuite, ...]
    dataset_manifests: Mapping[str, str]
    policy_shas: Mapping[str, str]
    policy_versions: Mapping[str, str]
    provider: str
    model_role_models: Mapping[str, str]
    seed: int
    git_sha: str | None
    mode: EvaluationMode
    metrics: EvaluationMetrics
    gate_result: GateResult
    gate_reasons: tuple[GateReason, ...]
    outcomes: tuple[CaseOutcome, ...]
    critic_precision: float | None = None
    critic_recall: float | None = None
    live_provider: bool = False

    def counters(self) -> dict[str, int]:
        """``job_runs.counters`` of this run (:data:`EVALUATION_COUNTER_NAMES`)."""
        return {
            "suites_run": len(self.suites),
            "cases_total": self.metrics.cases_total,
            "cases_passed": self.metrics.cases_passed,
            "cases_failed": self.metrics.cases_total - self.metrics.cases_passed,
            "must_not_violations": sum(row.must_not_violations for row in self.metrics.by_suite),
            "classes_below_sample_floor": sum(
                1 for row in self.metrics.by_decision_class.values() if not row.sufficient
            ),
            "model_calls": sum(
                1 for outcome in self.outcomes if outcome.critic_observation is not None
            ),
            "fallbacks": sum(
                1 for outcome in self.outcomes if outcome.decision.endswith("+fallback")
            ),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "runner_version": self.runner_version,
            "mode": self.mode.value,
            "provider": self.provider,
            "live_provider": self.live_provider,
            "model_role_models": dict(sorted(self.model_role_models.items())),
            "seed": self.seed,
            "git_sha": self.git_sha,
            "suites": [suite.value for suite in self.suites],
            "dataset_manifests": dict(sorted(self.dataset_manifests.items())),
            "policy_shas": dict(sorted(self.policy_shas.items())),
            "policy_versions": dict(sorted(self.policy_versions.items())),
            "gate_result": self.gate_result.value,
            "gate_reasons": [reason.model_dump(mode="json") for reason in self.gate_reasons],
            "critic_precision": self.critic_precision,
            "critic_recall": self.critic_recall,
            "metrics": self.metrics.model_dump(mode="json"),
            "cases": [
                {
                    "case_id": outcome.case_id,
                    "suite": outcome.suite.value,
                    "decision": outcome.decision,
                    "passed": outcome.passed,
                    "must_not_hit": outcome.must_not_hit,
                    "decision_class": outcome.decision_class,
                    "confidence": outcome.confidence,
                    "detail": outcome.detail,
                }
                for outcome in self.outcomes
            ],
        }


def report_json(report: EvaluationReport) -> str:
    """The report's canonical JSON: sorted keys, no spaces that vary, a trailing newline."""
    return json.dumps(report.as_dict(), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# Running the suites (no database, so it is testable on its own)
# --------------------------------------------------------------------------- #
def _policy_files(settings: Settings) -> tuple[dict[str, str], dict[str, str], Any, Any, Any]:
    """Per-policy sha256 and version, plus the loaded taxonomy/recommendation/ranking.

    The shas are recorded on the run: a passing evaluation is only meaningful against the
    exact policy documents it ran under, and promotion re-checks them.
    """
    taxonomy_path = Path(settings.taxonomy_path)
    recommendation_path = Path(settings.recommendation_policy_path)
    taxonomy = load_taxonomy(taxonomy_path)
    recommendation = load_recommendation_policy(recommendation_path)
    ranking = resolve_ranking_policy(recommendation, root=REPO_ROOT)
    ranking_path = (REPO_ROOT / recommendation.ranking_policy).resolve()
    agents_path = Path(settings.agents_policy_path)
    learning_path = Path(settings.learning_policy_path)
    agents = load_configured_agent_policy(settings)
    learning = load_configured_learning_policy(settings)
    paths = {
        "taxonomy": taxonomy_path,
        "recommendation": recommendation_path,
        "ranking": ranking_path,
        "agents": agents_path,
        "learning": learning_path,
    }
    versions = {
        "taxonomy": str(taxonomy.version),
        "recommendation": str(recommendation.version),
        "ranking": str(ranking.version),
        "agents": str(agents.version),
        "learning": str(learning.version),
    }
    shas = {key: sha256_file(paths[key]) for key in _POLICY_FILE_KEYS}
    return shas, versions, taxonomy, recommendation, ranking


async def _resolved_models(provider: ModelProvider) -> dict[str, str]:
    """The model actually serving each ROLE, from the provider itself.

    By role and never by name in a dataset: a recorded case must not pin a model, or changing
    ``MODEL_REASONING`` would silently invalidate every stored evaluation.
    """
    try:
        capabilities = await provider.capabilities()
    except Exception:
        # A provider that cannot describe itself must not fail the run: the report then
        # records no model per role, which is honest rather than invented.
        return {}
    # ModelRoleStatus.model is optional (a role the provider cannot serve has none); such a
    # role is simply absent from the record rather than recorded as null.
    return {row.role.value: row.model for row in capabilities.roles if row.model is not None}


async def run_suites(
    *,
    settings: Settings,
    policy: LearningPolicy,
    request: EvaluationRequest,
    model_provider: ModelProvider,
    dataset_root: Path | None = None,
    baseline: EvaluationMetrics | None = None,
    behavioural: BlockOnly | None = None,
) -> EvaluationReport:
    """Score every case of every requested suite and build the report. No database.

    Separated from :func:`run_evaluation` so the datasets, the scorers, the metrics and the
    gate are all testable without PostgreSQL -- and so ``make eval`` fails on a bad dataset
    before it writes a job run.
    """
    suites = tuple(dict.fromkeys(request.suites))
    if not suites:
        raise DatasetError("an evaluation must name at least one suite")
    deferred = [suite.value for suite in suites if suite in DEFERRED_SUITES]
    if deferred:
        raise DatasetError(
            "deferred suite(s) " + ", ".join(deferred) + ": no dataset exists (ADR-0016 15.3)"
        )
    datasets = load_suite_datasets(suites, root=dataset_root)
    shas, versions, taxonomy, recommendation, ranking = _policy_files(settings)
    ctx = ScoringContext(
        settings=settings,
        taxonomy=taxonomy,
        recommendation=recommendation,
        ranking=ranking,
        agents=load_configured_agent_policy(settings),
        provider=model_provider,
        provider_name=request.provider_name,
        seed=request.seed if request.seed is not None else 0,
    )
    outcomes: list[CaseOutcome] = []
    for suite in suites:
        for case in _ordered(datasets[suite]):
            outcomes.append(await score_case(case, ctx))
    metrics = build_metrics(
        outcomes,
        suites=suites,
        min_sample_count=policy.gates.min_sample_count,
        baseline=_baseline_rates(baseline),
    )
    live = request.provider_name != "mock"
    gate, reasons = evaluate_gate(
        metrics=metrics, policy=policy, baseline=baseline, behavioural=behavioural
    )
    if live:
        gate = GateResult.INCONCLUSIVE
        reasons = (
            GateReason(
                code=GateReasonCode.SAMPLE_TOO_SMALL,
                detail=f"provider {request.provider_name!r} is a live model and never gates",
            ),
            *reasons,
        )
    scores = critic_scores(outcomes)
    return EvaluationReport(
        runner_version=RUNNER_VERSION,
        suites=suites,
        dataset_manifests=manifest_shas(datasets),
        policy_shas=shas,
        policy_versions=versions,
        provider=request.provider_name,
        model_role_models=await _resolved_models(model_provider),
        seed=ctx.seed,
        git_sha=git_sha(),
        mode=request.mode,
        metrics=metrics,
        gate_result=gate,
        gate_reasons=reasons,
        outcomes=tuple(outcomes),
        critic_precision=scores.precision,
        critic_recall=scores.recall,
        live_provider=live,
    )


def _ordered(dataset: SuiteDataset) -> tuple[EvalCase, ...]:
    """Cases in ``case_id`` order: file order must not change a report."""
    return tuple(sorted(dataset.cases, key=lambda case: case.case_id))


def _baseline_rates(baseline: EvaluationMetrics | None) -> dict[EvalSuite, float] | None:
    if baseline is None:
        return None
    rates: dict[EvalSuite, float] = {}
    for row in baseline.by_suite:
        if row.pass_rate is not None:
            rates[row.suite] = row.pass_rate
    return rates


# --------------------------------------------------------------------------- #
# The job: one policy_evaluations row per run with a subject
# --------------------------------------------------------------------------- #
async def _load_baseline(
    session: AsyncSession, evaluation_id: uuid.UUID
) -> EvaluationMetrics | None:
    row = (
        await session.execute(
            sql_text("SELECT metrics FROM policy_evaluations WHERE id = :id"),
            {"id": evaluation_id},
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    payload = row if isinstance(row, dict) else json.loads(row)
    return EvaluationMetrics.model_validate(payload)


async def _insert_evaluation(
    session: AsyncSession,
    *,
    evaluation_id: uuid.UUID,
    job_run_id: uuid.UUID,
    request: EvaluationRequest,
    report: EvaluationReport,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    """Insert the one append-only ``policy_evaluations`` row that IS this run's report."""
    await session.execute(
        sql_text(
            "INSERT INTO policy_evaluations ("
            " id, job_run_id, mode, proposal_id, skill_id, suites, dataset_manifests,"
            " baseline_evaluation_id, metrics, gate_result, gate_reasons, runner_version,"
            " model_provider, model_role_models, seed, git_sha, started_at, finished_at"
            ") VALUES ("
            " :id, :job_run_id, :mode, :proposal_id, :skill_id, CAST(:suites AS jsonb),"
            " CAST(:dataset_manifests AS jsonb), :baseline_evaluation_id,"
            " CAST(:metrics AS jsonb), :gate_result, CAST(:gate_reasons AS jsonb),"
            " :runner_version, :model_provider, CAST(:model_role_models AS jsonb), :seed,"
            " :git_sha, :started_at, :finished_at)"
        ),
        {
            "id": evaluation_id,
            "job_run_id": job_run_id,
            "mode": request.mode.value,
            "proposal_id": request.proposal_id,
            "skill_id": request.skill_id,
            "suites": json.dumps([suite.value for suite in report.suites]),
            "dataset_manifests": json.dumps(dict(report.dataset_manifests), sort_keys=True),
            "baseline_evaluation_id": request.baseline_evaluation_id,
            "metrics": json.dumps(report.metrics.model_dump(mode="json"), sort_keys=True),
            "gate_result": report.gate_result.value,
            "gate_reasons": json.dumps(
                [reason.model_dump(mode="json") for reason in report.gate_reasons]
            ),
            "runner_version": report.runner_version,
            "model_provider": report.provider,
            "model_role_models": json.dumps(dict(report.model_role_models), sort_keys=True),
            "seed": report.seed,
            "git_sha": report.git_sha,
            "started_at": started_at,
            "finished_at": finished_at,
        },
    )


async def run_evaluation(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    policy: LearningPolicy,
    request: EvaluationRequest,
    model_provider: ModelProvider | None = None,
    now: datetime | None = None,
) -> EvaluationOutcome:
    """Run the requested suites as one ``evaluation`` job run and record the report.

    Implements the frozen :class:`~ken_services.learning.protocol.RunEvaluation`.

    **One row per run WITH A SUBJECT.** Migration 0009's
    ``ck_policy_evaluations_exactly_one_subject`` requires exactly one of ``proposal_id`` /
    ``skill_id``, so a baseline run (``make eval SUITES=...`` with neither) has no row it could
    legally write. Such a run still produces the full report: it is returned, written to
    ``evals/results/`` by the CLI, and its numbers go into ``job_runs.counters``, which is what
    CI gates on. A run bound to a proposal or a skill -- the only kind a promotion may ever
    cite as its ``evaluation_ref`` -- writes the row.

    Raises:
        DatasetError: a dataset is missing, unhashed or inconsistent with its manifest. Raised
            BEFORE the job run starts, so a bad dataset never leaves a half-finished run.
        JobAlreadyRunningError: another evaluation run holds the per-kind advisory lock.
    """
    outcome, _report = await run_evaluation_with_report(
        sessionmaker=sessionmaker,
        settings=settings,
        policy=policy,
        request=request,
        model_provider=model_provider,
        now=now,
    )
    return outcome


async def run_evaluation_with_report(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    policy: LearningPolicy,
    request: EvaluationRequest,
    model_provider: ModelProvider | None = None,
    now: datetime | None = None,
) -> tuple[EvaluationOutcome, EvaluationReport | None]:
    """:func:`run_evaluation` plus the report it produced, for the CLI and the tests.

    The frozen protocol returns only an :class:`EvaluationOutcome`, so the report comes back
    here instead of being recomputed: scoring the suites twice would double every model call
    and, with a live provider, could give two different answers.
    """
    provider = model_provider
    owned = False
    if provider is None:
        from ken_core.providers.model.factory import build_model_provider

        provider = build_model_provider(settings)
        owned = True
    started = now or _EPOCH
    evaluation_id = uuid.uuid4()
    report_holder: dict[str, EvaluationReport] = {}

    async def body(job: JobContext) -> RunStatus:
        baseline: EvaluationMetrics | None = None
        if request.baseline_evaluation_id is not None:
            async with job.transaction() as tx:
                baseline = await _load_baseline(tx.session, request.baseline_evaluation_id)
        report = await run_suites(
            settings=settings,
            policy=policy,
            request=request,
            model_provider=provider,
            baseline=baseline,
        )
        report_holder["report"] = report
        async with job.transaction() as tx:
            if request.proposal_id is not None or request.skill_id is not None:
                await _insert_evaluation(
                    tx.session,
                    evaluation_id=evaluation_id,
                    job_run_id=job.run_id,
                    request=request,
                    report=report,
                    started_at=started,
                    finished_at=started,
                )
            for name, value in report.counters().items():
                tx.increment(name, value)
        return RunStatus.SUCCEEDED

    try:
        detail = await run_job(
            kind=JobKind.EVALUATION,
            sessionmaker=sessionmaker,
            triggered_by=request.triggered_by,
            body=body,
            config_snapshot=_snapshot(request, settings),
            counter_names=sorted(EVALUATION_COUNTER_NAMES),
        )
    finally:
        if owned:
            await provider.aclose()
    report = report_holder.get("report")
    outcome = EvaluationOutcome(
        job_run_id=detail.id,
        evaluation_id=evaluation_id,
        gate_result=report.gate_result if report else GateResult.FAIL,
        counters=dict(detail.counters),
    )
    return outcome, report


def _snapshot(request: EvaluationRequest, settings: Settings) -> dict[str, Any]:
    """``job_runs.config_snapshot``: secret-free, and enough to reproduce the run."""
    return {
        "runner_version": RUNNER_VERSION,
        "suites": [suite.value for suite in request.suites],
        "mode": request.mode.value,
        "provider": request.provider_name,
        "seed": request.seed,
        "proposal_id": str(request.proposal_id) if request.proposal_id else None,
        "skill_id": str(request.skill_id) if request.skill_id else None,
        "baseline_evaluation_id": (
            str(request.baseline_evaluation_id) if request.baseline_evaluation_id else None
        ),
        "learning_policy_path": _repo_relative(Path(settings.learning_policy_path)),
        "git_sha": git_sha(),
    }


def _repo_relative(path: Path) -> str:
    resolved = path.resolve()
    root = REPO_ROOT.resolve()
    return str(resolved.relative_to(root)) if resolved.is_relative_to(root) else resolved.name


# --------------------------------------------------------------------------- #
# CLI (`make eval`, and `evals/runner.py`)
# --------------------------------------------------------------------------- #
def _parse_suites(value: str) -> tuple[EvalSuite, ...]:
    if value.strip() in ("all", "*"):
        return available_suites()
    out: list[EvalSuite] = []
    for item in value.split(","):
        name = item.strip()
        if not name:
            continue
        try:
            out.append(EvalSuite(name))
        except ValueError as exc:
            known = ", ".join(suite.value for suite in EvalSuite)
            raise SystemExit(f"unknown suite {name!r}; known suites: {known}") from exc
    if not out:
        raise SystemExit("--suites needs at least one suite name")
    return tuple(out)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make eval",
        description="Offline evaluation over the versioned eval datasets. Writes a report and "
        "activates nothing.",
    )
    parser.add_argument("--suites", required=True, help="Comma-separated suite names, or 'all'.")
    parser.add_argument("--proposal", default=None, help="Proposal UUID this run evaluates.")
    parser.add_argument("--skill", default=None, help="Skill UUID this run evaluates.")
    parser.add_argument("--baseline", default=None, help="policy_evaluations UUID to compare with.")
    parser.add_argument("--seed", type=int, default=0, help="Recorded on the run.")
    parser.add_argument(
        "--provider",
        choices=("mock", "ollama"),
        default="mock",
        help="'mock' replays recorded responses and gates; 'ollama' is opt-in and never gates.",
    )
    parser.add_argument(
        "--report",
        default=None,
        help=f"Write the canonical JSON report here (default: {DEFAULT_RESULTS_DIR}/<name>.json).",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Score the suites and print the report without recording a job run.",
    )
    return parser


async def _run_cli(args: argparse.Namespace) -> int:
    settings = Settings()
    policy = load_configured_learning_policy(settings)
    suites = _parse_suites(args.suites)
    if args.provider == "ollama" and os.environ.get(LIVE_PROVIDER_ENV) != "1":
        print(
            f"--provider ollama needs {LIVE_PROVIDER_ENV}=1 (a live model never gates).",
            file=sys.stderr,
        )
        return 2
    request = EvaluationRequest(
        suites=suites,
        mode=EvaluationMode.OFFLINE,
        proposal_id=uuid.UUID(args.proposal) if args.proposal else None,
        skill_id=uuid.UUID(args.skill) if args.skill else None,
        baseline_evaluation_id=uuid.UUID(args.baseline) if args.baseline else None,
        seed=args.seed,
        provider_name=args.provider,
    )
    if request.proposal_id is not None and request.skill_id is not None:
        print("name a proposal OR a skill, never both.", file=sys.stderr)
        return 2
    from ken_core.providers.model.factory import build_model_provider

    provider_settings = settings.model_copy(update={"model_provider": args.provider})
    provider = build_model_provider(provider_settings)
    try:
        if args.no_db:
            report: EvaluationReport | None = await run_suites(
                settings=settings, policy=policy, request=request, model_provider=provider
            )
        else:
            from ken_core.db.engine import create_engine
            from ken_core.db.session import create_sessionmaker

            engine = create_engine(settings)
            sessionmaker = create_sessionmaker(engine)
            try:
                _outcome, report = await run_evaluation_with_report(
                    sessionmaker=sessionmaker,
                    settings=settings,
                    policy=policy,
                    request=request,
                    model_provider=provider,
                    now=datetime.now(UTC),
                )
            finally:
                await engine.dispose()
    finally:
        await provider.aclose()
    if report is None:  # pragma: no cover - run_job always reaches the body or raises
        print("the evaluation produced no report", file=sys.stderr)
        return 2
    destination = _write_report(report, args.report)
    _print_summary(report, destination)
    return 0 if report.gate_result is not GateResult.FAIL else 1


def _write_report(report: EvaluationReport, destination: str | None) -> Path:
    if destination:
        path = Path(destination)
    else:
        name = hashlib.sha256(
            ",".join(suite.value for suite in report.suites).encode("utf-8")
        ).hexdigest()[:12]
        DEFAULT_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path = DEFAULT_RESULTS_DIR / f"{report.provider}-{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report_json(report), encoding="utf-8")
    return path


def _print_summary(report: EvaluationReport, destination: Path) -> None:
    print(f"runner {report.runner_version}  provider {report.provider}  seed {report.seed}")
    for row in report.metrics.by_suite:
        marker = "HARD-FAIL" if row.suite in HARD_FAIL_SUITES else "         "
        print(
            f"  {marker} {row.suite.value:<24} {row.passed}/{row.cases} "
            f"pass_rate={row.pass_rate} must_not={row.must_not_violations}"
        )
    print(f"gate: {report.gate_result.value}")
    for reason in report.gate_reasons:
        where = reason.suite.value if reason.suite else (reason.decision_class or "-")
        print(f"  - {reason.code.value} [{where}] {reason.detail}")
    print(f"report: {destination}")


def eval_main(argv: Sequence[str] | None = None) -> int:
    """``make eval`` / ``evals/runner.py``. Exit 1 only on a gate FAIL, 2 on bad usage."""
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run_cli(args))
    except DatasetError as exc:
        print(f"dataset error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(eval_main())
