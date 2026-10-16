"""Phase 7 analytics integration support: pages, plans with REAL transitions, deployments.

Deliberately lightweight. The analytics workstream needs plans whose live window, status and
deployment history are precisely controlled (applied, rolled back mid-window, per-plan
REMOVE, superseded, never deployed, overlapping on one source, content changed), which is
hard to arrange through the full recommendation pipeline and trivial here.

Nothing fakes the parts under test: pages come from the real ingestion path, every status
move goes through the real single transition service (so the 0007 status trigger and the
``link_plan_transitions`` rows are real), and every import runs through the real importer.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.analytics import (
    AnalyticsPolicy,
    AnalyticsSource,
    Dataset,
    DateRange,
    FetchStats,
    ImportVia,
    LinkMetrics,
    PageMetrics,
)
from ken_contracts.auth import Role
from ken_contracts.deployment import DeploymentStatus
from ken_contracts.recommendations import LinkPlanStatus
from ken_core.config import Settings
from ken_core.db.models.deployment import ItemStage
from ken_core.providers.analytics.factory import build_analytics_provider
from ken_core.providers.analytics.mock import MockAnalyticsProvider
from ken_services.analytics.importer import run_import
from ken_services.analytics.policy import load_analytics_policy
from ken_services.analytics.protocol import ImportOutcome, ImportRequest
from ken_services.analytics.urls import InventoryUrlResolver
from ken_services.recommendations.transitions import transition
from tests.conftest import make_settings
from tests.integration.conftest import ingest, make_page
from tests.integration.recommendation_support import SITE, make_plan
from tests.integration.recommendation_support import insert_plans as _insert_plans

SM = async_sessionmaker[AsyncSession]
S = LinkPlanStatus
POLICY_PATH: Final = (
    Path(__file__).resolve().parents[2] / "policies" / "analytics" / "analytics.v1.yaml"
)
POLICY: Final[AnalyticsPolicy] = load_analytics_policy(POLICY_PATH)
SETTINGS: Final = make_settings(analytics_policy_path=str(POLICY_PATH), ga4_timezone="Asia/Kolkata")
"""A non-UTC GA4 property zone on purpose: a day-boundary bug cannot hide behind UTC."""
T0: Final = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
PLAN_CREATED: Final = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
"""Plans are created BEFORE every scripted window: the transition service records
``max(clock(), updated_at)``, so a plan created later would drag its own history forward."""

_LINK_HEADER: Final = "date,source,link_id,impressions,clicks"
_PAGE_HEADER: Final = "date,source,url,impressions,clicks,position,pageviews,sessions"


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #
async def seed_pages(sessionmaker: SM, *paths: str) -> dict[str, uuid.UUID]:
    """Ingest one page per path (real ingestion) and return ``path -> page id``."""
    detail = await ingest(sessionmaker, *[make_page(path, body=f"body {path}") for path in paths])
    assert detail.counters.pages_failed == 0, detail.counters
    async with sessionmaker() as session:
        found = (await session.execute(text("SELECT canonical_url, id FROM pages"))).all()
    return {str(url).removeprefix(SITE): uuid.UUID(str(row_id)) for url, row_id in found}


async def add_page_version(
    sessionmaker: SM, path: str, *, body: str, fetched_at: datetime | None = None
) -> None:
    """A real second version of a page: what ``content_changed`` detects.

    ``fetched_at`` is when the new content was OBSERVED, which is the moment the window
    comparison compares it against.
    """
    page = make_page(path, body=body)
    if fetched_at is not None:
        page = page.model_copy(update={"fetched_at": fetched_at})
    detail = await ingest(sessionmaker, page)
    assert detail.counters.pages_failed == 0, detail.counters


# --------------------------------------------------------------------------- #
# Plans and their live windows
# --------------------------------------------------------------------------- #
async def new_plan(
    sessionmaker: SM,
    *,
    source: uuid.UUID,
    target: uuid.UUID,
    source_url: str,
    target_url: str,
    anchor: str = "electric vehicles",
    status: LinkPlanStatus = S.PROPOSED,
    created_at: datetime = PLAN_CREATED,
    **overrides: Any,
) -> uuid.UUID:
    plan = make_plan(
        source,
        target,
        status=status,
        anchor=anchor,
        created_at=created_at,
        source_url=source_url,
        target_url=target_url,
        **overrides,
    )
    await _insert_plans(sessionmaker, plan)
    return plan.id


async def move(
    sessionmaker: SM,
    plan_id: uuid.UUID,
    *,
    to: LinkPlanStatus,
    at: datetime,
    actor: str = "system:test",
) -> None:
    """One real transition at a chosen moment (the clock the service records)."""
    async with sessionmaker() as session, session.begin():
        await transition(session, plan_id=plan_id, to_status=to, actor=actor, clock=lambda: at)


async def make_live(
    sessionmaker: SM,
    plan_id: uuid.UUID,
    *,
    deployed_at: datetime,
    verified_at: datetime | None = None,
    rolled_back_at: datetime | None = None,
) -> None:
    """Walk a proposed plan to ``deployed`` (then optionally ``verified`` / ``rolled_back``).

    Every step is a real transition, so the live window really comes from
    ``link_plan_transitions``.
    """
    await move(sessionmaker, plan_id, to=S.AWAITING_APPROVAL, at=deployed_at - timedelta(days=3))
    await move(
        sessionmaker,
        plan_id,
        to=S.APPROVED,
        at=deployed_at - timedelta(days=2),
        actor="user:rita",
    )
    await move(sessionmaker, plan_id, to=S.VALIDATED, at=deployed_at - timedelta(days=1))
    await move(sessionmaker, plan_id, to=S.DEPLOYED, at=deployed_at)
    if verified_at is not None:
        await move(sessionmaker, plan_id, to=S.VERIFIED, at=verified_at)
    if rolled_back_at is not None:
        await move(sessionmaker, plan_id, to=S.ROLLED_BACK, at=rolled_back_at)


async def supersede(sessionmaker: SM, plan_id: uuid.UUID, *, at: datetime) -> None:
    await move(sessionmaker, plan_id, to=S.AWAITING_APPROVAL, at=at - timedelta(days=1))
    await move(sessionmaker, plan_id, to=S.SUPERSEDED, at=at, actor="user:rita")


# --------------------------------------------------------------------------- #
# Deployments (for the attribution chain and the measure lock order)
# --------------------------------------------------------------------------- #
async def a_user(sessionmaker: SM, username: str = "otto") -> uuid.UUID:
    from tests.integration.review_support import create_user

    return await create_user(sessionmaker, username=username, role=Role.OPERATOR_ADMIN)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def record_deployment(
    sessionmaker: SM,
    *,
    source_page_id: uuid.UUID,
    plan_ids: Sequence[uuid.UUID],
    user_id: uuid.UUID,
    applied_at: datetime,
    verified_at: datetime | None = None,
    status: DeploymentStatus = DeploymentStatus.VERIFIED,
    stage: str = ItemStage.APPLIED,
    cms_provider: str = "mock",
    hrefs: dict[uuid.UUID, str] | None = None,
    status_rolling_back: bool = False,
) -> uuid.UUID:
    """One ``deployments`` row with its applied items, as the deployment service writes them.

    ``status_rolling_back`` records the two-phase rollback intent (``rolling_back`` plus
    ``rollback_from_status``), which is the state the measure sweep must skip.
    """
    deployment_id = uuid.uuid4()
    if status_rolling_back:
        status = DeploymentStatus.ROLLING_BACK
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO deployments (id, operation_id, request_fingerprint, request_json, "
                "cms_provider, cms_page_id, source_page_id, source_page_version, status, actor, "
                "user_id, note, preview, created_at, updated_at, applied_at, verified_at, "
                "rollback_from_status) "
                "VALUES (:id, :op, :fp, :body, :provider, :cms_page, :source, 1, :status, "
                ":actor, :user, '', '[]'::jsonb, :created, :created, :applied, :verified, "
                ":rollback_from)"
            ),
            {
                "id": deployment_id,
                "op": uuid.uuid4(),
                "fp": _sha256(str(deployment_id)),
                "body": json.dumps({"operation_id": str(deployment_id)}),
                "provider": cms_provider,
                "cms_page": f"cms-{source_page_id}",
                "source": source_page_id,
                "status": status.value,
                "actor": "user:otto",
                "user": user_id,
                "created": applied_at,
                "applied": applied_at,
                "verified": verified_at,
                "rollback_from": (DeploymentStatus.APPLIED.value if status_rolling_back else None),
            },
        )
        for plan_id in plan_ids:
            await session.execute(
                text(
                    "INSERT INTO deployment_items (deployment_id, plan_id, change_id, stage, "
                    "href, anchor_text, target_page_id, codes, recorded_at) "
                    "SELECT :deployment, p.id, p.id, :stage, :href, p.anchor_text, "
                    "p.target_page_id, '[]'::jsonb, :at FROM link_plans p WHERE p.id = :plan"
                ),
                {
                    "deployment": deployment_id,
                    "plan": plan_id,
                    "stage": stage,
                    "href": (hrefs or {}).get(plan_id, f"{SITE}/t"),
                    "at": applied_at,
                },
            )
    return deployment_id


async def approve_record(
    sessionmaker: SM, plan_id: uuid.UUID, *, user_id: uuid.UUID, at: datetime
) -> None:
    """The ``review_actions`` row attribution reports as the approving human."""
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO review_actions (operation_id, request_fingerprint, action, actor, "
                "user_id, plan_id, from_status, to_status, reasons, note, anchor_before, "
                "target_before, result, created_at) "
                "SELECT :op, :fp, 'approve', :actor, :user, p.id, 'awaiting_approval', "
                "'approved', '[]'::jsonb, '', p.anchor_text, p.target_page_id, '{}'::jsonb, :at "
                "FROM link_plans p WHERE p.id = :plan"
            ),
            {
                "op": uuid.uuid4(),
                "fp": _sha256(f"approve:{plan_id}"),
                "actor": f"user:{user_id}",
                "user": user_id,
                "plan": plan_id,
                "at": at,
            },
        )


async def record_agent_review(
    sessionmaker: SM,
    plan_id: uuid.UUID,
    *,
    source_page_id: uuid.UUID,
    source_url: str,
    at: datetime,
    verdict: str = "accept",
    model: str = "qwen3:8b",
    graph_version: str = "graph-v1",
) -> uuid.UUID:
    """An ``agent_runs`` + ``agent_run_plans`` pair, so attribution has an agent to report."""
    run_id = uuid.uuid4()
    async with sessionmaker() as session, session.begin():
        job_run_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO job_runs (id, kind, status, triggered_by, started_at, "
                "completed_at, counters, config_snapshot) VALUES (:id, 'agent', "
                "'succeeded', 'test', :at, :at, '{}'::jsonb, '{}'::jsonb)"
            ),
            {"id": job_run_id, "at": at},
        )
        await session.execute(
            text(
                "INSERT INTO agent_runs (id, job_run_id, source_page_id, source_page_version, "
                "source_url, trigger, status, outcome, correlation_id, trace_id, "
                "graph_version, config_snapshot, started_at, finished_at, created_at, "
                "updated_at) "
                "VALUES (:id, :job, :source, 1, :url, 'batch', 'awaiting_approval', "
                "'recommend', :id, :trace, :graph, '{}'::jsonb, :at, :at, :at, :at)"
            ),
            {
                "id": run_id,
                "job": job_run_id,
                "source": source_page_id,
                "url": source_url,
                "trace": run_id.hex,  # the schema requires trace_id = correlation_id without dashes
                "graph": graph_version,
                "at": at,
            },
        )
        await session.execute(
            text(
                "INSERT INTO agent_run_plans (run_id, plan_id, verdict, reasons, explanation, "
                "handed_off, fallback_used, model, reviewed_at) VALUES (:run, :plan, :verdict, "
                "'[]'::jsonb, '', true, false, :model, :at)"
            ),
            {"run": run_id, "plan": plan_id, "verdict": verdict, "model": model, "at": at},
        )
    return run_id


# --------------------------------------------------------------------------- #
# CSV fixtures and import requests
# --------------------------------------------------------------------------- #
def link_csv(directory: Path, rows: Sequence[str], *, name: str = "links.csv") -> Path:
    path = directory / name
    path.write_text("\n".join([_LINK_HEADER, *rows]) + "\n", encoding="utf-8")
    return path


def page_csv(directory: Path, rows: Sequence[str], *, name: str = "pages.csv") -> Path:
    path = directory / name
    path.write_text("\n".join([_PAGE_HEADER, *rows]) + "\n", encoding="utf-8")
    return path


def raw_csv(directory: Path, body: str, *, name: str = "raw.csv") -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def request_for(
    *,
    source: AnalyticsSource,
    dataset: Dataset,
    start: str,
    end: str,
    via: ImportVia = ImportVia.CSV,
    synthetic: bool = False,
    replace_via: bool = False,
    source_timezone: str = "UTC",
) -> ImportRequest:
    return ImportRequest(
        source=source,
        dataset=dataset,
        via=via,
        date_range=DateRange(
            start=datetime.strptime(start, "%Y-%m-%d").date(),
            end=datetime.strptime(end, "%Y-%m-%d").date(),
        ),
        source_timezone=source_timezone,
        synthetic=synthetic,
        replace_via=replace_via,
        triggered_by="test",
    )


# --------------------------------------------------------------------------- #
# Running the real importer
# --------------------------------------------------------------------------- #
async def run_csv_import(
    sessionmaker: SM,
    path: Path,
    *,
    request: ImportRequest,
    now: datetime = T0,
    settings: Settings = SETTINGS,
) -> ImportOutcome:
    """The real CSV provider (built by the real factory) through the real importer."""
    async with sessionmaker() as session:
        resolver = await InventoryUrlResolver.build(session)
    provider = build_analytics_provider(settings, name="csv", resolver=resolver, file=path)
    try:
        return await run_import(
            sessionmaker=sessionmaker,
            settings=settings,
            policy=POLICY,
            provider=provider,
            request=request,
            now=now,
        )
    finally:
        await provider.aclose()


async def run_mock_import(
    sessionmaker: SM,
    *,
    request: ImportRequest,
    link_rows: Sequence[LinkMetrics] = (),
    page_rows: Sequence[PageMetrics] = (),
    stats: FetchStats | None = None,
    now: datetime = T0,
    settings: Settings = SETTINGS,
) -> ImportOutcome:
    """Exact provider rows through the real importer (row-level rules under test)."""
    provider = MockAnalyticsProvider(link_rows=link_rows, page_rows=page_rows, stats=stats)
    return await run_import(
        sessionmaker=sessionmaker,
        settings=settings,
        policy=POLICY,
        provider=provider,
        request=request,
        now=now,
    )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
async def rows_of(sessionmaker: SM, sql: str, **params: Any) -> list[Any]:
    async with sessionmaker() as session:
        return list((await session.execute(text(sql), params)).all())


async def link_metric_rows(sessionmaker: SM) -> list[tuple[Any, ...]]:
    found = await rows_of(
        sessionmaker,
        "SELECT source, metric_date, plan_id, impressions, clicks, synthetic "
        "FROM link_metrics_daily ORDER BY source, metric_date, plan_id",
    )
    return [tuple(row) for row in found]


async def page_metric_rows(sessionmaker: SM) -> list[tuple[Any, ...]]:
    found = await rows_of(
        sessionmaker,
        "SELECT source, metric_date, page_id, impressions, clicks, position_sum, pageviews, "
        "sessions, synthetic FROM page_metrics_daily ORDER BY source, metric_date, page_id",
    )
    return [tuple(row) for row in found]


async def plan_status(sessionmaker: SM, plan_id: uuid.UUID) -> LinkPlanStatus:
    found = await rows_of(sessionmaker, "SELECT status FROM link_plans WHERE id = :p", p=plan_id)
    return LinkPlanStatus(found[0].status)


async def analytics_events(sessionmaker: SM) -> list[Any]:
    return await rows_of(
        sessionmaker,
        "SELECT * FROM events WHERE event_name = 'analytics.updated' ORDER BY occurred_at, id",
    )


async def job_runs(sessionmaker: SM) -> list[Any]:
    return await rows_of(
        sessionmaker,
        "SELECT id, status, failure_reason, counters, config_snapshot FROM job_runs "
        "WHERE kind = 'analytics_import' ORDER BY started_at, id",
    )


__all__ = [
    "PLAN_CREATED",
    "POLICY",
    "POLICY_PATH",
    "SETTINGS",
    "T0",
    "a_user",
    "add_page_version",
    "analytics_events",
    "approve_record",
    "job_runs",
    "link_csv",
    "link_metric_rows",
    "make_live",
    "move",
    "new_plan",
    "page_csv",
    "page_metric_rows",
    "plan_status",
    "raw_csv",
    "record_agent_review",
    "record_deployment",
    "request_for",
    "rows_of",
    "run_csv_import",
    "run_mock_import",
    "seed_pages",
    "supersede",
]
