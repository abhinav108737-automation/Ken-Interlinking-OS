"""Phase 6 integration support: REAL engine plans approved by a reviewer and deployed through
:class:`DatabaseDeploymentService` into an in-memory CMS whose pages are the inventory's own
pages, with the REAL HTML library (``HtmlMutator`` / ``HtmlLinkVerifier``) inside both the
service and the CMS (ADR-0014 s2, s4).

Test doubles here wrap the real objects (fault injection on a CMS call, a scripted HTTP
probe); nothing replaces the deterministic mutation itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text

from ken_contracts.auth import Principal, Role
from ken_contracts.cms import (
    CMSPage,
    CMSUnavailableError,
    DeploymentResult,
    LinkChangeRequest,
    RollbackResult,
)
from ken_contracts.deployment import DeployRequest, DryRunRequest, RollbackReason, RollbackRequest
from ken_contracts.recommendations import LinkPlanStatus
from ken_contracts.review import ApproveRequest
from ken_core.providers.cms.factory import build_html_engine
from ken_core.providers.cms.mock import MockCMSProvider
from ken_services.deployment.http_probe import FetchOutcome, FetchResult
from ken_services.deployment.mutation import HtmlMutator
from ken_services.deployment.service import DatabaseDeploymentService
from ken_services.deployment.verification import HtmlLinkVerifier
from tests.integration.recommendation_support import SETTINGS, SITE, SM, rows
from tests.integration.review_support import (
    open_plans,
    principal,
    queue,
    review_service,
    seeded,
)

S = LinkPlanStatus
NOW_SLEEP: Final = 0.0


def cms_page_id(canonical_url: str) -> str:
    return "cms-" + canonical_url.removeprefix(SITE).strip("/").replace("/", "-")


async def inventory_pages(sessionmaker: SM) -> list[CMSPage]:
    """Every current inventory page as the CMS would hold it (its stored body HTML)."""
    found = await rows(
        sessionmaker,
        "SELECT p.canonical_url, v.title, v.body_html FROM pages p JOIN page_versions v "
        "ON v.page_id = p.id AND v.version = p.current_version ORDER BY p.canonical_url",
    )
    return [
        CMSPage(
            page_id=cms_page_id(r.canonical_url),
            url=r.canonical_url,
            canonical_url=r.canonical_url,
            title=r.title or "Page",
            body_html=r.body_html,
            version="v1",
        )
        for r in found
    ]


class FaultyCMS:
    """A real CMS provider with one-shot faults around ``apply_link_changes`` / ``rollback``:

    * ``fail_before`` -- raise :class:`CMSUnavailableError` WITHOUT calling the CMS (the app
      committed its intent; the CMS call timed out before reaching the CMS);
    * ``fail_after`` -- call the CMS (it COMMITS) and then raise (the app never learns the
      outcome);
    * ``unavailable`` -- every call raises.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = inner.name
        self.fail_before = 0
        self.fail_after = 0
        self.rollback_fail_before = 0
        self.rollback_fail_after = 0
        self.unavailable = False
        self.applied: list[LinkChangeRequest] = []
        self.before_apply: Callable[[LinkChangeRequest], Awaitable[None]] | None = None
        self.resolve_override: dict[str, str | None] = {}

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)

    def _check(self) -> None:
        if self.unavailable:
            raise CMSUnavailableError("injected: unavailable")

    async def get_page(self, *, page_id: str) -> CMSPage:
        self._check()
        page: CMSPage = await self.inner.get_page(page_id=page_id)
        return page

    async def validate_link_changes(self, *, request: LinkChangeRequest) -> Any:
        self._check()
        return await self.inner.validate_link_changes(request=request)

    async def apply_link_changes(self, *, request: LinkChangeRequest) -> DeploymentResult:
        self._check()
        self.applied.append(request)
        if self.before_apply is not None:
            await self.before_apply(request)
        if self.fail_before > 0:
            self.fail_before -= 1
            raise CMSUnavailableError("injected: timeout before the CMS")
        result: DeploymentResult = await self.inner.apply_link_changes(request=request)
        if self.fail_after > 0:
            self.fail_after -= 1
            raise CMSUnavailableError("injected: timeout after the CMS committed")
        return result

    async def rollback(self, *, deployment_id: str, operation_id: Any, actor: str) -> Any:
        self._check()
        if self.rollback_fail_before > 0:
            self.rollback_fail_before -= 1
            raise CMSUnavailableError("injected: timeout before the CMS")
        result: RollbackResult = await self.inner.rollback(
            deployment_id=deployment_id, operation_id=operation_id, actor=actor
        )
        if self.rollback_fail_after > 0:
            self.rollback_fail_after -= 1
            raise CMSUnavailableError("injected: timeout after the CMS committed")
        return result

    async def resolve_page_id(self, *, url: str) -> str | None:
        self._check()
        if url in self.resolve_override:
            return self.resolve_override[url]
        found: str | None = await self.inner.resolve_page_id(url=url)
        return found

    async def health(self) -> Any:
        return await self.inner.health()

    async def aclose(self) -> None:
        await self.inner.aclose()


@dataclass
class ScriptedProbe:
    """An ``HttpProbe`` stand-in: published pages come from the CMS (no publish lag unless
    ``lag``), targets answer ``targets[url]`` (default 200)."""

    cms: Any
    page_ids: dict[str, str]
    base: str = "http://published.test"
    targets: dict[str, FetchResult] = field(default_factory=dict)
    lag: bool = False
    published_outcome: FetchOutcome = FetchOutcome.OK
    calls: list[str] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return self.base + "/"

    def published_url(self, page_url: str) -> str:
        return self.base + page_url.removeprefix(SITE)

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        if url in self.targets:
            return self.targets[url]
        path = url.removeprefix(self.base)
        page_id = self.page_ids.get(SITE + path)
        if page_id is None:
            return FetchResult(FetchOutcome.FAILED, status=404, detail="http_404")
        if self.published_outcome is not FetchOutcome.OK:
            return FetchResult(self.published_outcome, detail="network_error")
        page = await self.cms.inner.get_page(page_id=page_id)
        body = page.body_html
        if self.lag:
            body = self._lagged.get(page_id, body)
        return FetchResult(FetchOutcome.OK, status=200, body=body)

    _lagged: dict[str, str] = field(default_factory=dict)


@dataclass
class World:
    sessionmaker: SM
    plans: dict[tuple[str, str], Any]
    reviewer: Principal
    operator: Principal
    viewer: Principal
    cms: FaultyCMS
    mock: MockCMSProvider

    def service(
        self,
        *,
        probe: Any = None,
        clock: Callable[[], datetime] | None = None,
        **overrides: Any,
    ) -> DatabaseDeploymentService:
        settings = SETTINGS.model_copy(update={"deploy_verify_retry_delay_s": 0.0, **overrides})
        kwargs: dict[str, Any] = {}
        if clock is not None:
            kwargs["clock"] = clock
        return DatabaseDeploymentService(
            sessionmaker=self.sessionmaker,
            settings=settings,
            cms=self.cms,
            mutator=HtmlMutator(),
            verifier=HtmlLinkVerifier(),
            probe=probe,
            sleep=_no_sleep,
            **kwargs,
        )

    def by_source(self, source: str) -> list[Any]:
        return [p for (s, _t), p in self.plans.items() if s == source]

    async def approve(self, *plans: Any) -> list[uuid.UUID]:
        service = review_service(self.sessionmaker)
        ids = []
        for plan in plans:
            if plan.status == S.PROPOSED.value:
                await queue(self.sessionmaker, plan.id)
            await service.approve(
                plan_id=plan.id,
                request=ApproveRequest(
                    operation_id=uuid.uuid4(), expected_status=S.AWAITING_APPROVAL
                ),
                principal=self.reviewer,
            )
            ids.append(plan.id)
        return ids

    def page_ids(self) -> dict[str, str]:
        return {p.url: p.page_id for p in self.mock._pages.values()}


async def _no_sleep(_seconds: float) -> None:
    return None


async def world(sessionmaker: SM, directory: Path) -> World:
    plans = await seeded(sessionmaker, directory)
    mock = MockCMSProvider(await inventory_pages(sessionmaker), html_engine=build_html_engine())
    return World(
        sessionmaker=sessionmaker,
        plans=plans,
        reviewer=await principal(sessionmaker, "rita", role=Role.REVIEWER),
        operator=await principal(sessionmaker, "otto", role=Role.OPERATOR_ADMIN),
        viewer=await principal(sessionmaker, "vic", role=Role.VIEWER),
        cms=FaultyCMS(mock),
        mock=mock,
    )


def deploy_request(plan_ids: Sequence[uuid.UUID], **kw: Any) -> DeployRequest:
    return DeployRequest(operation_id=kw.pop("op", uuid.uuid4()), plan_ids=list(plan_ids), **kw)


def dry_request(plan_ids: Sequence[uuid.UUID], **kw: Any) -> DryRunRequest:
    return DryRunRequest(operation_id=kw.pop("op", uuid.uuid4()), plan_ids=list(plan_ids), **kw)


def rollback_request(**kw: Any) -> RollbackRequest:
    return RollbackRequest(
        operation_id=kw.pop("op", uuid.uuid4()),
        reason=kw.pop("reason", RollbackReason.EDITORIAL_REVERSAL),
        **kw,
    )


async def plan_status(sessionmaker: SM, plan_id: uuid.UUID) -> LinkPlanStatus:
    found = await rows(sessionmaker, "SELECT status FROM link_plans WHERE id = :p", p=plan_id)
    return LinkPlanStatus(found[0].status)


async def statuses(sessionmaker: SM, plan_ids: Sequence[uuid.UUID]) -> list[LinkPlanStatus]:
    return [await plan_status(sessionmaker, p) for p in plan_ids]


async def count(sessionmaker: SM, table: str) -> int:
    async with sessionmaker() as session:
        return int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608


async def events(sessionmaker: SM, name: str) -> list[Any]:
    return await rows(
        sessionmaker,
        "SELECT * FROM events WHERE event_name = :n ORDER BY occurred_at, id",
        n=name,
    )


__all__ = [
    "FaultyCMS",
    "ScriptedProbe",
    "World",
    "cms_page_id",
    "count",
    "deploy_request",
    "dry_request",
    "events",
    "inventory_pages",
    "open_plans",
    "plan_status",
    "rollback_request",
    "statuses",
    "world",
]
