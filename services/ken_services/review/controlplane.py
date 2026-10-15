"""Control-plane read views (ADR-0013 section 7): dashboard, event log, active policies.

The API resolves this module by name (``ken_api.deps.CONTROL_PLANE_READER``) and builds
``SqlControlPlaneReader(sessionmaker=, settings=)`` per request. READ-ONLY.

* :meth:`dashboard` -- page, plan, opportunity, agent-run and review counts, the 10 newest
  review actions and the latest run of each job kind. ``pages_analyzed`` counts pages whose
  CURRENT version has an analysis at the CURRENT analysis version; ``agent_runs_by_outcome``
  counts a still-running run under ``running``.
* :meth:`list_events` -- the append-only event log, newest first (by id: insertion order is
  total and stable), filtered by event name and/or subject id; rows are re-validated.
* :meth:`policies` -- the four ACTIVE policy documents (taxonomy, recommendation, ranking,
  agents), loaded and validated by the existing loaders: the validated model dump, its
  version, the sha256 of the file's bytes and its repository-relative path (only the file
  name for a file outside the repository: the filesystem layout is not published).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final, Protocol

from sqlalchemy import and_, distinct, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from ken_contracts.controlplane import (
    DashboardSummary,
    EventRecord,
    PoliciesView,
    PolicyDocument,
    PolicyKind,
)
from ken_contracts.errors import ListResponse, PageInfo
from ken_contracts.events import EventName
from ken_contracts.intelligence import analysis_version
from ken_contracts.jobs import JobKind, JobRunSummary
from ken_contracts.recommendations import LinkPlanStatus
from ken_core.config import REPO_ROOT, Settings
from ken_core.db.models.agents import AgentRun
from ken_core.db.models.event import Event
from ken_core.db.models.intelligence import JobRun, PageAnalysis, PageProtection
from ken_core.db.models.inventory import Page
from ken_core.db.repositories import jobs as jobs_repo
from ken_core.db.repositories import recommendations as rec_repo
from ken_core.db.repositories import review as review_repo
from ken_services.agents.policy import load_configured_agent_policy
from ken_services.analytics.policy import load_analytics_policy
from ken_services.intelligence.taxonomy import load_taxonomy
from ken_services.recommendations.policy import load_recommendation_policy, resolve_ranking_policy

__all__ = ["MAX_LIMIT", "RECENT_REVIEW_ACTIONS", "ControlPlaneReader", "SqlControlPlaneReader"]

MAX_LIMIT: Final = 200
RECENT_REVIEW_ACTIONS: Final = 10
RUNNING: Final = "running"


class ControlPlaneReader(Protocol):
    async def dashboard(self) -> DashboardSummary: ...

    async def list_events(
        self,
        *,
        limit: int,
        offset: int,
        event_name: EventName | None = None,
        subject_id: str | None = None,
    ) -> ListResponse[EventRecord]: ...

    async def policies(self) -> PoliciesView: ...


def _repo_relative(path: Path) -> str:
    resolved = path.resolve()
    root = REPO_ROOT.resolve()
    return str(resolved.relative_to(root)) if resolved.is_relative_to(root) else resolved.name


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SqlControlPlaneReader:
    def __init__(
        self, *, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings

    # -------------------------------------------------------------- dashboard
    async def dashboard(self) -> DashboardSummary:
        av = analysis_version(load_taxonomy(self._settings.taxonomy_path).version)
        async with self._sessionmaker() as session:
            pages_total = int(await session.scalar(select(func.count()).select_from(Page)) or 0)
            pages_live = int(
                await session.scalar(
                    select(func.count()).select_from(Page).where(Page.gone_at.is_(None))
                )
                or 0
            )
            analysis = aliased(PageAnalysis)
            pages_analyzed = int(
                await session.scalar(
                    select(func.count(distinct(Page.id))).join(
                        analysis,
                        and_(
                            analysis.page_id == Page.id,
                            analysis.page_version == Page.current_version,
                            analysis.analysis_version == av,
                        ),
                    )
                )
                or 0
            )
            pages_protected = int(
                await session.scalar(
                    select(func.count(distinct(PageProtection.page_id))).where(
                        PageProtection.active.is_(True)
                    )
                )
                or 0
            )
            plans = await rec_repo.plan_counts(session)
            opportunities = await rec_repo.opportunity_counts(session)
            runs = {
                RUNNING if outcome is None else str(outcome): int(count)
                for outcome, count in (
                    await session.execute(
                        select(AgentRun.outcome, func.count())
                        .group_by(AgentRun.outcome)
                        .order_by(AgentRun.outcome)
                    )
                ).all()
            }
            reviews = await review_repo.action_counts(session)
            recent, _total = await review_repo.list_actions(
                session, limit=RECENT_REVIEW_ACTIONS, offset=0
            )
            latest = (
                await session.scalars(
                    select(JobRun)
                    .ext(distinct_on(JobRun.kind))
                    .order_by(JobRun.kind, JobRun.started_at.desc(), JobRun.id.desc())
                )
            ).all()
        order = {kind.value: n for n, kind in enumerate(JobKind)}
        latest_jobs: list[JobRunSummary] = sorted(
            (jobs_repo.to_summary(row) for row in latest),
            key=lambda s: order.get(s.kind.value, len(order)),
        )
        return DashboardSummary(
            pages_total=pages_total,
            pages_live=pages_live,
            pages_analyzed=pages_analyzed,
            pages_protected=pages_protected,
            plans_by_status=plans.by_status,
            awaiting_approval=plans.by_status.get(LinkPlanStatus.AWAITING_APPROVAL.value, 0),
            opportunities_by_kind=opportunities,
            agent_runs_by_outcome=runs,
            reviews_by_action=reviews,
            recent_review_actions=recent,
            latest_jobs=latest_jobs,
        )

    # ----------------------------------------------------------------- events
    async def list_events(
        self,
        *,
        limit: int,
        offset: int,
        event_name: EventName | None = None,
        subject_id: str | None = None,
    ) -> ListResponse[EventRecord]:
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        conditions = []
        if event_name is not None:
            conditions.append(Event.event_name == EventName(event_name).value)
        if subject_id is not None:
            conditions.append(Event.subject_id == subject_id)
        async with self._sessionmaker() as session:
            total = int(
                await session.scalar(select(func.count()).select_from(Event).where(*conditions))
                or 0
            )
            rows = (
                await session.scalars(
                    select(Event)
                    .where(*conditions)
                    .order_by(Event.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        items = [
            EventRecord(
                id=row.id,
                event_id=row.event_id,
                event_name=EventName(row.event_name),
                event_version=row.event_version,
                occurred_at=row.occurred_at,
                emitted_by=row.emitted_by,
                subject_type=row.subject_type,
                subject_id=row.subject_id,
                payload=dict(row.payload),
                correlation_id=row.correlation_id,
            )
            for row in rows
        ]
        return ListResponse[EventRecord](
            items=items, page=PageInfo(total=total, limit=limit, offset=offset)
        )

    # --------------------------------------------------------------- policies
    async def policies(self) -> PoliciesView:
        settings = self._settings
        taxonomy_path = Path(settings.taxonomy_path)
        recommendation_path = Path(settings.recommendation_policy_path)
        agents_path = Path(settings.agents_policy_path)
        taxonomy = load_taxonomy(taxonomy_path)
        recommendation = load_recommendation_policy(recommendation_path)
        ranking = resolve_ranking_policy(recommendation, root=REPO_ROOT)
        ranking_path = (REPO_ROOT / recommendation.ranking_policy).resolve()
        agents = load_configured_agent_policy(settings)
        analytics_path = Path(settings.analytics_policy_path)
        analytics = load_analytics_policy(analytics_path)
        documents: list[tuple[PolicyKind, int | str, Path, dict[str, object]]] = [
            ("taxonomy", taxonomy.version, taxonomy_path, taxonomy.model_dump(mode="json")),
            (
                "recommendation",
                recommendation.version,
                recommendation_path,
                recommendation.model_dump(mode="json"),
            ),
            ("ranking", ranking.version, ranking_path, ranking.model_dump(mode="json")),
            ("agents", agents.version, agents_path, agents.model_dump(mode="json")),
            (
                "analytics",
                analytics.version,
                analytics_path,
                analytics.model_dump(mode="json"),
            ),
        ]
        return PoliciesView(
            policies=[
                PolicyDocument(
                    kind=kind,
                    version=version,
                    path=_repo_relative(path),
                    sha256=_sha256_file(path),
                    content=content,
                )
                for kind, version, path, content in documents
            ]
        )
