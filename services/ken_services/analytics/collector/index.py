"""The collector's in-memory index of LIVE links (the W1/W2 boundary).

Implements :class:`~ken_services.analytics.protocol.LiveLinkIndex`. Lookups are pure and
synchronous because they run on the request path; the index is rebuilt every
``collector.live_index_refresh_seconds`` from a READ ONLY session, and a failed rebuild keeps
the previous snapshot (an unreachable database must never turn into an exception inside an
ASGI handler).

**Which plans are live** (ADR-0015 §12.6.3, amendment 1e -- binding):

* the plan has a ``link_plan_transitions`` row to ``deployed`` (NEVER the ``deployment_items``
  stage: a CMS no-op records ``already_applied`` while the plan still becomes ``deployed``),
  and no row to ``rolled_back`` -- so its window ``[deployed_at, rolled_back_at)`` is open;
* its status is in :data:`~ken_services.deployment.service.LINK_ON_PAGE`, which includes
  ``failed`` ON PURPOSE: a failed deployment item may still have left the link on the page,
  and measuring is a different question (only ``verified`` ever measures).

**Resolution.** ``attr`` mode is ``(plan_id, path)``. ``href`` mode (for a CMS that strips
``data-*``, ADR-0014 §8) matches, among the live plans whose own source path equals ``path``,
those whose written ``deployment_items.href`` is ``hrefs_equivalent`` to the event's raw
``href`` -- both first resolved against the plan's ``source_url`` (§12.9.7) -- AND whose
``anchor_text`` is exactly equal. Exactly one match is accepted; none is ``unresolved_link``
and several is ``ambiguous_link``. Paths are compared through
:func:`~ken_services.analytics.protocol.normalized_path` on both sides, so a query string or
a dot segment can never decide a match.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.analytics import MatchMode
from ken_contracts.recommendations import LinkPlanStatus
from ken_core.db.models.deployment import DeploymentItem, ItemStage
from ken_core.db.models.recommendations import LinkPlan as LinkPlanRow
from ken_core.db.models.recommendations import LinkPlanTransition
from ken_core.logging import get_logger
from ken_services.analytics.protocol import (
    CollectorRejection,
    LinkResolution,
    normalized_path,
)
from ken_services.deployment.href_rules import hrefs_equivalent
from ken_services.deployment.service import LINK_ON_PAGE

__all__ = [
    "APPLIED_STAGES",
    "LiveLinkSnapshot",
    "LivePlan",
    "SqlLiveLinkIndex",
    "live_plan_query",
]

logger = get_logger("ken_services.analytics.collector.index")

APPLIED_STAGES: tuple[str, ...] = (ItemStage.APPLIED, ItemStage.ALREADY_APPLIED)
"""The stages whose ``href`` was actually written into the page (amendment 1a / 4)."""


@dataclass(frozen=True, slots=True)
class LivePlan:
    """One live plan, as the request path needs it."""

    plan_id: UUID
    source_url: str
    path: str
    """:func:`normalized_path` of ``source_url`` (``imp`` / ``clk`` match the source page)."""
    anchor_text: str
    hrefs: tuple[str, ...]
    """The distinct ``deployment_items.href`` values written for this plan."""


def _resolved(base: str, value: str) -> str | None:
    """``value`` resolved against ``base`` (ADR-0015 §12.9.7); ``None`` if unparseable."""
    try:
        return urljoin(base, value.strip())
    except ValueError:
        return None


class LiveLinkSnapshot:
    """An immutable, pure snapshot of the live plans. Satisfies the lookup half of
    :class:`~ken_services.analytics.protocol.LiveLinkIndex`."""

    __slots__ = ("_by_path", "_by_plan")

    def __init__(self, plans: Iterable[LivePlan] = ()) -> None:
        by_plan: dict[UUID, LivePlan] = {}
        by_path: dict[str, list[LivePlan]] = {}
        for plan in plans:
            by_plan[plan.plan_id] = plan
            by_path.setdefault(plan.path, []).append(plan)
        self._by_plan: Mapping[UUID, LivePlan] = by_plan
        self._by_path: Mapping[str, Sequence[LivePlan]] = by_path

    def __len__(self) -> int:
        return len(self._by_plan)

    def resolve_attr(self, *, plan_id: UUID, path: str) -> LinkResolution:
        plan = self._by_plan.get(plan_id)
        if plan is None:
            return LinkResolution(
                plan_id=None,
                match_mode=MatchMode.ATTR,
                rejection=CollectorRejection.LINK_NOT_LIVE,
            )
        if plan.path != path:
            return LinkResolution(
                plan_id=None,
                match_mode=MatchMode.ATTR,
                rejection=CollectorRejection.PATH_MISMATCH,
            )
        return LinkResolution(plan_id=plan_id, match_mode=MatchMode.ATTR)

    def resolve_href(self, *, path: str, href: str, anchor_text: str) -> LinkResolution:
        matches = [
            plan
            for plan in self._by_path.get(path, ())
            if plan.anchor_text == anchor_text and self._href_matches(plan, href)
        ]
        if len(matches) == 1:
            return LinkResolution(plan_id=matches[0].plan_id, match_mode=MatchMode.HREF)
        rejection = (
            CollectorRejection.UNRESOLVED_LINK if not matches else CollectorRejection.AMBIGUOUS_LINK
        )
        return LinkResolution(plan_id=None, match_mode=MatchMode.HREF, rejection=rejection)

    async def refresh(self) -> None:
        """Nothing to rebuild: a snapshot is fixed. Defined so a snapshot alone satisfies
        :class:`~ken_services.analytics.protocol.LiveLinkIndex` (fixtures and tests)."""
        return None

    @staticmethod
    def _href_matches(plan: LivePlan, href: str) -> bool:
        wanted = _resolved(plan.source_url, href)
        if wanted is None:
            return False
        return any(
            hrefs_equivalent(_resolved(plan.source_url, item), wanted) for item in plan.hrefs
        )


def live_plan_query() -> Select[UUID, str, str, list[str] | None]:
    """The live-plan SELECT (also used by the tests, so the rule is asserted once)."""
    hrefs = (
        select(func.array_agg(func.distinct(DeploymentItem.href)))
        .where(
            DeploymentItem.plan_id == LinkPlanRow.id,
            DeploymentItem.stage.in_(APPLIED_STAGES),
        )
        .correlate(LinkPlanRow)
        .scalar_subquery()
    )
    deployed = (
        select(LinkPlanTransition.id)
        .where(
            LinkPlanTransition.plan_id == LinkPlanRow.id,
            LinkPlanTransition.to_status == LinkPlanStatus.DEPLOYED.value,
        )
        .exists()
    )
    rolled_back = (
        select(LinkPlanTransition.id)
        .where(
            LinkPlanTransition.plan_id == LinkPlanRow.id,
            LinkPlanTransition.to_status == LinkPlanStatus.ROLLED_BACK.value,
        )
        .exists()
    )
    return select(
        LinkPlanRow.id, LinkPlanRow.source_url, LinkPlanRow.anchor_text, hrefs.label("hrefs")
    ).where(
        LinkPlanRow.status.in_(sorted(status.value for status in LINK_ON_PAGE)),
        deployed,
        ~rolled_back,
    )


class SqlLiveLinkIndex:
    """:class:`~ken_services.analytics.protocol.LiveLinkIndex` over the application database.

    ``sessionmaker`` must be a READ ONLY factory: the collector never writes through it.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker
        self._snapshot = LiveLinkSnapshot()

    @property
    def size(self) -> int:
        return len(self._snapshot)

    def resolve_attr(self, *, plan_id: UUID, path: str) -> LinkResolution:
        return self._snapshot.resolve_attr(plan_id=plan_id, path=path)

    def resolve_href(self, *, path: str, href: str, anchor_text: str) -> LinkResolution:
        return self._snapshot.resolve_href(path=path, href=href, anchor_text=anchor_text)

    async def refresh(self) -> None:
        """Rebuild from the database. On failure keep the previous snapshot (never raises)."""
        try:
            async with self._sessionmaker() as session:
                rows = (await session.execute(live_plan_query())).all()
        except Exception as exc:  # the request path must never see a database error
            logger.warning("collector_live_index_refresh_failed", error_class=type(exc).__name__)
            return
        plans: list[LivePlan] = []
        for plan_id, source_url, anchor_text, hrefs in rows:
            path = normalized_path(source_url)
            if path is None:
                continue
            plans.append(
                LivePlan(
                    plan_id=plan_id,
                    source_url=source_url,
                    path=path,
                    anchor_text=anchor_text,
                    hrefs=tuple(href for href in (hrefs or ()) if href),
                )
            )
        self._snapshot = LiveLinkSnapshot(plans)
        logger.info("collector_live_index_refreshed", live_plans=len(plans))
