"""SQL implementation of :class:`ken_contracts.recommendations.RecommendationReader` (read-only).

The API resolves this module by name (``ken_api.deps.RECOMMENDATION_READER``) and builds
``SqlRecommendationReader(sessionmaker=, settings=)`` per request. Nothing here writes.

* **Plans** are listed by ``total_score`` desc, then ``created_at``, then id (a total order).
  Rows are re-validated through the frozen ``LinkPlan`` contract on the way out (the score
  breakdown's identities are re-checked).
* **Plan detail** adds the append-only transition history (oldest first), the source
  version's title, and the FULL text of the block the anchor sits in, re-extracted from the
  source version's stored body by ``context_hash`` (the block id is only a hint).
* **Candidates** are listed newest run first, then by total score desc (rejected last), then
  by row id. ``rejected=True`` lists rejected candidates only, ``False`` kept ones only.
* **Opportunities** are listed by score desc, then canonical URL, then page id.
* **Summary** counts plans by status, distinct sources / targets over ALL plans, and
  opportunities by kind; ``last_run_id`` is the newest recommendation job run; the policy
  versions are those of the configured policy files.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Final

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.errors import ListResponse, PageInfo
from ken_contracts.jobs import JobKind
from ken_contracts.recommendations import (
    CandidateView,
    LinkPlan,
    LinkPlanDetail,
    LinkPlanStatus,
    OpportunityKind,
    OpportunityView,
    PlanNotFoundError,
    RankingPolicy,
    RecommendationPolicy,
    RecommendationSummary,
)
from ken_core.config import REPO_ROOT, Settings
from ken_core.db.models.intelligence import JobRun
from ken_core.db.models.inventory import Page, PageVersion
from ken_core.db.models.recommendations import LinkCandidate, Opportunity
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow
from ken_core.db.repositories import recommendations as repo
from ken_services.inventory.normalize import extract_blocks
from ken_services.recommendations.policy import (
    load_recommendation_policy,
    resolve_ranking_policy,
)

__all__ = ["MAX_LIMIT", "SqlRecommendationReader", "block_text_for"]

MAX_LIMIT: Final = 200


def _check_paging(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        raise ValueError("offset must be >= 0")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def block_text_for(body_html: str, *, context_hash: str, block_id: str) -> str | None:
    """The text of the block whose sha256 is ``context_hash`` (``block_id`` breaks ties)."""
    matches = [b for b in extract_blocks(body_html) if _sha256(b.text) == context_hash]
    if not matches:
        return None
    for block in matches:
        if block.block_id == block_id:
            return block.text
    return matches[0].text


class SqlRecommendationReader:
    """Read-only recommendation views. ``policy`` / ``ranking`` are injectable for tests."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        policy: RecommendationPolicy | None = None,
        ranking: RankingPolicy | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._policy = policy
        self._ranking = ranking

    def _policies(self) -> tuple[RecommendationPolicy, RankingPolicy]:
        policy = self._policy or load_recommendation_policy(
            self._settings.recommendation_policy_path
        )
        ranking = self._ranking or resolve_ranking_policy(policy, root=REPO_ROOT)
        return policy, ranking

    # ------------------------------------------------------------------ plans
    async def list_plans(
        self,
        *,
        limit: int,
        offset: int,
        status: LinkPlanStatus | None = None,
        source_page_id: uuid.UUID | None = None,
        target_page_id: uuid.UUID | None = None,
        min_score: float | None = None,
    ) -> ListResponse[LinkPlan]:
        _check_paging(limit, offset)
        conditions: list[ColumnElement[bool]] = []
        if status is not None:
            conditions.append(LinkPlanRow.status == LinkPlanStatus(status).value)
        if source_page_id is not None:
            conditions.append(LinkPlanRow.source_page_id == source_page_id)
        if target_page_id is not None:
            conditions.append(LinkPlanRow.target_page_id == target_page_id)
        if min_score is not None:
            if not 0.0 <= min_score <= 1.0:
                raise ValueError("min_score must be between 0 and 1")
            conditions.append(LinkPlanRow.total_score >= min_score)
        async with self._sessionmaker() as session:
            total = int(
                await session.scalar(
                    select(func.count()).select_from(LinkPlanRow).where(*conditions)
                )
                or 0
            )
            rows = (
                await session.scalars(
                    select(LinkPlanRow)
                    .where(*conditions)
                    .order_by(
                        LinkPlanRow.total_score.desc(), LinkPlanRow.created_at, LinkPlanRow.id
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        return ListResponse[LinkPlan](
            items=[repo.plan_from_row(r) for r in rows],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    async def get_plan(self, *, plan_id: uuid.UUID) -> LinkPlanDetail:
        async with self._sessionmaker() as session:
            row = await session.get(LinkPlanRow, plan_id)
            if row is None:
                raise PlanNotFoundError
            transitions = await repo.plan_transitions(session, plan_id=plan_id)
            version = (
                await session.execute(
                    select(PageVersion.title, PageVersion.body_html).where(
                        PageVersion.page_id == row.source_page_id,
                        PageVersion.version == row.source_page_version,
                    )
                )
            ).one()
        plan = repo.plan_from_row(row)
        text = block_text_for(
            version.body_html,
            context_hash=plan.locator.context_hash,
            block_id=plan.locator.block_id,
        )
        if text is None:  # pragma: no cover - the plan was placed in this very version
            text = plan.preview.before + plan.preview.anchor + plan.preview.after
        return LinkPlanDetail(
            **plan.model_dump(),
            transitions=transitions,
            source_title=version.title,
            source_block_text=text,
        )

    # ------------------------------------------------------------- candidates
    async def list_candidates(
        self,
        *,
        limit: int,
        offset: int,
        run_id: uuid.UUID | None = None,
        source_page_id: uuid.UUID | None = None,
        rejected: bool | None = None,
    ) -> ListResponse[CandidateView]:
        _check_paging(limit, offset)
        conditions: list[ColumnElement[bool]] = []
        if run_id is not None:
            conditions.append(LinkCandidate.run_id == run_id)
        if source_page_id is not None:
            conditions.append(LinkCandidate.source_page_id == source_page_id)
        if rejected is True:
            conditions.append(LinkCandidate.rejection.is_not(None))
        elif rejected is False:
            conditions.append(LinkCandidate.rejection.is_(None))
        async with self._sessionmaker() as session:
            total = int(
                await session.scalar(
                    select(func.count()).select_from(LinkCandidate).where(*conditions)
                )
                or 0
            )
            rows = (
                await session.execute(
                    select(LinkCandidate, Page.canonical_url)
                    .join(Page, Page.id == LinkCandidate.target_page_id)
                    .where(*conditions)
                    .order_by(
                        LinkCandidate.created_at.desc(),
                        LinkCandidate.total_score.desc().nulls_last(),
                        LinkCandidate.id,
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        return ListResponse[CandidateView](
            items=[repo.candidate_from_row(r[0], r[1]) for r in rows],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    # ---------------------------------------------------------- opportunities
    async def list_opportunities(
        self, *, limit: int, offset: int, kind: OpportunityKind | None = None
    ) -> ListResponse[OpportunityView]:
        _check_paging(limit, offset)
        conditions: list[ColumnElement[bool]] = []
        if kind is not None:
            conditions.append(Opportunity.kind == OpportunityKind(kind).value)
        async with self._sessionmaker() as session:
            total = int(
                await session.scalar(
                    select(func.count()).select_from(Opportunity).where(*conditions)
                )
                or 0
            )
            rows = (
                await session.execute(
                    select(Opportunity, Page.canonical_url, Page.title)
                    .join(Page, Page.id == Opportunity.page_id)
                    .where(*conditions)
                    .order_by(Opportunity.score.desc(), Page.canonical_url, Opportunity.page_id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        return ListResponse[OpportunityView](
            items=[repo.opportunity_from_row(r[0], r[1], r[2]) for r in rows],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    # ---------------------------------------------------------------- summary
    async def get_summary(self) -> RecommendationSummary:
        policy, ranking = self._policies()
        async with self._sessionmaker() as session:
            counts = await repo.plan_counts(session)
            opportunities = await repo.opportunity_counts(session)
            last_run: Any = await session.scalar(
                select(JobRun.id)
                .where(JobRun.kind == JobKind.RECOMMENDATION.value)
                .order_by(JobRun.started_at.desc(), JobRun.id)
                .limit(1)
            )
        return RecommendationSummary(
            plans_by_status=counts.by_status,
            sources_with_plans=counts.sources,
            targets_with_plans=counts.targets,
            opportunities_by_kind=opportunities,
            last_run_id=last_run,
            policy_version=policy.version,
            ranking_policy_version=ranking.version,
        )
