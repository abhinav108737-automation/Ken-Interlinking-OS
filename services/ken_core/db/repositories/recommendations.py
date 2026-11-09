"""Recommendation repository: SQL primitives for link plans, transitions, candidates and
opportunities (ADR-0011), plus the live page/link facts the engine reads.

The caller owns the transaction: nothing here commits. There is deliberately no
function that updates plan CONTENT or deletes a plan, a transition or a
candidate (migration 0004's triggers would refuse it anyway): the only plan
UPDATE is :func:`set_plan_status`, and only the transition service
(``ken_services.recommendations.transitions``) calls it. ``opportunities`` is
derived data and is replaced per run.

"Current" always means the page's ``pages.current_version``. A link from page S
"reaches" page T when it is an internal body link of S's CURRENT version whose
``resolved_url`` is a ``known_urls`` row that has led to T (redirects and
aliases included; ADR-0009 section 5). For INBOUND counts a link whose URL's
latest outcome is a definitive 404/410, or whose source page is gone, is not
counted; for ``already_linked`` any link that has led to T counts (never
propose a link that is already there).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import ColumnElement, and_, delete, exists, func, or_, select, union, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ken_contracts.intelligence import PageAnalysis
from ken_contracts.inventory import LinkType, UrlOutcome
from ken_contracts.recommendations import (
    LIVE_STATUSES,
    OPEN_STATUSES,
    CandidateRejection,
    CandidateView,
    LinkPlan,
    LinkPlanStatus,
    ModuleKind,
    OpportunityKind,
    OpportunityView,
    PageModuleItemView,
    PageModuleView,
    PlacementLocator,
    PlacementPreview,
    PlanTransitionView,
    ReasonCode,
    RetrievalChannel,
    ScoreBreakdown,
)
from ken_contracts.review import ReviewAction
from ken_core.db.models.deployment import Deployment, DeploymentItem, ItemStage
from ken_core.db.models.intelligence import PageAnalysis as PageAnalysisRow
from ken_core.db.models.intelligence import PageProtection
from ken_core.db.models.inventory import ExistingLink, KnownUrl, Page, PageVersion
from ken_core.db.models.recommendations import (
    LinkCandidate,
    LinkPlanTransition,
    Opportunity,
    PageModule,
    PageModuleItem,
)
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow
from ken_core.db.models.review import ReviewActionRow
from ken_core.db.repositories.intelligence import analysis_from_row

OPEN_VALUES: Final = tuple(sorted(s.value for s in OPEN_STATUSES))
LIVE_VALUES: Final = tuple(sorted(s.value for s in LIVE_STATUSES))
_BROKEN_OUTCOMES: Final = (UrlOutcome.NOT_FOUND.value, UrlOutcome.GONE.value)
_INTERNAL: Final = LinkType.INTERNAL.value


# --------------------------------------------------------------------------- #
# Row <-> contract mapping
# --------------------------------------------------------------------------- #
def plan_from_row(row: LinkPlanRow) -> LinkPlan:
    """Re-validate a stored plan through the contract on the way out."""
    return LinkPlan(
        id=row.id,
        run_id=row.run_id,
        source_page_id=row.source_page_id,
        source_page_version=row.source_page_version,
        source_url=row.source_url,
        target_page_id=row.target_page_id,
        target_page_version=row.target_page_version,
        target_url=row.target_url,
        target_title=row.target_title,
        relationship_type=row.relationship_type,
        channels=tuple(RetrievalChannel(c) for c in row.channels),
        anchor_text=row.anchor_text,
        anchor_source=row.anchor_source,
        locator=PlacementLocator(
            block_id=row.block_id,
            block_kind=row.block_kind,
            occurrence_index=row.occurrence_index,
            start=row.start_offset,
            end=row.end_offset,
            context_hash=row.context_hash,
        ),
        preview=PlacementPreview.model_validate(row.preview),
        score=ScoreBreakdown.model_validate(row.score),
        confidence=row.confidence,
        calibration_version=row.calibration_version,
        reason_codes=tuple(row.reason_codes),
        rationale=row.rationale,
        engine_version=row.engine_version,
        policy_version=row.policy_version,
        ranking_policy_version=row.ranking_policy_version,
        embedding_model=row.embedding_model,
        reasoning_model=row.reasoning_model,
        analysis_version=row.analysis_version,
        supersedes=row.supersedes,
        status=LinkPlanStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def transition_from_row(row: LinkPlanTransition) -> PlanTransitionView:
    return PlanTransitionView(
        id=row.id,
        plan_id=row.plan_id,
        from_status=None if row.from_status is None else LinkPlanStatus(row.from_status),
        to_status=LinkPlanStatus(row.to_status),
        actor=row.actor,
        note=row.note,
        occurred_at=row.occurred_at,
    )


def candidate_from_row(row: LinkCandidate, target_url: str) -> CandidateView:
    return CandidateView(
        run_id=row.run_id,
        source_page_id=row.source_page_id,
        target_page_id=row.target_page_id,
        target_url=target_url,
        channels=tuple(RetrievalChannel(c) for c in row.channels),
        channel_scores={str(k): float(v) for k, v in row.channel_scores.items()},
        total_score=row.total_score,
        rejection=None if row.rejection is None else CandidateRejection(row.rejection),
        plan_id=row.plan_id,
    )


def opportunity_from_row(
    row: Opportunity, canonical_url: str, title: str | None
) -> OpportunityView:
    return OpportunityView(
        page_id=row.page_id,
        canonical_url=canonical_url,
        title=title,
        kind=OpportunityKind(row.kind),
        score=row.score,
        evidence=dict(row.evidence),
        run_id=row.run_id,
        computed_at=row.computed_at,
    )


# --------------------------------------------------------------------------- #
# Plans and transitions
# --------------------------------------------------------------------------- #
async def insert_plan(session: AsyncSession, *, plan: LinkPlan) -> None:
    """Insert one (already validated) plan. Its content is immutable from here on."""
    plan = LinkPlan.model_validate(plan.model_dump())  # re-validate at the boundary
    session.add(
        LinkPlanRow(
            id=plan.id,
            run_id=plan.run_id,
            source_page_id=plan.source_page_id,
            source_page_version=plan.source_page_version,
            source_url=plan.source_url,
            target_page_id=plan.target_page_id,
            target_page_version=plan.target_page_version,
            target_url=plan.target_url,
            target_title=plan.target_title,
            relationship_type=None
            if plan.relationship_type is None
            else plan.relationship_type.value,
            channels=[c.value for c in plan.channels],
            anchor_text=plan.anchor_text,
            anchor_source=plan.anchor_source,
            block_id=plan.locator.block_id,
            block_kind=plan.locator.block_kind,
            occurrence_index=plan.locator.occurrence_index,
            start_offset=plan.locator.start,
            end_offset=plan.locator.end,
            context_hash=plan.locator.context_hash,
            preview=plan.preview.model_dump(mode="json"),
            score=plan.score.model_dump(mode="json"),
            total_score=plan.score.total,
            confidence=plan.confidence,
            calibration_version=plan.calibration_version,
            reason_codes=[r.value for r in plan.reason_codes],
            rationale=plan.rationale,
            engine_version=plan.engine_version,
            policy_version=plan.policy_version,
            ranking_policy_version=plan.ranking_policy_version,
            embedding_model=plan.embedding_model,
            reasoning_model=plan.reasoning_model,
            analysis_version=plan.analysis_version,
            supersedes=plan.supersedes,
            status=plan.status.value,
            created_at=plan.created_at,
            updated_at=plan.updated_at,
        )
    )
    await session.flush()


async def lock_plan(session: AsyncSession, *, plan_id: uuid.UUID) -> LinkPlanRow | None:
    """The plan row, locked ``FOR UPDATE`` until the caller's transaction ends."""
    return (
        await session.scalars(
            select(LinkPlanRow)
            .where(LinkPlanRow.id == plan_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).first()


async def set_plan_status(
    session: AsyncSession, *, plan_id: uuid.UUID, status: LinkPlanStatus, now: datetime
) -> None:
    """The ONLY plan UPDATE: status and updated_at (the content trigger allows nothing else)."""
    await session.execute(
        update(LinkPlanRow)
        .where(LinkPlanRow.id == plan_id)
        .values(status=LinkPlanStatus(status).value, updated_at=now)
        .execution_options(synchronize_session=False)
    )


async def insert_transition(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    from_status: LinkPlanStatus | None,
    to_status: LinkPlanStatus,
    actor: str,
    note: str | None,
    occurred_at: datetime,
) -> PlanTransitionView:
    row = LinkPlanTransition(
        plan_id=plan_id,
        from_status=None if from_status is None else LinkPlanStatus(from_status).value,
        to_status=LinkPlanStatus(to_status).value,
        actor=actor,
        note=note,
        occurred_at=occurred_at,
    )
    session.add(row)
    await session.flush()
    return transition_from_row(row)


async def plan_transitions(
    session: AsyncSession, *, plan_id: uuid.UUID
) -> list[PlanTransitionView]:
    rows = (
        await session.scalars(
            select(LinkPlanTransition)
            .where(LinkPlanTransition.plan_id == plan_id)
            .order_by(LinkPlanTransition.id)
        )
    ).all()
    return [transition_from_row(r) for r in rows]


@dataclass(frozen=True, slots=True)
class OpenPlan:
    """An OPEN plan of a source: what occupies a pair and an anchor span."""

    id: uuid.UUID
    target_page_id: uuid.UUID
    source_page_version: int
    status: LinkPlanStatus
    anchor_text: str
    locator: PlacementLocator


async def open_plans_for_source(
    session: AsyncSession, *, source_page_id: uuid.UUID
) -> list[OpenPlan]:
    rows = (
        await session.scalars(
            select(LinkPlanRow)
            .where(
                LinkPlanRow.source_page_id == source_page_id,
                LinkPlanRow.status.in_(OPEN_VALUES),
            )
            .order_by(LinkPlanRow.created_at, LinkPlanRow.id)
        )
    ).all()
    return [
        OpenPlan(
            id=r.id,
            target_page_id=r.target_page_id,
            source_page_version=r.source_page_version,
            status=LinkPlanStatus(r.status),
            anchor_text=r.anchor_text,
            locator=PlacementLocator(
                block_id=r.block_id,
                block_kind=r.block_kind,
                occurrence_index=r.occurrence_index,
                start=r.start_offset,
                end=r.end_offset,
                context_hash=r.context_hash,
            ),
        )
        for r in rows
    ]


async def open_plan_counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    """source page id -> number of its OPEN plans."""
    rows = (
        await session.execute(
            select(LinkPlanRow.source_page_id, func.count())
            .where(LinkPlanRow.status.in_(OPEN_VALUES))
            .group_by(LinkPlanRow.source_page_id)
        )
    ).all()
    return {r[0]: int(r[1]) for r in rows}


async def open_plan_target_counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    """target page id -> number of OPEN plans (from any source) pointing at it."""
    rows = (
        await session.execute(
            select(LinkPlanRow.target_page_id, func.count())
            .where(LinkPlanRow.status.in_(OPEN_VALUES))
            .group_by(LinkPlanRow.target_page_id)
        )
    ).all()
    return {r[0]: int(r[1]) for r in rows}


OCCUPYING_LIVE_VALUES: Final = LIVE_VALUES
"""Plans whose link is on the page: they occupy their pair until ``rolled_back``."""


def occupying_condition() -> ColumnElement[bool]:
    """ADR-0014 s8 pair occupancy (amends ADR-0011's live rule): a plan whose link is
    PHYSICALLY on the page occupies its (source, target) pair REGARDLESS of its
    ``source_page_version``, until it is ``rolled_back``:

    * ``deployed``, ``verified``, ``measured``, ``learning_captured``;
    * ``failed`` when its deployment was APPLIED (``deployments.applied_at`` set: the link
      reached the page, then verification failed). A plan that failed at APPLY (the CMS
      refused it) never reached the page and does not occupy the pair.
    """
    applied = (
        select(DeploymentItem.id)
        .join(Deployment, Deployment.id == DeploymentItem.deployment_id)
        .where(
            DeploymentItem.plan_id == LinkPlanRow.id,
            DeploymentItem.stage == ItemStage.REQUESTED,
            Deployment.applied_at.is_not(None),
        )
        .correlate(LinkPlanRow)
        .exists()
    )
    return or_(
        LinkPlanRow.status.in_(OCCUPYING_LIVE_VALUES),
        and_(LinkPlanRow.status == LinkPlanStatus.FAILED.value, applied),
    )


async def occupying_targets(session: AsyncSession, *, source_page_id: uuid.UUID) -> set[uuid.UUID]:
    """Targets of the source's plans whose link is on the page (:func:`occupying_condition`).
    Together with the OPEN plans, these are the engine's ``duplicate_recommendation`` pairs."""
    rows = await session.scalars(
        select(LinkPlanRow.target_page_id).where(
            LinkPlanRow.source_page_id == source_page_id, occupying_condition()
        )
    )
    return set(rows.all())


async def rejected_targets_since(
    session: AsyncSession, *, source_page_id: uuid.UUID, since: datetime
) -> set[uuid.UUID]:
    """Targets a reviewer turned down for this source at or after ``since`` (the cooldown):
    REJECTED plans, and destinations a reviewer REPLACED (``review_actions.action =
    'replace_target'``, ADR-0013 s7) -- otherwise the next run would re-propose the very
    destination the reviewer just replaced."""
    rejected = (
        select(LinkPlanRow.target_page_id)
        .join(LinkPlanTransition, LinkPlanTransition.plan_id == LinkPlanRow.id)
        .where(
            LinkPlanRow.source_page_id == source_page_id,
            LinkPlanTransition.to_status == LinkPlanStatus.REJECTED.value,
            LinkPlanTransition.occurred_at >= since,
        )
    )
    replaced = (
        select(ReviewActionRow.target_before)
        .join(LinkPlanRow, LinkPlanRow.id == ReviewActionRow.plan_id)
        .where(
            LinkPlanRow.source_page_id == source_page_id,
            ReviewActionRow.action == ReviewAction.REPLACE_TARGET.value,
            ReviewActionRow.created_at >= since,
        )
    )
    rows = await session.scalars(union(rejected, replaced))
    return set(rows.all())


# --------------------------------------------------------------------------- #
# Candidates and opportunities
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class NewCandidate:
    source_page_id: uuid.UUID
    source_page_version: int
    target_page_id: uuid.UUID
    target_page_version: int
    channels: tuple[RetrievalChannel, ...]
    channel_scores: Mapping[str, float]
    total_score: float | None
    rejection: CandidateRejection | None
    plan_id: uuid.UUID | None


async def insert_candidates(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    candidates: Sequence[NewCandidate],
    created_at: datetime,
) -> int:
    for item in candidates:
        session.add(
            LinkCandidate(
                run_id=run_id,
                source_page_id=item.source_page_id,
                source_page_version=item.source_page_version,
                target_page_id=item.target_page_id,
                target_page_version=item.target_page_version,
                channels=[RetrievalChannel(c).value for c in item.channels],
                channel_scores={str(k): float(v) for k, v in item.channel_scores.items()},
                total_score=item.total_score,
                rejection=None if item.rejection is None else item.rejection.value,
                plan_id=item.plan_id,
                created_at=created_at,
            )
        )
    await session.flush()
    return len(candidates)


@dataclass(frozen=True, slots=True)
class NewOpportunity:
    page_id: uuid.UUID
    kind: OpportunityKind
    score: float
    evidence: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class NewModuleItem:
    rank: int
    target_page_id: uuid.UUID
    target_url: str
    target_title: str | None
    geography_label: str | None
    reason_codes: Sequence[ReasonCode]


@dataclass(frozen=True, slots=True)
class NewModule:
    page_id: uuid.UUID
    page_version: int
    kind: ModuleKind
    market_core: str
    geography_id: str | None
    geography_label: str | None
    analysis_version: str
    items: Sequence[NewModuleItem]


async def replace_modules(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    modules: Sequence[NewModule],
    computed_at: datetime,
) -> int:
    """Replace every module of the kinds present with this run's. Returns items written.

    Only the kinds this run computed are cleared, so a future ``adjacent`` build cannot
    silently delete the regional-sibling modules by being the more recent run. Items go with
    their module through ``ON DELETE CASCADE``.
    """
    kinds = {ModuleKind(module.kind).value for module in modules}
    if kinds:
        await session.execute(delete(PageModule).where(PageModule.kind.in_(sorted(kinds))))
    items = 0
    for module in modules:
        module_id = uuid.uuid4()
        session.add(
            PageModule(
                id=module_id,
                run_id=run_id,
                page_id=module.page_id,
                page_version=module.page_version,
                kind=ModuleKind(module.kind).value,
                market_core=module.market_core,
                geography_id=module.geography_id,
                geography_label=module.geography_label,
                analysis_version=module.analysis_version,
                computed_at=computed_at,
            )
        )
        for item in module.items:
            session.add(
                PageModuleItem(
                    module_id=module_id,
                    rank=item.rank,
                    target_page_id=item.target_page_id,
                    target_url=item.target_url,
                    target_title=item.target_title,
                    geography_label=item.geography_label,
                    reason_codes=[ReasonCode(code).value for code in item.reason_codes],
                )
            )
            items += 1
    await session.flush()
    return items


def module_from_rows(
    row: PageModule,
    canonical_url: str,
    title: str | None,
    items: Sequence[PageModuleItem],
) -> PageModuleView:
    return PageModuleView(
        page_id=row.page_id,
        canonical_url=canonical_url,
        title=title,
        kind=ModuleKind(row.kind),
        market_core=row.market_core,
        geography_label=row.geography_label,
        items=[
            PageModuleItemView(
                rank=item.rank,
                target_page_id=item.target_page_id,
                target_url=item.target_url,
                target_title=item.target_title,
                geography_label=item.geography_label,
                reason_codes=[ReasonCode(code) for code in item.reason_codes],
            )
            for item in sorted(items, key=lambda i: i.rank)
        ],
        run_id=row.run_id,
        analysis_version=row.analysis_version,
        computed_at=row.computed_at,
    )


async def module_items(
    session: AsyncSession, *, module_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[PageModuleItem]]:
    """Every item of the given modules, keyed by module id (one query, not N)."""
    out: dict[uuid.UUID, list[PageModuleItem]] = {module_id: [] for module_id in module_ids}
    if not module_ids:
        return out
    rows = await session.scalars(
        select(PageModuleItem)
        .where(PageModuleItem.module_id.in_(list(module_ids)))
        .order_by(PageModuleItem.module_id, PageModuleItem.rank)
    )
    for item in rows.all():
        out[item.module_id].append(item)
    return out


async def replace_opportunities(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    opportunities: Sequence[NewOpportunity],
    computed_at: datetime,
) -> int:
    """Replace the whole (derived) table with this run's opportunities."""
    await session.execute(delete(Opportunity))
    for item in opportunities:
        session.add(
            Opportunity(
                run_id=run_id,
                page_id=item.page_id,
                kind=OpportunityKind(item.kind).value,
                score=float(item.score),
                evidence=dict(item.evidence),
                computed_at=computed_at,
            )
        )
    await session.flush()
    return len(opportunities)


# --------------------------------------------------------------------------- #
# Live page facts
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PageFacts:
    """Everything the engine needs to know about one page NOW (current version)."""

    id: uuid.UUID
    canonical_url: str
    title: str | None
    current_version: int
    publication_state: str
    gone: bool
    word_count: int
    internal_link_count: int
    robots_directives: tuple[str, ...]
    canonical_status: str | None
    http_status: int | None
    protected: bool
    has_any_analysis: bool
    analysis: PageAnalysis | None
    """The current version's analysis, the CURRENT analysis version's preferred."""


async def page_facts(
    session: AsyncSession, *, analysis_version: str, page_ids: Iterable[uuid.UUID] | None = None
) -> dict[uuid.UUID, PageFacts]:
    """Live facts of every page (or of ``page_ids``), keyed by page id."""
    current = aliased(PageAnalysisRow)
    preferred = (
        select(PageAnalysisRow.id)
        .where(
            PageAnalysisRow.page_id == Page.id,
            PageAnalysisRow.page_version == Page.current_version,
        )
        .order_by(
            (PageAnalysisRow.analysis_version == analysis_version).desc(),
            PageAnalysisRow.created_at.desc(),
            PageAnalysisRow.id,
        )
        .limit(1)
        .correlate(Page)
        .scalar_subquery()
    )
    statement = (
        select(
            Page,
            PageVersion.robots_directives,
            PageVersion.canonical_status,
            PageVersion.http_status,
            current,
            exists()
            .where(PageProtection.page_id == Page.id, PageProtection.active.is_(True))
            .label("protected"),
            exists().where(PageAnalysisRow.page_id == Page.id).label("any_analysis"),
        )
        .join(
            PageVersion,
            and_(PageVersion.page_id == Page.id, PageVersion.version == Page.current_version),
        )
        .outerjoin(current, current.id == preferred)
        .order_by(Page.id)
    )
    if page_ids is not None:
        statement = statement.where(Page.id.in_(list(page_ids)))
    out: dict[uuid.UUID, PageFacts] = {}
    for row in (await session.execute(statement)).all():
        page: Page = row[0]
        analysis_row: PageAnalysisRow | None = row[4]
        out[page.id] = PageFacts(
            id=page.id,
            canonical_url=page.canonical_url,
            title=page.title,
            current_version=page.current_version,
            publication_state=page.publication_state,
            gone=page.gone_at is not None,
            word_count=page.word_count,
            internal_link_count=page.internal_link_count,
            robots_directives=tuple(str(d) for d in row.robots_directives or ()),
            canonical_status=row.canonical_status,
            http_status=row.http_status,
            protected=bool(row.protected),
            has_any_analysis=bool(row.any_analysis),
            analysis=None if analysis_row is None else analysis_from_row(analysis_row),
        )
    return out


async def version_body(session: AsyncSession, *, page_id: uuid.UUID, version: int) -> str | None:
    return (
        await session.scalars(
            select(PageVersion.body_html).where(
                PageVersion.page_id == page_id, PageVersion.version == version
            )
        )
    ).first()


@dataclass(frozen=True, slots=True)
class SourceLink:
    """One body link of a source version, with the page it has led to (if any)."""

    block_id: str
    link_type: str
    target_page_id: uuid.UUID | None


async def version_links(
    session: AsyncSession, *, page_id: uuid.UUID, version: int
) -> list[SourceLink]:
    """Every body link of one version; internal ones resolved through ``known_urls``."""
    known = aliased(KnownUrl)
    rows = (
        await session.execute(
            select(ExistingLink.block_id, ExistingLink.link_type, known.page_id)
            .outerjoin(
                known,
                and_(
                    ExistingLink.link_type == _INTERNAL,
                    known.url == ExistingLink.resolved_url,
                ),
            )
            .where(ExistingLink.page_id == page_id, ExistingLink.page_version == version)
            .order_by(ExistingLink.ordinal)
        )
    ).all()
    return [
        SourceLink(block_id=r.block_id, link_type=r.link_type, target_page_id=r.page_id)
        for r in rows
    ]


def _counted_inbound() -> tuple[Any, Any, list[ColumnElement[bool]]]:
    """(source page alias, known url alias, conditions) of a COUNTED inbound link."""
    source = aliased(Page)
    known = aliased(KnownUrl)
    conditions: list[ColumnElement[bool]] = [
        ExistingLink.link_type == _INTERNAL,
        source.gone_at.is_(None),
        known.page_id.is_not(None),
        known.page_id != ExistingLink.page_id,  # a self link is not inbound
        or_(known.outcome.is_(None), known.outcome.not_in(_BROKEN_OUTCOMES)),
    ]
    return source, known, conditions


async def inbound_link_counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    """page id -> internal body links into it from the CURRENT versions of other live pages."""
    source, known, conditions = _counted_inbound()
    rows = (
        await session.execute(
            select(known.page_id, func.count())
            .select_from(ExistingLink)
            .join(
                source,
                and_(
                    source.id == ExistingLink.page_id,
                    source.current_version == ExistingLink.page_version,
                ),
            )
            .join(known, known.url == ExistingLink.resolved_url)
            .where(*conditions)
            .group_by(known.page_id)
        )
    ).all()
    return {r[0]: int(r[1]) for r in rows}


async def inbound_counts_for(
    session: AsyncSession, *, page_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """:func:`inbound_link_counts` restricted to ``page_ids`` (pages without links are absent)."""
    if not page_ids:
        return {}
    source, known, conditions = _counted_inbound()
    rows = (
        await session.execute(
            select(known.page_id, func.count())
            .select_from(ExistingLink)
            .join(
                source,
                and_(
                    source.id == ExistingLink.page_id,
                    source.current_version == ExistingLink.page_version,
                ),
            )
            .join(known, known.url == ExistingLink.resolved_url)
            .where(*conditions, known.page_id.in_(list(page_ids)))
            .group_by(known.page_id)
        )
    ).all()
    return {r[0]: int(r[1]) for r in rows}


def inbound_count_expression(target_id: Any) -> Any:
    """Correlated scalar: counted inbound links into ``target_id`` (a column)."""
    source, known, conditions = _counted_inbound()
    return (
        select(func.count())
        .select_from(ExistingLink)
        .join(
            source,
            and_(
                source.id == ExistingLink.page_id,
                source.current_version == ExistingLink.page_version,
            ),
        )
        .join(known, known.url == ExistingLink.resolved_url)
        .where(*conditions, known.page_id == target_id)
        .scalar_subquery()
    )


# --------------------------------------------------------------------------- #
# Reader helpers
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class PlanCounts:
    by_status: dict[str, int] = field(default_factory=dict)
    sources: int = 0
    targets: int = 0


async def plan_counts(session: AsyncSession) -> PlanCounts:
    by_status = {
        str(status): int(count)
        for status, count in (
            await session.execute(
                select(LinkPlanRow.status, func.count())
                .group_by(LinkPlanRow.status)
                .order_by(LinkPlanRow.status)
            )
        ).all()
    }
    sources = int(
        await session.scalar(select(func.count(func.distinct(LinkPlanRow.source_page_id)))) or 0
    )
    targets = int(
        await session.scalar(select(func.count(func.distinct(LinkPlanRow.target_page_id)))) or 0
    )
    return PlanCounts(by_status=by_status, sources=sources, targets=targets)


async def opportunity_counts(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Opportunity.kind, func.count())
            .group_by(Opportunity.kind)
            .order_by(Opportunity.kind)
        )
    ).all()
    counts: dict[str, int] = defaultdict(int)
    for kind, count in rows:
        counts[str(kind)] = int(count)
    return dict(counts)
