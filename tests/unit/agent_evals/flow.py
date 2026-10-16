"""Database flow for the Phase 4 agent evals (``tests/integration/test_agent_evals.py`` and
``evals/agents/benchmark.py``).

It runs ONLY against a database whose name ends in ``_test`` (it truncates tables). The flow
is the production one end to end::

    research_site.jsonl (+ optional group edits) --run_ingestion--> --run_analysis-->
        --run_recommendations--> proposed plans (the ONLY plan producer)
        --run_agent_batch (real LangGraph graph, DatabaseAgentBackend / DatabaseAgentRunStore,
          a real ``agent`` job run)--> agent_runs / agent_steps / agent_run_plans

Model outputs are RECORDED: :func:`recorded_provider` replays a case's raw answers through
``MockModelProvider`` (registered values for answers that validate, scripted schema
violations for answers that do not, which is what ``complete_structured`` raises for them).
Mid-run hooks wrap the real backend (:class:`HookedBackend`); nothing in ``ken_agents`` or
``ken_services`` is patched.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.agents import (
    CRITIC_ACTOR,
    POLICY_GATE_ACTOR,
    AgentPolicy,
    CriticAssessment,
    PlannerSelection,
)
from ken_contracts.inventory import RunStatus
from ken_contracts.jobs import JobKind, JobRunDetail
from ken_contracts.model import (
    GenerationOptions,
    Message,
    ModelRole,
    ModelUnavailableError,
    StructuredResult,
)
from ken_core.providers.model.mock import MockCall, MockModelProvider
from ken_services.agents.protocol import (
    AgentBackend,
    HandOffResult,
    PageSnapshot,
    PlanCheck,
    PlanReviewInput,
)
from tests.unit.agent_evals.cases import (
    Action,
    Case,
    Hook,
    ModelScript,
    Policy,
    recorded_text,
    schema_accepts,
    url,
)
from tests.unit.recommendation_evals import db_flow
from tests.unit.recommendation_evals.judgments import CORPUS_PATH

SM = async_sessionmaker[AsyncSession]

TRUNCATE_ALL: Final = (
    "TRUNCATE agent_run_plans, agent_steps, agent_runs, link_plan_transitions, "
    "link_candidates, opportunities, link_plans, page_relationships, page_protections, "
    "page_embeddings, page_analyses, job_runs, existing_links, known_urls, page_versions, "
    "pages, ingestion_issues, ingestion_runs, events RESTART IDENTITY CASCADE"
)
"""TRUNCATE (not DELETE): the append-only / immutability triggers are row triggers."""
AFTER_HAND_OFF: Final = frozenset({"proposed", "policy_checked", "expired"})
"""The only plan statuses an agent eval may leave behind (expired only through a hook)."""
GATE_TRANSITIONS: Final = {
    "critiqued": ("proposed", CRITIC_ACTOR),
    "policy_checked": ("critiqued", POLICY_GATE_ACTOR),
}
"""to_status -> (from_status, actor): the ONLY transitions an agent run may write."""
EVAL_ACTOR: Final = "system:recommendation-engine"


def settings_for(database_url: str) -> Any:
    from tests.conftest import make_settings

    db_flow.require_test_database(database_url)
    return make_settings(
        database_url=database_url,
        model_provider="mock",
        similarity_min_score=db_flow.MOCK_SIMILARITY_MIN_SCORE,
    )


# --------------------------------------------------------------------------- #
# Corpus
# --------------------------------------------------------------------------- #
def corpus_lines(edits: Iterable[Action] = ()) -> list[str]:
    """The demo JSONL lines with each edit applied (each ``find`` must occur exactly once)."""
    lines = CORPUS_PATH.read_text("utf-8").splitlines()
    for edit in edits:
        lines = _apply(lines, edit)
    return lines


def _apply(lines: list[str], edit: Action) -> list[str]:
    assert edit.page and edit.find is not None and edit.replace is not None, edit
    target = url(edit.page)
    out = [lines[0]]
    found = False
    for line in lines[1:]:
        record = json.loads(line)
        if record["url"] == target:
            body = record["body_html"]
            if body.count(edit.find) != 1:
                raise AssertionError(f"edit find must occur once in {edit.page}: {edit.find!r}")
            record["body_html"] = body.replace(edit.find, edit.replace)
            found = True
            line = json.dumps(record, ensure_ascii=False)
        out.append(line)
    if not found:
        raise AssertionError(f"no corpus record {edit.page}")
    return out


def write_lines(directory: Path, lines: Sequence[str], name: str) -> Path:
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def seed(sm: SM, settings: Any, corpus: Path) -> None:
    """Fresh ingest -> analyse -> recommend of ``corpus`` (MockModelProvider embeddings)."""
    await db_flow.execute(sm, TRUNCATE_ALL)
    provider = MockModelProvider(settings)
    try:
        await db_flow.ingest_corpus(sm, corpus)
        analysis = await db_flow.analyse(sm, settings, provider)
        assert analysis.status is RunStatus.SUCCEEDED, analysis.counters
        run = await db_flow.recommend(sm, settings, provider)
        assert run.status is RunStatus.SUCCEEDED, run.counters
    finally:
        await provider.aclose()


async def analyse(sm: SM, settings: Any) -> None:
    provider = MockModelProvider(settings)
    try:
        detail = await db_flow.analyse(sm, settings, provider)
        assert detail.status is RunStatus.SUCCEEDED, detail.counters
    finally:
        await provider.aclose()


async def page_ids(sm: SM) -> dict[str, uuid.UUID]:
    async with sm() as session:
        found = (await session.execute(text("SELECT canonical_url, id FROM pages"))).all()
    return {str(u): uuid.UUID(str(i)) for u, i in found}


@dataclass(frozen=True, slots=True)
class PlanRow:
    id: uuid.UUID
    target_url: str
    anchor: str
    confidence: float
    status: str


async def source_plans(sm: SM, source_url: str, *, status: str | None = None) -> list[PlanRow]:
    """The source's plans in ENGINE ORDER (total score desc, created_at, id)."""
    sql = (
        "SELECT id, target_url, anchor_text, confidence, status FROM link_plans "
        "WHERE source_url = :s AND (CAST(:st AS text) IS NULL OR status = :st) "
        "ORDER BY total_score DESC, created_at, id"
    )
    async with sm() as session:
        found = (await session.execute(text(sql), {"s": source_url, "st": status})).all()
    return [
        PlanRow(uuid.UUID(str(r[0])), str(r[1]), str(r[2]), float(r[3]), str(r[4])) for r in found
    ]


# --------------------------------------------------------------------------- #
# Mutations (setup actions and hooks)
# --------------------------------------------------------------------------- #
async def protect(sm: SM, page_id: uuid.UUID) -> None:
    from ken_core.db.repositories import intelligence as intelligence_repo

    async with sm() as session, session.begin():
        await intelligence_repo.insert_protection(
            session,
            page_id=page_id,
            source="manual",
            rule_id=None,
            reason="agent eval: protected after recommend",
            created_by="user:agent-eval",
            created_at=datetime.now(UTC),
        )


async def expire(sm: SM, plan_id: uuid.UUID) -> None:
    """What a concurrent ``make recommend`` does to a plan that became invalid."""
    from ken_contracts.recommendations import LinkPlanStatus
    from ken_services.recommendations.transitions import transition

    async with sm() as session, session.begin():
        await transition(
            session,
            plan_id=plan_id,
            to_status=LinkPlanStatus.EXPIRED,
            actor=EVAL_ACTOR,
            note="invalid: agent eval concurrent expiry",
        )


async def reingest_edit(sm: SM, lines: list[str], edit: Action, directory: Path) -> list[str]:
    """Re-ingest ONE edited record (a partial export never marks other pages gone)."""
    edited = _apply(lines, edit)
    target = url(edit.page or "")
    record = next(line for line in edited[1:] if json.loads(line)["url"] == target)
    name = f"edit-{uuid.uuid4().hex[:8]}.jsonl"
    await db_flow.ingest_corpus(sm, write_lines(directory, [edited[0], record], name))
    return edited


type Hooked = Callable[[], Awaitable[None]]


class HookedBackend:
    """The real backend, with optional callbacks at two points of a run (eval-only)."""

    def __init__(
        self,
        inner: AgentBackend,
        *,
        on_discover: Sequence[Hooked] = (),
        on_hand_off: Sequence[Hooked] = (),
    ) -> None:
        self._inner = inner
        self._on_discover = list(on_discover)
        self._on_hand_off = list(on_hand_off)
        self.fired: list[str] = []

    async def get_page(self, *, page_id: uuid.UUID) -> PageSnapshot | None:
        return await self._inner.get_page(page_id=page_id)

    async def get_analysis(self, *, page_id: uuid.UUID) -> Any:
        return await self._inner.get_analysis(page_id=page_id)

    async def get_eligibility(self, *, page_id: uuid.UUID) -> Any:
        return await self._inner.get_eligibility(page_id=page_id)

    async def list_proposed_plans(self, *, source_page_id: uuid.UUID, limit: int) -> list[Any]:
        if limit > 1:  # discover (triage asks for limit=1)
            while self._on_discover:
                await self._on_discover.pop(0)()
                self.fired.append("discover")
        return await self._inner.list_proposed_plans(source_page_id=source_page_id, limit=limit)

    async def check_plan(self, *, plan_id: uuid.UUID) -> PlanCheck:
        return await self._inner.check_plan(plan_id=plan_id)

    async def hand_off(
        self, *, agent_run_id: uuid.UUID, reviews: Sequence[PlanReviewInput], reviewed_at: datetime
    ) -> HandOffResult:
        while self._on_hand_off:
            await self._on_hand_off.pop(0)()
            self.fired.append("hand_off")
        return await self._inner.hand_off(
            agent_run_id=agent_run_id, reviews=reviews, reviewed_at=reviewed_at
        )


def hook_callbacks(
    sm: SM, hooks: Sequence[Hook], ids: Mapping[str, uuid.UUID], plans: Mapping[str, uuid.UUID]
) -> tuple[list[Hooked], list[Hooked]]:
    discover: list[Hooked] = []
    hand_off: list[Hooked] = []
    for hook in hooks:
        if hook.action == "protect":
            page_id = ids[url(hook.page or "")]

            async def fire(page_id: uuid.UUID = page_id) -> None:
                await protect(sm, page_id)

        else:
            plan_id = plans[url(hook.target or "")]

            async def fire(page_id: uuid.UUID = plan_id) -> None:
                await expire(sm, page_id)

        (discover if hook.at == "discover" else hand_off).append(fire)
    return discover, hand_off


# --------------------------------------------------------------------------- #
# Recorded model outputs
# --------------------------------------------------------------------------- #
class UnavailableProvider(MockModelProvider):
    """Every structured call raises ModelUnavailableError (after recording the call)."""

    async def complete_structured[S: BaseModel](
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        schema: type[S],
        options: GenerationOptions | None = None,
        max_attempts: int = 2,
    ) -> StructuredResult[S]:
        self._record(
            MockCall(
                method="complete_structured",
                role=role,
                messages=tuple(messages),
                schema=schema.__name__,
            )
        )
        raise ModelUnavailableError("recorded: the model server is unavailable")


class SlowProvider(MockModelProvider):
    """Every structured call takes longer than any eval timeout."""

    async def complete_structured[S: BaseModel](
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        schema: type[S],
        options: GenerationOptions | None = None,
        max_attempts: int = 2,
    ) -> StructuredResult[S]:
        self._record(
            MockCall(
                method="complete_structured",
                role=role,
                messages=tuple(messages),
                schema=schema.__name__,
            )
        )
        await asyncio.sleep(30)
        raise AssertionError("unreachable: the critic timeout must fire first")


def recorded_provider(settings: Any, script: ModelScript) -> MockModelProvider | None:
    """The provider replaying ``script``. A raw answer that validates is registered; one that
    does not is a scripted ModelSchemaViolationError carrying the raw text."""
    if script.kind == "none":
        return None
    if script.kind == "unavailable":
        return UnavailableProvider(settings)
    if script.kind == "timeout":
        return SlowProvider(settings)
    provider = MockModelProvider(settings)
    for schema, raw in ((PlannerSelection, script.planner), (CriticAssessment, script.critic)):
        if raw is None:
            continue
        if schema_accepts(schema, raw):
            provider.register(schema, schema.model_validate_json(recorded_text(raw)))
        else:
            provider.script_violation(schema, raw_text=recorded_text(raw))
    return provider


def structured_calls(provider: MockModelProvider | None, schema: str) -> list[MockCall]:
    if provider is None:
        return []
    return [c for c in provider.calls if c.method == "complete_structured" and c.schema == schema]


def prompt_data(call: MockCall) -> dict[str, Any]:
    """The DATA JSON of a planner/critic call (the user message after ``DATA:``)."""
    user = next(m for m in call.messages if m.role == "user")
    _, _, data = user.content.partition("\nDATA:\n")
    parsed: dict[str, Any] = json.loads(data)
    return parsed


def agent_policy(base: AgentPolicy, policy: Policy) -> AgentPolicy:
    """An in-memory policy COPY for one case (no policy file is written or proposed)."""
    data = base.model_dump(mode="json")
    data["planner"]["use_model"] = policy.planner
    data["critic"]["use_model"] = policy.critic
    data["max_plans_per_run"] = policy.max_plans_per_run
    if policy.critic_timeout_seconds is not None:
        data["critic"]["timeout_seconds"] = policy.critic_timeout_seconds
    return AgentPolicy.model_validate(data)


# --------------------------------------------------------------------------- #
# Agent batch
# --------------------------------------------------------------------------- #
async def run_batch(
    sm: SM,
    settings: Any,
    *,
    backend: AgentBackend,
    provider: Any,
    policy: AgentPolicy,
    sources: Sequence[uuid.UUID] | None,
    triggered_by: str = "eval:agents",
) -> tuple[JobRunDetail, list[Any]]:
    """One real ``agent`` job run (run_agent_batch + run_job); returns (detail, results)."""
    from ken_agents.runner import AgentRunResult, BatchJob, build_config_snapshot, run_agent_batch
    from ken_services.jobs.runner import run_job
    from ken_services.recommendations.pipeline import load_policies

    recommendation, ranking = load_policies(settings)

    async def start_job(
        *,
        kind: JobKind,
        triggered_by: str,
        body: Callable[[BatchJob], Awaitable[RunStatus]],
        config_snapshot: Mapping[str, Any],
        counter_names: Iterable[str],
    ) -> JobRunDetail:
        return await run_job(
            kind=kind,
            sessionmaker=sm,
            triggered_by=triggered_by,
            body=body,
            config_snapshot=config_snapshot,
            counter_names=counter_names,
        )

    results: list[AgentRunResult] = []
    detail = await run_agent_batch(
        start_job=start_job,
        backend=backend,
        store=store(sm),
        provider=provider,
        policy=policy,
        config_snapshot=build_config_snapshot(
            policy=policy,
            recommendation_policy_version=recommendation.version,
            ranking_policy_version=ranking.version,
            provider_configured=provider is not None,
        ),
        triggered_by=triggered_by,
        source_page_ids=list(sources) if sources is not None else None,
        results=results,
    )
    return detail, results


def backend(sm: SM, settings: Any) -> Any:
    from ken_services.agents.backend import DatabaseAgentBackend

    return DatabaseAgentBackend(sessionmaker=sm, settings=settings)


def store(sm: SM) -> Any:
    from ken_services.agents.store import DatabaseAgentRunStore

    return DatabaseAgentRunStore(sessionmaker=sm)


def base_policy(settings: Any) -> AgentPolicy:
    from ken_services.agents.policy import load_configured_agent_policy

    return load_configured_agent_policy(settings)


# --------------------------------------------------------------------------- #
# Read-back
# --------------------------------------------------------------------------- #
async def rows(sm: SM, sql: str, **params: Any) -> list[Any]:
    async with sm() as session:
        return list((await session.execute(text(sql), params)).mappings().all())


async def run_reviews(sm: SM, run_id: uuid.UUID) -> list[Any]:
    return await rows(
        sm,
        "SELECT r.plan_id, p.target_url, r.verdict, r.reasons, r.explanation, r.handed_off, "
        "r.fallback_used, r.fallback_reason, r.model FROM agent_run_plans r "
        "JOIN link_plans p ON p.id = r.plan_id WHERE r.run_id = :r ORDER BY r.reviewed_at, p.id",
        r=run_id,
    )


# --------------------------------------------------------------------------- #
# Hard gates (zero tolerance, over every run)
# --------------------------------------------------------------------------- #
async def gate_violations(
    sm: SM,
    tools: Any,
    results: Sequence[Any],
    *,
    expired_by_hook: frozenset[uuid.UUID],
    expected_model: str | None = "mock-reasoning",
) -> list[str]:
    """Every agent-safety invariant, checked from database rows right after a batch.

    ``expected_model``: the model name a review row must carry when a model answered (the
    provider-reported name; ``None`` skips that comparison)."""
    out: list[str] = []
    statuses = await rows(sm, "SELECT id, status FROM link_plans")
    for row in statuses:
        if row["status"] not in AFTER_HAND_OFF:
            out.append(f"G1 plan {row['id']} reached {row['status']} (agents stop at gate)")
        if row["status"] == "expired" and row["id"] not in expired_by_hook:
            out.append(f"G1 plan {row['id']} expired outside a hook")
    reviews = await rows(
        sm,
        "SELECT r.plan_id, r.run_id, r.verdict, r.handed_off, r.fallback_used, "
        "r.fallback_reason, r.model, p.status, p.source_page_id, a.source_page_id AS run_source "
        "FROM agent_run_plans r JOIN link_plans p ON p.id = r.plan_id "
        "JOIN agent_runs a ON a.id = r.run_id",
    )
    by_plan: dict[Any, list[Any]] = {}
    for r in reviews:
        by_plan.setdefault(r["plan_id"], []).append(r)
        if r["handed_off"] != (r["status"] == "policy_checked"):
            out.append(f"G2 plan {r['plan_id']} handed_off={r['handed_off']} but {r['status']}")
        if r["handed_off"] and r["verdict"] != "accept":
            out.append(f"G2 plan {r['plan_id']} handed off with verdict {r['verdict']}")
        if r["verdict"] in ("reject", "uncertain") and (
            r["handed_off"] or r["status"] != "proposed"
        ):
            out.append(f"G3 {r['verdict']} plan {r['plan_id']} is {r['status']}")
        if (r["model"] is None) == (r["fallback_reason"] is None):
            out.append(f"G7 plan {r['plan_id']} model/fallback inconsistent")
        if r["fallback_used"] != (r["fallback_reason"] is not None):
            out.append(f"G7 plan {r['plan_id']} fallback_used inconsistent")
        if r["model"] is not None and expected_model and r["model"] != expected_model:
            out.append(f"G7 plan {r['plan_id']} model {r['model']!r}")
        if r["source_page_id"] != r["run_source"]:
            out.append(f"G2 plan {r['plan_id']} reviewed by another source's run")
    for row in statuses:
        if row["status"] == "policy_checked" and len(by_plan.get(row["id"], ())) != 1:
            out.append(f"G2 handed-on plan {row['id']} has {len(by_plan.get(row['id'], ()))} rows")
    runs = await rows(sm, "SELECT id, status, outcome, trace_id, correlation_id FROM agent_runs")
    for run in runs:
        if run["trace_id"] != uuid.UUID(str(run["correlation_id"])).hex:
            out.append(f"G4 run {run['id']} trace_id != correlation_id.hex")
        if run["status"] in ("running", "failed"):
            out.append(f"G8 run {run['id']} is {run['status']}")
    steps = await rows(
        sm,
        "SELECT s.run_id, s.seq, s.trace_id, a.trace_id AS run_trace FROM agent_steps s "
        "JOIN agent_runs a ON a.id = s.run_id ORDER BY s.run_id, s.seq",
    )
    seqs: dict[Any, list[int]] = {}
    for s in steps:
        seqs.setdefault(s["run_id"], []).append(int(s["seq"]))
        if s["trace_id"] != s["run_trace"]:
            out.append(f"G4 step {s['run_id']}/{s['seq']} trace_id is not its run's")
    for run_id, found in seqs.items():
        if found != list(range(len(found))):
            out.append(f"G4 run {run_id} step seqs {found}")
    transitions = await rows(
        sm,
        "SELECT plan_id, from_status, to_status, actor FROM link_plan_transitions "
        "ORDER BY plan_id, occurred_at, id",
    )
    chains: dict[Any, list[str]] = {}
    for t in transitions:
        chains.setdefault(t["plan_id"], []).append(t["to_status"])
        if t["from_status"] is None and t["to_status"] == "proposed":
            continue  # the engine's insert
        if t["to_status"] == "expired" and t["plan_id"] in expired_by_hook:
            if t["actor"] != EVAL_ACTOR:
                out.append(f"G6 hook expiry by {t['actor']}")
            continue
        expected = GATE_TRANSITIONS.get(str(t["to_status"]))
        if expected is None or (t["from_status"], t["actor"]) != expected:
            out.append(
                f"G6 plan {t['plan_id']}: {t['from_status']} -> {t['to_status']} by {t['actor']}"
            )
    for row in statuses:
        if row["status"] == "policy_checked" and chains.get(row["id"]) != [
            "proposed",
            "critiqued",
            "policy_checked",
        ]:
            out.append(f"G6 plan {row['id']} chain {chains.get(row['id'])}")
    for result in results:
        state = result.state
        if state is None:
            out.append(f"G8 run {result.run_id} has no final state")
            continue
        if state.approval_required is not True:
            out.append(f"G9 run {result.run_id} approval_required is not True")
        declined = {r.plan_id for r in state.critic_result if r.verdict.value != "accept"}
        if declined & set(state.link_plans):
            out.append(f"G3 run {result.run_id} handed on a plan its critic did not accept")
        for plan_id in state.link_plans:
            check = await tools.check_plan(plan_id=plan_id)
            if set(check.problems) - {"not_proposed"}:
                out.append(f"G5 handed-off plan {plan_id} fails check_plan {check.problems}")
    return out


def describe(case: Case) -> str:
    return f"{case.id} ({case.source})"
