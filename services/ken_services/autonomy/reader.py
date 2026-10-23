"""``SqlAutonomyReader``: :class:`ken_contracts.autonomy.AutonomyReader` over PostgreSQL.

Read-only: the API hands it the READ ONLY session factory, so an autonomy route cannot write
even if a future handler tried. Every row -> view mapping lives in
:mod:`ken_core.db.repositories.autonomy`, so what the API shows and what the engine wrote are
the same shape re-validated through the frozen contracts.

:meth:`SqlAutonomyReader.get_health` is the one method that reads SETTINGS rather than rows,
and it is the honest answer to "can this installation actually publish?" -- the mode, both
acknowledgements, the CMS provider and whether its writes are on, combined into
``publishing_enabled``. It is ``False`` in ``shadow``, always, by construction and not by
convention.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.autonomy import (
    AutonomyDecision,
    AutonomyDecisionView,
    AutonomyHealth,
    AutonomyMode,
    AutonomySummary,
    BlockCode,
    BlockedActionView,
    PublishAttemptView,
    VerificationProfile,
)
from ken_contracts.errors import ListResponse, PageInfo
from ken_core.config import Settings
from ken_core.db.repositories import autonomy as repo
from ken_services.autonomy.calibration import CALIBRATION_RECIPE
from ken_services.autonomy.gate import publishing_enabled
from ken_services.autonomy.policy import load_configured_autonomy_policy

__all__ = ["SqlAutonomyReader"]


class SqlAutonomyReader:
    def __init__(
        self, *, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings

    @property
    def _mode(self) -> AutonomyMode:
        return AutonomyMode(self._settings.autonomy_mode)

    async def get_health(self) -> AutonomyHealth:
        return AutonomyHealth(
            mode=self._mode,
            autonomous_acknowledged=self._settings.autonomy_autonomous_acknowledged,
            allow_production=self._settings.autonomy_allow_production,
            cms_provider=self._settings.cms_provider,
            cms_writes_enabled=self._settings.cms_writes_enabled,
            publishing_enabled=publishing_enabled(self._settings),
            verification_profile_default=VerificationProfile(
                self._settings.autonomy_verification_profile
            ),
            calibration_version=await self._calibration_version(),
            policy_version=load_configured_autonomy_policy(self._settings).version,
        )

    async def _calibration_version(self) -> str | None:
        """The recipe this build calibrates with, or ``None`` when nothing is calibrated.

        Reported as the RECIPE and not a per-class version: a class without a sufficient
        evaluation row has no calibration at all, and the per-decision version on each
        ``autonomy_decisions`` row is where the exact evaluation run is recorded.
        """
        async with self._sessionmaker() as session:
            counts = await repo.decision_counts(session)
            await session.rollback()
        return CALIBRATION_RECIPE if counts else None

    async def get_summary(self) -> AutonomySummary:
        async with self._sessionmaker() as session:
            by_decision = await repo.decision_counts(session)
            by_mode = await repo.mode_counts(session)
            by_code = await repo.block_code_counts(session)
            blocked, published, rolled_back = await repo.plan_outcome_counts(session)
            await session.rollback()
        return AutonomySummary(
            decisions_by_decision=dict(sorted(by_decision.items())),
            decisions_by_mode=dict(sorted(by_mode.items())),
            blocks_by_code=dict(sorted(by_code.items())),
            blocked_plans=blocked,
            auto_published=published,
            rolled_back=rolled_back,
            mode=self._mode,
        )

    async def list_decisions(
        self,
        *,
        limit: int,
        offset: int,
        decision: AutonomyDecision | None = None,
        mode: AutonomyMode | None = None,
        plan_id: uuid.UUID | None = None,
    ) -> ListResponse[AutonomyDecisionView]:
        async with self._sessionmaker() as session:
            total = await repo.count_decisions(
                session, decision=decision, mode=mode, plan_id=plan_id
            )
            items = await repo.decision_views(
                session,
                limit=limit,
                offset=offset,
                decision=decision,
                mode=mode,
                plan_id=plan_id,
            )
            await session.rollback()
        return ListResponse[AutonomyDecisionView](
            items=items, page=PageInfo(total=total, limit=limit, offset=offset)
        )

    async def list_blocked(
        self, *, limit: int, offset: int, code: BlockCode | None = None
    ) -> ListResponse[BlockedActionView]:
        async with self._sessionmaker() as session:
            total = await repo.count_blocked(session, code=code)
            items = await repo.blocked_actions(session, limit=limit, offset=offset, code=code)
            await session.rollback()
        return ListResponse[BlockedActionView](
            items=items, page=PageInfo(total=total, limit=limit, offset=offset)
        )

    async def list_attempts(
        self, *, limit: int, offset: int, plan_id: uuid.UUID | None = None
    ) -> ListResponse[PublishAttemptView]:
        async with self._sessionmaker() as session:
            total = await repo.count_attempts(session, plan_id=plan_id)
            items = await repo.attempt_views(session, limit=limit, offset=offset, plan_id=plan_id)
            await session.rollback()
        return ListResponse[PublishAttemptView](
            items=items, page=PageInfo(total=total, limit=limit, offset=offset)
        )
