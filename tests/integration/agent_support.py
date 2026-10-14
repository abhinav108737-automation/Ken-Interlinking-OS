"""Phase 4 integration support: real plans from the real engine, then agent runs over them.

The Phase 3 synthetic corpus (``recommendation_support``) is imported, analysed and
recommended for real, so every plan the agent services see is an engine plan with a verbatim
anchor. Agent runs live inside a ``job_runs`` batch of kind ``agent`` created here directly
(the batch runner belongs to the agent runtime).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text

from ken_contracts.agents import (
    GRAPH_VERSION,
    AgentNode,
    AgentStepView,
    AgentTrigger,
    StepStatus,
)
from ken_contracts.recommendations import LinkPlan, LinkPlanStatus
from ken_core.db.repositories import intelligence as intelligence_repo
from ken_services.agents.backend import DatabaseAgentBackend
from ken_services.agents.protocol import NewAgentRun
from ken_services.agents.store import DatabaseAgentRunStore
from ken_services.recommendations.transitions import ENGINE_ACTOR, transition
from tests.integration.recommendation_support import (
    CHARGING,
    EV,
    LI,
    SETTINGS,
    SM,
    page_ids,
    recommend,
    seed_corpus,
)

T0: Final = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)

__all__ = [
    "CHARGING",
    "EV",
    "LI",
    "SETTINGS",
    "SM",
    "T0",
    "backend",
    "expire",
    "job_run",
    "new_run",
    "page_ids",
    "plans_corpus",
    "protect",
    "set_job_status",
    "start",
    "step",
    "store",
]


def backend(sessionmaker: SM, **kwargs: Any) -> DatabaseAgentBackend:
    return DatabaseAgentBackend(sessionmaker=sessionmaker, settings=SETTINGS, **kwargs)


def store(sessionmaker: SM) -> DatabaseAgentRunStore:
    return DatabaseAgentRunStore(sessionmaker=sessionmaker)


async def plans_corpus(sessionmaker: SM, directory: Path) -> dict[str, uuid.UUID]:
    """Seed, analyse and recommend the synthetic corpus; page ids by path."""
    await seed_corpus(sessionmaker, directory)
    detail = await recommend(sessionmaker)
    assert detail.counters["errors"] == 0, detail.counters
    assert detail.counters["plans_created"] > 0, detail.counters
    return await page_ids(sessionmaker)


async def job_run(sessionmaker: SM, *, status: str = "running") -> uuid.UUID:
    run_id = uuid.uuid4()
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO job_runs (id, kind, status, triggered_by, started_at, completed_at) "
                "VALUES (:id, 'agent', :s, 'test', :t, CASE WHEN :s = 'running' THEN NULL "
                "ELSE CAST(:t AS timestamptz) END)"
            ),
            {"id": run_id, "s": status, "t": T0},
        )
    return run_id


async def set_job_status(sessionmaker: SM, job_id: uuid.UUID, status: str) -> None:
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("UPDATE job_runs SET status = :s, completed_at = :t WHERE id = :id"),
            {"s": status, "t": T0 + timedelta(minutes=5), "id": job_id},
        )


def new_run(
    job_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    started_at: datetime = T0,
    trigger: AgentTrigger = AgentTrigger.BATCH,
) -> NewAgentRun:
    return NewAgentRun(
        id=uuid.uuid4(),
        job_run_id=job_id,
        source_page_id=source_id,
        source_url=f"https://site.test/{source_id}",
        trigger=trigger,
        correlation_id=uuid.uuid4(),
        graph_version=GRAPH_VERSION,
        config_snapshot={"graph_version": GRAPH_VERSION, "agents_policy": {"version": 1}},
        started_at=started_at,
    )


async def start(sessionmaker: SM, source_id: uuid.UUID, **kwargs: Any) -> NewAgentRun:
    job_id = kwargs.pop("job_id", None) or await job_run(sessionmaker)
    run = new_run(job_id, source_id, **kwargs)
    await store(sessionmaker).start_run(run=run)
    return run


def step(run: NewAgentRun, seq: int, **overrides: Any) -> AgentStepView:
    values: dict[str, Any] = {
        "run_id": run.id,
        "seq": seq,
        "node": AgentNode.LOAD_PAGE,
        "status": StepStatus.SUCCEEDED,
        "attempt": 1,
        "started_at": run.started_at + timedelta(seconds=seq),
        "finished_at": run.started_at + timedelta(seconds=seq, milliseconds=5),
        "duration_ms": 5,
        "trace_id": run.trace_id,
        "span_id": f"{seq:016x}",
    } | overrides
    return AgentStepView(**values)


async def expire(sessionmaker: SM, plan_id: uuid.UUID) -> None:
    """What a concurrent ``make recommend`` does to a plan that became invalid."""
    async with sessionmaker() as session, session.begin():
        await transition(
            session,
            plan_id=plan_id,
            to_status=LinkPlanStatus.EXPIRED,
            actor=ENGINE_ACTOR,
            note="invalid: test",
        )


async def protect(sessionmaker: SM, page_id: uuid.UUID) -> None:
    async with sessionmaker() as session, session.begin():
        await intelligence_repo.insert_protection(
            session,
            page_id=page_id,
            source="manual",
            rule_id=None,
            reason="test",
            created_by="user:test",
            created_at=T0,
        )


def engine_order(plans: list[LinkPlan]) -> list[LinkPlan]:
    return sorted(plans, key=lambda p: (-p.score.total, p.created_at, str(p.id)))


async def current_block(sessionmaker: SM, page_id: uuid.UUID, anchor: str) -> tuple[int, Any]:
    """(current version, the first current block containing ``anchor``)."""
    from ken_core.db.repositories import recommendations as repo
    from ken_services.inventory.normalize import extract_blocks

    async with sessionmaker() as session:
        version = int(
            await session.scalar(
                text("SELECT current_version FROM pages WHERE id = :id"), {"id": page_id}
            )
        )
        body = await repo.version_body(session, page_id=page_id, version=version)
    block = next(b for b in extract_blocks(body or "") if anchor in b.text)
    return version, block


async def extra_plan(
    sessionmaker: SM,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    anchor: str,
    *,
    total: float = 0.55,
    **overrides: Any,
) -> LinkPlan:
    """A hand-made ``proposed`` plan placed verbatim on the source's CURRENT version."""
    from ken_contracts.recommendations import PlacementLocator, PlacementPreview
    from ken_services.inventory.hashing import sha256_hex
    from tests.integration.recommendation_support import insert_plans, make_plan

    version, block = await current_block(sessionmaker, source_id, anchor)
    start = block.text.index(anchor)
    end = start + len(anchor)
    values: dict[str, Any] = {
        "source_page_version": version,
        "locator": PlacementLocator(
            block_id=block.block_id,
            block_kind=block.kind,
            occurrence_index=0,
            start=start,
            end=end,
            context_hash=sha256_hex(block.text),
        ),
        "preview": PlacementPreview(
            before=block.text[:start], anchor=anchor, after=block.text[end:]
        ),
    } | overrides
    plan = make_plan(
        source_id,
        target_id,
        block_text=block.text,
        anchor=anchor,
        total=total,
        created_at=T0,
        **values,
    )
    await insert_plans(sessionmaker, plan)
    return plan
